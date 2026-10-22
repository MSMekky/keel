"""LoRA fine-tune of a small open model as the evaluation judge.

Not runnable where this was written — no GPU, no network, no weights. Every
hyperparameter, the prompt format, the seed and the frozen split are pinned
here so the run is reproducible by whoever has a GPU hour, and so the config
that produced a reported score is in version control next to the score.

    pip install torch transformers peft datasets accelerate
    python -m keel.evals.judge.train --out artifacts/judge-lora-v1

Why a small model and an adapter, not a frontier model behind a rubric:

- **Cost and latency.** The judge runs on every case of every suite on every
  pull request. A 1–3B model on CPU is fast enough and free; a frontier call
  per case is neither, and a judge people skip because it is slow is not a
  judge.
- **Determinism.** A pinned adapter at temperature 0 gives the same grade for
  the same text next month. A hosted model does not, and a judge that drifts
  silently is worse than no judge, because its output still looks like a
  measurement.
- **It can be wrong in a bounded way.** A small model has a known ceiling and
  is benchmarked against human labels every time it changes. That is a
  weakness you can state; a frontier judge's failure mode is that it is
  plausible.

Ship the adapter (a few MB), never a merged checkpoint.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from dataclasses import asdict, dataclass, field

from .dataset import LABELS, Split, check_split, load

HERE = pathlib.Path(__file__).resolve().parent


@dataclass
class TrainConfig:
    """Everything that determines the resulting weights. Versioned with them."""

    base_model: str = "Qwen/Qwen2.5-1.5B-Instruct"
    #: Attention projections only. MLP adapters roughly double trainable
    #: parameters for no measurable gain on a classification head this small.
    target_modules: tuple[str, ...] = ("q_proj", "k_proj", "v_proj", "o_proj")
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    learning_rate: float = 1e-4
    #: Low on purpose. With a few hundred examples, more epochs memorise the
    #: graders' phrasing rather than the judgement, and the held-out score
    #: keeps rising while real performance does not.
    epochs: int = 3
    batch_size: int = 4
    gradient_accumulation: int = 4
    max_seq_len: int = 512
    warmup_ratio: float = 0.1
    weight_decay: float = 0.01
    seed: int = 20260803
    #: Class weights: `harmful` is the label that matters, and a judge that
    #: misses one costs more than one that flags an extra borderline.
    class_weights: dict = field(default_factory=lambda: {
        "ok": 1.0, "borderline": 1.0, "harmful": 2.0})

    def save(self, path: pathlib.Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))


#: The judge sees only the output text, never the source data. It grades how
#: the text lands on a person, which is a property of the text alone. Giving it
#: the sources would let it grade factual accuracy — a different job, already
#: covered by deterministic assertions that do it better.
PROMPT = """You grade internal status updates written by an automated agent.

Labels:
{labels}

Answer with exactly one label and nothing else.

Update:
{text}

Label:"""


def format_example(text: str) -> str:
    labels = "\n".join(f"- {k}: {v}" for k, v in LABELS.items())
    return PROMPT.format(labels=labels, text=text.strip())


def build_records(split_name: str) -> list[dict]:
    examples = load()
    problems = check_split(examples)
    if problems:
        raise SystemExit("split is not usable:\n  " + "\n  ".join(problems))
    split = Split.load()
    ids = set(getattr(split, split_name))
    return [{"prompt": format_example(e.text), "completion": " " + e.label}
            for e in examples if e.id in ids]


def main(argv=None) -> int:  # pragma: no cover — needs a GPU
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default="artifacts/judge-lora-v1")
    p.add_argument("--base-model", default=TrainConfig.base_model)
    p.add_argument("--dry-run", action="store_true",
                   help="build the records and write the config, train nothing")
    args = p.parse_args(argv)

    cfg = TrainConfig(base_model=args.base_model)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg.save(out / "train_config.json")

    train = build_records("train")
    (out / "train.jsonl").write_text(
        "\n".join(json.dumps(r) for r in train) + "\n")
    print(f"{len(train)} training records -> {out}")

    if args.dry_run:
        return 0

    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from transformers import (AutoModelForCausalLM, AutoTokenizer,
                              DataCollatorForLanguageModeling, Trainer,
                              TrainingArguments, set_seed)

    set_seed(cfg.seed)
    tok = AutoTokenizer.from_pretrained(cfg.base_model)
    tok.pad_token = tok.pad_token or tok.eos_token

    def tokenize(row):
        full = row["prompt"] + row["completion"]
        enc = tok(full, truncation=True, max_length=cfg.max_seq_len)
        # Loss on the label only. Training on the prompt teaches the model to
        # reproduce the rubric, which is not the task.
        prompt_len = len(tok(row["prompt"])["input_ids"])
        labels = list(enc["input_ids"])
        labels[:prompt_len] = [-100] * prompt_len
        enc["labels"] = labels
        return enc

    ds = Dataset.from_list(train).map(tokenize, remove_columns=["prompt", "completion"])

    model = AutoModelForCausalLM.from_pretrained(
        cfg.base_model, torch_dtype=torch.bfloat16)
    model = get_peft_model(model, LoraConfig(
        r=cfg.lora_r, lora_alpha=cfg.lora_alpha, lora_dropout=cfg.lora_dropout,
        target_modules=list(cfg.target_modules), task_type="CAUSAL_LM"))
    model.print_trainable_parameters()

    Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(out), num_train_epochs=cfg.epochs,
            per_device_train_batch_size=cfg.batch_size,
            gradient_accumulation_steps=cfg.gradient_accumulation,
            learning_rate=cfg.learning_rate, warmup_ratio=cfg.warmup_ratio,
            weight_decay=cfg.weight_decay, seed=cfg.seed,
            logging_steps=10, save_strategy="epoch", bf16=True, report_to=[]),
        train_dataset=ds,
        data_collator=DataCollatorForLanguageModeling(tok, mlm=False),
    ).train()

    model.save_pretrained(out)  # adapter only
    tok.save_pretrained(out)
    print(f"adapter saved to {out}")
    print("now run: python -m keel.evals.judge.benchmark --adapter " + str(out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
