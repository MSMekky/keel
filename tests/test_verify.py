"""Tests for the verification companion.

The script's whole value is that its output cannot be nicer than what
happened. These tests are what stop that from quietly becoming untrue — the
interesting ones assert that a failed run produces a log saying so, and that
the thresholds protecting the judge dataset actually refuse.

The live path itself is not tested here; it needs credentials and a network,
and a test that mocked those would be asserting that the mock works.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("keel_verify",
                                               ROOT / "keel_verify.py")
kv = importlib.util.module_from_spec(_spec)
sys.modules["keel_verify"] = kv
_spec.loader.exec_module(kv)


def evidence(*steps: tuple[str, bool]) -> "kv.Evidence":
    ev = kv.Evidence(started_at="2026-08-04T09:00:00", repo="acme/throwaway",
                     model="claude-sonnet-5-5", python="3.12.1")
    for name, ok in steps:
        ev.steps.append(kv.Step(
            name=name, ok=ok,
            detail="looked fine" if ok else "",
            error="" if ok else "RuntimeError: the thing did not happen",
            calls=[("POST", "https://api.anthropic.com/v1/messages", 200)],
            observed={"status": "committed" if ok else "rolled_back"}))
    return ev


class TestTheLogCannotOverclaim(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._orig = kv.RUN_LOG
        kv.RUN_LOG = self.tmp / "RUN-LOG.md"

    def tearDown(self):
        kv.RUN_LOG = self._orig

    def test_a_failed_step_produces_a_not_verified_log(self):
        kv.write_run_log(evidence(("a", True), ("b", False)), outputs=[])
        text = kv.RUN_LOG.read_text()
        self.assertIn("**NOT verified**", text)
        self.assertNotIn("**VERIFIED end-to-end.**", text)
        self.assertIn("Do not remove the UNVERIFIED markers", text)

    def test_an_all_passing_run_says_verified(self):
        kv.write_run_log(evidence(("a", True), ("b", True)), outputs=[])
        self.assertIn("**VERIFIED end-to-end.**", kv.RUN_LOG.read_text())

    def test_an_empty_run_is_not_verified(self):
        """Zero steps must not vacuously pass."""
        ev = evidence()
        self.assertFalse(ev.all_passed)
        kv.write_run_log(ev, outputs=[])
        self.assertIn("**NOT verified**", kv.RUN_LOG.read_text())

    def test_the_failure_text_reaches_the_log(self):
        kv.write_run_log(evidence(("b", False)), outputs=[])
        self.assertIn("the thing did not happen", kv.RUN_LOG.read_text())

    def test_real_http_codes_are_recorded_not_summarised(self):
        kv.write_run_log(evidence(("a", True)), outputs=[])
        text = kv.RUN_LOG.read_text()
        self.assertIn("api.anthropic.com/v1/messages", text)
        self.assertIn("200", text)

    def test_agent_output_is_quoted_verbatim(self):
        kv.write_run_log(evidence(("a", True)), outputs=[
            {"agent": "repo-weekly-digest", "run_id": "run_abc",
             "text": "Three changes merged. @nadia fixed a pool leak."}])
        text = kv.RUN_LOG.read_text()
        self.assertIn("@nadia fixed a pool leak", text)
        self.assertIn("run_abc", text)


class TestJudgeGuardrails(unittest.TestCase):
    def test_the_production_threshold_is_high_enough_to_mean_something(self):
        """A LoRA on a dozen examples reports a number and measures nothing."""
        self.assertGreaterEqual(kv.MIN_PRODUCTION, 50)

    def test_train_refuses_without_enough_production_examples(self):
        import argparse

        args = argparse.Namespace(out="artifacts/x", dry_run=True, force=False)
        self.assertEqual(kv.cmd_train(args), 1)

    def test_status_reports_absence_rather_than_silence(self):
        """`status` must say 'none', not print nothing and look fine."""
        import argparse
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            kv.cmd_status(argparse.Namespace())
        out = buf.getvalue()
        self.assertIn("not verified", out)
        self.assertIn("rubric is wired in", out)


class TestEvidenceShape(unittest.TestCase):
    def test_evidence_serialises_for_review(self):
        ev = evidence(("a", True), ("b", False))
        blob = json.dumps({"steps": [vars(s) for s in ev.steps]}, default=str)
        self.assertIn("rolled_back", blob)

    def test_all_passed_is_false_if_any_step_failed(self):
        self.assertTrue(evidence(("a", True)).all_passed)
        self.assertFalse(evidence(("a", True), ("b", False)).all_passed)


if __name__ == "__main__":
    unittest.main()
