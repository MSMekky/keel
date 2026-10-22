"""Benchmark the judge against the rubric baseline, on human labels.

Three arms on the held-out set:

  ``human``     the labels themselves — ground truth, and the ceiling
  ``rubric``    a rubric-prompted model, the incumbent
  ``finetuned`` the LoRA adapter, the challenger

The comparison that matters is agreement with the human labels. If the
fine-tune does not beat the rubric, **that is the result**, it goes in the
README, and the rubric stays wired in. A negative result honestly reported is
worth more than a win reached for by moving the split, and anyone reading the
repo can tell the difference.

Metrics, and why these:

``accuracy``          plain agreement. Easy to read, misleading alone when the
                      classes are unbalanced.
``cohen_kappa``       agreement corrected for chance. On three classes with a
                      skewed distribution, this is the number to quote.
``harmful_recall``    of everything a human called harmful, how much the judge
                      caught. **The number that decides deployment.** A judge
                      that misses a harmful update is worse than no judge,
                      because the gate then reports green.
``harmful_precision`` how often a `harmful` verdict was right. Matters second:
                      a noisy judge gets ignored, but an over-cautious one
                      only costs review time.

The human ceiling is not 1.0 and pretending otherwise sets a target nobody can
hit. If two people grade the same hundred outputs they will disagree on the
borderline cases, and ``--second-pass`` measures exactly that so the judge can
be compared against how well humans agree with each other rather than against
perfection.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Callable

from .dataset import LABELS, Example, Split, check_split, load
from .train import format_example

HERE = pathlib.Path(__file__).resolve().parent
REPORT = HERE / "report.json"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def accuracy(truth: list[str], pred: list[str]) -> float:
    if not truth:
        return 0.0
    return sum(t == p for t, p in zip(truth, pred)) / len(truth)


def cohen_kappa(truth: list[str], pred: list[str]) -> float:
    """Agreement corrected for what chance would produce."""
    n = len(truth)
    if n == 0:
        return 0.0
    po = accuracy(truth, pred)
    tc, pc = collections.Counter(truth), collections.Counter(pred)
    pe = sum((tc[k] / n) * (pc[k] / n) for k in LABELS)
    if pe == 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


def recall(truth: list[str], pred: list[str], label: str) -> float:
    relevant = [i for i, t in enumerate(truth) if t == label]
    if not relevant:
        return float("nan")
    return sum(pred[i] == label for i in relevant) / len(relevant)


def precision(truth: list[str], pred: list[str], label: str) -> float:
    flagged = [i for i, p in enumerate(pred) if p == label]
    if not flagged:
        return float("nan")
    return sum(truth[i] == label for i in flagged) / len(flagged)


def confusion(truth: list[str], pred: list[str]) -> dict[str, dict[str, int]]:
    m = {t: {p: 0 for p in LABELS} for t in LABELS}
    for t, p in zip(truth, pred):
        m[t][p] += 1
    return m


@dataclass
class ArmResult:
    arm: str
    n: int
    accuracy: float
    cohen_kappa: float
    harmful_recall: float
    harmful_precision: float
    confusion: dict = field(default_factory=dict)
    note: str = ""


def score_arm(name: str, truth: list[str], pred: list[str],
              note: str = "") -> ArmResult:
    return ArmResult(
        arm=name, n=len(truth),
        accuracy=round(accuracy(truth, pred), 4),
        cohen_kappa=round(cohen_kappa(truth, pred), 4),
        harmful_recall=round(recall(truth, pred, "harmful"), 4),
        harmful_precision=round(precision(truth, pred, "harmful"), 4),
        confusion=confusion(truth, pred), note=note)


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def rubric_arm(provider=None) -> Callable[[str], str]:
    """The incumbent: a model prompted with the rubric, no training.

    With no provider configured this returns a majority-class predictor, which
    is the correct floor: it makes the benchmark runnable offline and it makes
    a fine-tune that fails to beat it look exactly as bad as it is.
    """
    if provider is None:
        return lambda text: "ok"

    def grade(text: str) -> str:
        c = provider.complete(
            [{"role": "system", "content": format_example("").split("Update:")[0]},
             {"role": "user", "content": text}], [])
        answer = (c.text or "").strip().lower()
        for label in LABELS:
            if label in answer:
                return label
        return "ok"

    return grade


def finetuned_arm(adapter: str) -> Callable[[str], str]:  # pragma: no cover
    """The challenger. Requires torch/peft and the adapter directory."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    cfg = json.loads((pathlib.Path(adapter) / "train_config.json").read_text())
    tok = AutoTokenizer.from_pretrained(adapter)
    base = AutoModelForCausalLM.from_pretrained(cfg["base_model"],
                                                torch_dtype=torch.bfloat16)
    model = PeftModel.from_pretrained(base, adapter).eval()

    def grade(text: str) -> str:
        prompt = format_example(text)
        ids = tok(prompt, return_tensors="pt")
        with torch.no_grad():
            # Greedy. A judge that samples is a judge that disagrees with
            # itself, and then nobody can tell a regression from a re-roll.
            out = model.generate(**ids, max_new_tokens=6, do_sample=False,
                                 pad_token_id=tok.eos_token_id)
        answer = tok.decode(out[0][ids["input_ids"].shape[1]:]).strip().lower()
        for label in LABELS:
            if label in answer:
                return label
        return "ok"

    return grade


# ---------------------------------------------------------------------------


def holdout_examples() -> list[Example]:
    examples = load()
    problems = check_split(examples)
    if problems:
        raise SystemExit("split is not usable:\n  " + "\n  ".join(problems))
    ids = set(Split.load().holdout)
    return [e for e in examples if e.id in ids]


def run(adapter: str | None = None, provider=None,
        second_pass: dict[str, str] | None = None) -> dict:
    held = holdout_examples()
    truth = [e.label for e in held]
    texts = [e.text for e in held]

    arms = [score_arm("rubric", truth, [rubric_arm(provider)(t) for t in texts],
                      note="prompted baseline"
                           + ("" if provider else " (offline floor: majority class)"))]

    if adapter:
        grade = finetuned_arm(adapter)
        arms.append(score_arm("finetuned", truth, [grade(t) for t in texts],
                              note=f"adapter {adapter}"))

    if second_pass:
        # Human vs human. The realistic ceiling for any of the above.
        pairs = [(e.label, second_pass[e.id]) for e in held if e.id in second_pass]
        if pairs:
            arms.append(score_arm("human_second_pass", [a for a, _ in pairs],
                                  [b for _, b in pairs],
                                  note="inter-rater agreement; the real ceiling"))

    best = max(arms, key=lambda a: a.cohen_kappa)
    winner = best.arm if best.arm != "human_second_pass" else "rubric"
    finetuned = next((a for a in arms if a.arm == "finetuned"), None)
    rubric = next(a for a in arms if a.arm == "rubric")

    verdict = "no fine-tuned adapter benchmarked; rubric remains wired in"
    if finetuned:
        if finetuned.cohen_kappa > rubric.cohen_kappa:
            verdict = (f"fine-tuned judge wins on kappa "
                       f"({finetuned.cohen_kappa:.3f} vs {rubric.cohen_kappa:.3f}); "
                       f"wire it in")
        else:
            verdict = (f"fine-tuned judge does NOT beat the rubric "
                       f"({finetuned.cohen_kappa:.3f} vs {rubric.cohen_kappa:.3f}); "
                       f"keep the rubric and report this")

    return {
        "holdout_n": len(held),
        "split_seed": Split.load().seed,
        "dataset_hash": Split.load().dataset_hash,
        "arms": [asdict(a) for a in arms],
        "default_judge": winner,
        "verdict": verdict,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--adapter", help="path to a trained LoRA adapter")
    p.add_argument("--second-pass", help="JSON of {example_id: label} from a "
                                         "second human grader")
    p.add_argument("--out", default=str(REPORT))
    args = p.parse_args(argv)

    second = json.loads(pathlib.Path(args.second_pass).read_text()) \
        if args.second_pass else None
    report = run(adapter=args.adapter, second_pass=second)
    pathlib.Path(args.out).write_text(json.dumps(report, indent=2))

    print(f"held-out n={report['holdout_n']}  (production only)")
    for arm in report["arms"]:
        print(f"  {arm['arm']:<20} acc={arm['accuracy']:.3f}  "
              f"kappa={arm['cohen_kappa']:.3f}  "
              f"harmful recall={arm['harmful_recall']:.3f}")
    print(f"\n{report['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
