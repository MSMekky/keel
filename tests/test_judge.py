"""Judge tests.

Two groups. The first covers the metrics and the split rules — ordinary
correctness. The second is the one that matters: it fails when a number
reported in prose stops matching the number in the eval output, which is how
benchmark claims rot.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

from keel.evals.judge import benchmark as bm
from keel.evals.judge.dataset import (
    LABELS, DatasetError, Example, build_split, check_split, dataset_hash, load)

HERE = pathlib.Path(__file__).resolve().parent.parent
REPORT = HERE / "keel" / "evals" / "judge" / "report.json"
README = HERE / "README.md"


def ex(i, label="ok", prov="production", family=None, text="t") -> Example:
    return Example(id=i, text=text, label=label, provenance=prov, family=family)


class TestDatasetRules(unittest.TestCase):
    def test_seed_only_dataset_refuses_to_produce_a_split(self):
        """The circularity trap, refused rather than documented.

        A judge trained and tested on generated data measures the generator.
        """
        with self.assertRaises(DatasetError) as ctx:
            build_split([ex("s1", prov="seed"), ex("s2", prov="seed")])
        self.assertIn("measures the generator", str(ctx.exception))

    def test_smoke_test_escape_hatch_is_explicit(self):
        split = build_split([ex("s1", prov="seed"), ex("s2", prov="seed")],
                            allow_no_production=True)
        self.assertEqual(len(split.train) + len(split.holdout), 2)

    def test_holdout_is_production_only(self):
        pool = ([ex(f"p{i}") for i in range(10)]
                + [ex(f"s{i}", prov="seed") for i in range(10)])
        split = build_split(pool)
        by_id = {e.id: e for e in pool}
        for i in split.holdout:
            self.assertEqual(by_id[i].provenance, "production")

    def test_synthetic_variants_stay_with_their_source(self):
        """Rule 3 — the one that quietly inflates every number downstream."""
        pool = [ex(f"p{i}") for i in range(10)]
        pool += [ex(f"p{i}-var", prov="synthetic", family=f"p{i}")
                 for i in range(10)]
        split = build_split(pool)
        by_id = {e.id: e for e in pool}
        train_groups = {by_id[i].group for i in split.train}
        holdout_groups = {by_id[i].group for i in split.holdout}
        self.assertEqual(train_groups & holdout_groups, set())

    def test_a_synthetic_example_without_a_family_is_rejected(self):
        with self.assertRaises(ValueError):
            Example(id="x", text="t", label="ok", provenance="synthetic")

    def test_unknown_label_or_provenance_is_rejected(self):
        with self.assertRaises(ValueError):
            Example(id="x", text="t", label="mostly_fine", provenance="seed")
        with self.assertRaises(ValueError):
            Example(id="x", text="t", label="ok", provenance="vibes")

    def test_split_is_deterministic_for_a_seed(self):
        pool = [ex(f"p{i}") for i in range(20)]
        a, b = build_split(pool, seed=7), build_split(pool, seed=7)
        self.assertEqual(a.holdout, b.holdout)
        self.assertNotEqual(build_split(pool, seed=8).holdout, a.holdout)

    def test_check_split_detects_a_changed_dataset(self):
        pool = [ex(f"p{i}") for i in range(10)]
        split = build_split(pool)
        problems = check_split(pool + [ex("p99")], split)
        self.assertTrue(any("different dataset" in p for p in problems))

    def test_check_split_detects_family_leakage(self):
        pool = [ex("p1"), ex("p1-var", prov="synthetic", family="p1")]
        split = build_split(pool)
        split.train, split.holdout = ["p1"], ["p1-var"]
        split.dataset_hash = dataset_hash(pool)
        problems = check_split(pool, split)
        self.assertTrue(any("leakage" in p for p in problems))

    def test_shipped_seed_examples_are_valid_and_cover_every_label(self):
        examples = load()
        self.assertGreaterEqual(len(examples), 10)
        self.assertEqual({e.label for e in examples}, set(LABELS))


class TestMetrics(unittest.TestCase):
    def test_accuracy(self):
        self.assertEqual(bm.accuracy(["ok", "ok"], ["ok", "harmful"]), 0.5)

    def test_kappa_is_zero_when_agreement_is_chance(self):
        truth = ["ok", "harmful"] * 10
        pred = ["ok"] * 10 + ["harmful"] * 10
        self.assertAlmostEqual(bm.cohen_kappa(truth, pred), 0.0, places=6)

    def test_kappa_is_one_on_perfect_agreement(self):
        truth = ["ok", "harmful", "borderline"] * 5
        self.assertEqual(bm.cohen_kappa(truth, truth), 1.0)

    def test_kappa_punishes_a_majority_class_predictor(self):
        """Accuracy flatters it; kappa does not. This is why kappa is quoted."""
        truth = ["ok"] * 18 + ["harmful"] * 2
        pred = ["ok"] * 20
        self.assertGreater(bm.accuracy(truth, pred), 0.85)
        self.assertAlmostEqual(bm.cohen_kappa(truth, pred), 0.0, places=6)

    def test_harmful_recall_is_the_deployment_number(self):
        truth = ["harmful", "harmful", "ok"]
        pred = ["harmful", "ok", "ok"]
        self.assertAlmostEqual(bm.recall(truth, pred, "harmful"), 0.5)
        self.assertAlmostEqual(bm.precision(truth, pred, "harmful"), 1.0)

    def test_confusion_matrix_totals(self):
        m = bm.confusion(["ok", "harmful"], ["ok", "ok"])
        self.assertEqual(m["ok"]["ok"], 1)
        self.assertEqual(m["harmful"]["ok"], 1)

    def test_offline_rubric_floor_is_a_majority_predictor(self):
        """Deliberate: a fine-tune that cannot beat this is worthless."""
        self.assertEqual(bm.rubric_arm(None)("anything at all"), "ok")


class TestReportedNumbersDoNotDrift(unittest.TestCase):
    """The test the plan asked for: prose cannot outrun the eval output."""

    def test_readme_judge_claims_match_the_report(self):
        section = _judge_section()
        if section is None:
            self.skipTest("README has no judge benchmark section yet")

        claimed = re.findall(r"(rubric|finetuned)\s*[:=]?\s*kappa\s*([0-9.]+)",
                             section, re.I)

        if not REPORT.exists():
            # The symmetric half, and the one that actually bites: prose may
            # not quote a score that no benchmark produced.
            self.assertEqual(
                claimed, [],
                "README quotes judge kappa numbers but keel/evals/judge/"
                "report.json does not exist. Run the benchmark or delete the "
                "claim.")
            return

        report = json.loads(REPORT.read_text())
        by_arm = {a["arm"]: a for a in report["arms"]}
        self.assertTrue(claimed,
                        "a benchmark has been run but the README quotes no "
                        "numbers from it; state them or remove report.json")
        for arm, value in claimed:
            arm = arm.lower()
            self.assertIn(arm, by_arm, f"README cites arm {arm!r}, report has "
                                       f"{sorted(by_arm)}")
            self.assertAlmostEqual(
                float(value), by_arm[arm]["cohen_kappa"], places=3,
                msg=f"README says {arm} kappa {value}, report.json says "
                    f"{by_arm[arm]['cohen_kappa']}. Re-run the benchmark or fix "
                    f"the README; do not let them disagree.")

    def test_report_if_present_is_internally_consistent(self):
        if not REPORT.exists():
            self.skipTest("no benchmark has been run yet")
        report = json.loads(REPORT.read_text())
        self.assertIn(report["default_judge"], {"rubric", "finetuned"})
        arms = {a["arm"] for a in report["arms"]}
        self.assertIn(report["default_judge"], arms)
        if report["default_judge"] == "finetuned":
            ft = next(a for a in report["arms"] if a["arm"] == "finetuned")
            rb = next(a for a in report["arms"] if a["arm"] == "rubric")
            self.assertGreater(
                ft["cohen_kappa"], rb["cohen_kappa"],
                "the fine-tuned judge is wired in without beating the rubric")

    def test_the_wired_in_judge_matches_the_report(self):
        from keel.evals.harness import RubricJudge, default_judge

        judge = default_judge()
        if not REPORT.exists():
            self.assertIsInstance(judge, RubricJudge)
            return
        report = json.loads(REPORT.read_text())
        if report["default_judge"] == "rubric":
            self.assertIs(type(judge), RubricJudge)


def _judge_section() -> str | None:
    if not README.exists():
        return None
    text = README.read_text()
    m = re.search(r"^##+ .*judge.*$", text, re.I | re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^##+ ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


if __name__ == "__main__":
    unittest.main()
