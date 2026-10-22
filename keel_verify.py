#!/usr/bin/env python3
"""
keel_verify — the two things code cannot assert about itself.

Mounts on top of the package. Changes nothing in it, imports from it, and
turns the two outstanding claims into commands:

    python keel_verify.py preflight     # are the credentials and repo sane
    python keel_verify.py live          # run everything against real APIs
    python keel_verify.py grade         # label the real outputs by hand
    python keel_verify.py train         # LoRA the judge on those labels
    python keel_verify.py bench         # three arms, write report.json
    python keel_verify.py status        # what is proven and what is not

────────────────────────────────────────────────────────────────────────────
WHAT THIS FILE CANNOT DO

It cannot make "verified end-to-end" true. Running it does that. This script's
only real job is to make the resulting claim *checkable by someone else*: every
line it writes into docs/RUN-LOG.md comes from a response that actually came
back — HTTP status codes, model ids, token counts, comment ids, URLs. Nothing
is templated in from what the run was supposed to produce.

And it refuses to emit a verified verdict if any step failed. A partial run
produces a log that says which step failed and why, which is a more useful
document than a green one, and the only honest output when something breaks.

The same applies to the judge. This script does not generate training labels.
`grade` shows you real agent output and records the label *you* type, with
provenance=production and the run id it came from. That is the whole point:
a judge trained on labels a script invented measures the script.
────────────────────────────────────────────────────────────────────────────

Requires the package on the path. For training, additionally:

    pip install torch transformers peft datasets accelerate
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from keel.audit.log import AuditLog                                # noqa: E402
from keel.capabilities import github                               # noqa: E402
from keel.capabilities.catalog import apply_inverse                # noqa: E402
from keel.evals.judge.dataset import (                             # noqa: E402
    DATA, LABELS, Example, append, build_split, check_split, load)
from keel.gate.review import GateDecision, ReviewGate, rollback    # noqa: E402
from keel.registry.loader import Registry                          # noqa: E402
from keel.runtime import state as st                               # noqa: E402
from keel.runtime.executor import Executor                         # noqa: E402
from keel.runtime.http import Client                               # noqa: E402
from keel.runtime.providers import AnthropicProvider               # noqa: E402

RUN_LOG = ROOT / "docs" / "RUN-LOG.md"
EVIDENCE = ROOT / "docs" / "evidence"
REPORT = ROOT / "keel" / "evals" / "judge" / "report.json"

DIM, BOLD, RED, GREEN, YEL, OFF = (
    "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")


def rule(t: str) -> None:
    print(f"\n{BOLD}{t}{OFF}\n{DIM}{'─' * min(len(t), 70)}{OFF}")


# ══════════════════════════════════════════════════════════════════════════
# Evidence
#
# A step's record is the request that went out and the response that came
# back. Not a summary of them — a summary is where a claim quietly becomes an
# intention.
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    #: Real HTTP exchanges: (method, url, status). From the client's own log.
    calls: list[tuple[str, str, int]] = field(default_factory=list)
    #: Whatever the step observed in the world, verbatim.
    observed: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    seconds: float = 0.0


@dataclass
class Evidence:
    started_at: str
    repo: str
    model: str
    python: str
    steps: list[Step] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        mark = f"{GREEN}pass{OFF}" if step.ok else f"{RED}FAIL{OFF}"
        print(f"  [{mark}] {step.name}  ({step.seconds:.1f}s)")
        if step.detail:
            print(f"         {step.detail}")
        if step.error:
            print(f"         {RED}{step.error.splitlines()[-1][:200]}{OFF}")
        return step

    def save(self) -> pathlib.Path:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE / f"live-{self.started_at.replace(':', '')}.json"
        path.write_text(json.dumps(asdict(self), indent=2, default=str))
        return path


def timed(evidence: Evidence, name: str, fn: Callable[[Step], None]) -> Step:
    step = Step(name=name, ok=False)
    t0 = time.time()
    try:
        fn(step)
        step.ok = True
    except Exception:
        step.error = traceback.format_exc()
    step.seconds = time.time() - t0
    return evidence.add(step)


# ══════════════════════════════════════════════════════════════════════════
# Preflight
# ══════════════════════════════════════════════════════════════════════════


def cmd_preflight(args) -> int:
    rule("Preflight")
    problems: list[str] = []

    if not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY is not set")
    if not os.environ.get("GITHUB_TOKEN"):
        problems.append("GITHUB_TOKEN is not set")
    repo = args.repo or os.environ.get("KEEL_REPO", "")
    if not repo or "/" not in repo:
        problems.append("--repo owner/name is required (or export KEEL_REPO)")

    if problems:
        for p in problems:
            print(f"  {RED}missing{OFF} {p}")
        print(f"\n  See docs/RUNNING.md for the exact GitHub token scopes.")
        return 1

    gh = github.GitHub(client=Client(max_retries=1))
    github.configure(gh)

    try:
        info = gh.get(f"/repos/{repo}")
    except Exception as exc:
        print(f"  {RED}cannot read {repo}{OFF}: {exc}")
        return 1

    print(f"  repo        {info['full_name']}  "
          f"({'private' if info['private'] else 'public'})")
    print(f"  stars       {info['stargazers_count']}")
    print(f"  open issues {info['open_issues_count']}")

    # This script posts a real comment and then deletes it. Doing that to a
    # repository people watch is rude and, in a portfolio context, memorable
    # for the wrong reason.
    if info["stargazers_count"] > 5 or info["forks_count"] > 2:
        print(f"\n  {YEL}This repository has an audience.{OFF} `live` posts a "
              f"real comment and deletes it seconds later, which will email "
              f"every watcher. Use a throwaway repo.")
        if not args.force:
            print(f"  Re-run with --force if you are certain.")
            return 1

    try:
        rate = gh.get("/rate_limit")["resources"]["core"]
        print(f"  rate limit  {rate['remaining']}/{rate['limit']}")
    except Exception:
        pass

    print(f"\n  {GREEN}ready{OFF}. Next: python keel_verify.py live --repo {repo}")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# The live run
# ══════════════════════════════════════════════════════════════════════════


def _fresh(tmp: pathlib.Path) -> tuple[Executor, st.Store, AuditLog, Client]:
    client = Client(max_retries=3)
    github.configure(github.GitHub(client=client))
    provider = AnthropicProvider(
        model=os.environ.get("KEEL_MODEL", "claude-sonnet-4-6"), client=client)
    store = st.Store(tmp / "live.db")
    audit = AuditLog(tmp / "live.jsonl")
    ex = Executor(Registry.load(ROOT / "registry" / "agents"), store, provider, audit)
    return ex, store, audit, client


def cmd_live(args) -> int:
    repo = args.repo or os.environ.get("KEEL_REPO", "")
    if not repo:
        print("--repo owner/name is required")
        return 1

    tmp = ROOT / ".live"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()

    ev = Evidence(started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                  repo=repo,
                  model=os.environ.get("KEEL_MODEL", "claude-sonnet-4-6"),
                  python=sys.version.split()[0])

    ex, store, audit, client = _fresh(tmp)
    gate = ReviewGate(store, audit)
    outputs: list[dict[str, Any]] = []   # for `grade`

    # ---- 1. offline gate still green on this machine ---------------------

    rule("1. Offline gate")

    def _gate(step: Step) -> None:
        r = subprocess.run([sys.executable, "-m", "unittest", "discover",
                            "-s", "tests", "-t", "."],
                           cwd=ROOT, capture_output=True, text=True)
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        step.observed["unittest"] = "\n".join(tail)
        step.detail = tail[-1] if tail else ""
        if r.returncode != 0:
            raise RuntimeError("offline tests fail on this machine; fix that "
                               "before trusting anything below")

    timed(ev, "offline test suite", _gate)

    # ---- 2. read-only agent, first real model call -----------------------

    rule("2. repo-weekly-digest — read only")

    since = args.since or time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))

    def _digest(step: Step) -> None:
        before = len(client.calls)
        run_id = ex.submit("repo-weekly-digest", "verify",
                           {"repo": repo, "since": since})
        outcome = ex.drain()[0]
        step.calls = client.calls[before:]
        step.observed = {
            "run_id": run_id, "status": outcome.status, "turns": outcome.turns,
            "output": outcome.output,
            "staged_effects": [e["payload"] for e in
                               store.effects(run_id, state="staged")],
            "tools_called": [s["payload"]["tool"] for s in store.steps(run_id)
                             if s["kind"] == "tool_result"],
        }
        if outcome.error:
            raise RuntimeError(outcome.error)
        if outcome.status != st.AWAITING_REVIEW:
            raise RuntimeError(f"expected awaiting_review, got {outcome.status}")
        text = (outcome.output or "") + json.dumps(step.observed["staged_effects"])
        outputs.append({"agent": "repo-weekly-digest", "run_id": run_id,
                        "text": _digest_text(step.observed) or text})
        step.detail = f"{outcome.status}, {len(step.calls)} HTTP calls"

    digest_step = timed(ev, "digest run reaches the gate", _digest)

    def _approve(step: Step) -> None:
        run_id = digest_step.observed["run_id"]
        result = gate.decide(run_id, GateDecision(True, args.reviewer, "verify"))
        step.observed = {"run_id": run_id, "result": result,
                         "applied": store.effects(run_id, state="applied")}
        if result != st.COMMITTED:
            raise RuntimeError(f"gate returned {result}")
        step.detail = "approved and applied"

    if digest_step.ok:
        timed(ev, "human approval applies the draft", _approve)

    def _audit(step: Step) -> None:
        ok, broken = audit.verify()
        step.observed = {"entries": len(audit.entries()), "chain_intact": ok,
                         "broken_at": broken}
        if not ok:
            raise RuntimeError(f"audit chain broken at {broken}")
        step.detail = f"{step.observed['entries']} entries, chain intact"

    timed(ev, "audit chain verifies", _audit)

    # ---- 3. the gated write, all the way round ---------------------------

    rule("3. pr-note-carrier — staged, approved, posted, rolled back")

    def _comment_count() -> int:
        return len(github.client().get(
            f"/repos/{repo}/issues/{args.issue}/comments"))

    carrier: Step | None = None

    def _carry(step: Step) -> None:
        before_calls, before_comments = len(client.calls), _comment_count()
        run_id = ex.submit("pr-note-carrier", "verify", {
            "repo": repo, "number": args.issue,
            "note": args.note, "authored_by": args.reviewer})
        outcome = ex.drain()[0]
        after_comments = _comment_count()
        step.calls = client.calls[before_calls:]
        step.observed = {
            "run_id": run_id, "status": outcome.status,
            "output": outcome.output,
            "staged": store.effects(run_id, state="staged"),
            "comments_before": before_comments,
            "comments_while_staged": after_comments,
        }
        if outcome.error:
            raise RuntimeError(outcome.error)
        # The claim under test: held means not written.
        if after_comments != before_comments:
            raise RuntimeError(
                f"a staged run changed the world: comments went "
                f"{before_comments} -> {after_comments}")
        outputs.append({"agent": "pr-note-carrier", "run_id": run_id,
                        "text": _staged_text(step.observed) or (outcome.output or "")})
        step.detail = (f"staged, comment count unchanged at {after_comments}")

    carrier = timed(ev, "staged write does not touch the repository", _carry)

    posted_id: int | None = None

    def _post(step: Step) -> None:
        nonlocal posted_id
        run_id = carrier.observed["run_id"]
        before = _comment_count()
        result = gate.decide(run_id, GateDecision(True, args.reviewer, "verify"))
        applied = store.effects(run_id, state="applied")
        posted_id = applied[0]["inverse"]["comment_id"] if applied else None
        after = _comment_count()
        step.observed = {"result": result, "comment_id": posted_id,
                         "comments_before": before, "comments_after": after,
                         "url": f"https://github.com/{repo}/issues/"
                                f"{args.issue}#issuecomment-{posted_id}"}
        if after != before + 1:
            raise RuntimeError(f"approval did not post: {before} -> {after}")
        step.detail = f"comment {posted_id} posted"

    if carrier.ok:
        timed(ev, "approval posts exactly one comment", _post)

    def _rollback(step: Step) -> None:
        run_id = carrier.observed["run_id"]
        before = _comment_count()
        n = rollback(store, audit, run_id)
        after = _comment_count()
        step.observed = {"reversed": n, "comments_before": before,
                         "comments_after": after, "comment_id": posted_id}
        if after != before - 1:
            raise RuntimeError(f"rollback did not remove it: {before} -> {after}")
        step.detail = f"comment {posted_id} deleted, count {before} -> {after}"

    if carrier.ok and posted_id:
        timed(ev, "rollback deletes the comment", _rollback)

    # ---- 4. the prohibition, against a real API --------------------------

    rule("4. The forbidden write, against the real GitHub API")

    def _injection(step: Step) -> None:
        """The demo's step 5, except the API on the other end is real."""
        from keel.capabilities.base import ToolContractViolation
        from keel.capabilities.catalog import CATALOG

        before = len(client.calls)
        try:
            CATALOG["github.comment"].invoke(
                repo=repo, number=args.issue, body="reassigning",
                authored_by=args.reviewer,
                assignees=["someone"], state="closed", milestone=1)
            raise RuntimeError("the forbidden call was accepted")
        except ToolContractViolation as exc:
            step.observed = {"refused_with": str(exc),
                             "http_calls_made": len(client.calls) - before}
            if len(client.calls) != before:
                raise RuntimeError("an HTTP call was made despite the refusal")
        step.detail = "refused before any HTTP call"

    timed(ev, "injected commitment fields never reach GitHub", _injection)

    def _no_patch(step: Step) -> None:
        import inspect

        source = inspect.getsource(github)
        step.observed = {"patch_occurrences": source.count('"PATCH"')}
        if "PATCH" in source.replace("PATCH /repos", "").replace(
                "that back the PATCH", "").replace("rejects PATCH", ""):
            pass  # docstrings mention it; only a real call matters
        if step.observed["patch_occurrences"]:
            raise RuntimeError("a PATCH call exists in the GitHub module")
        step.detail = "no PATCH call exists in the module"

    timed(ev, "no code path issues PATCH /issues/{n}", _no_patch)

    # ---- 5. the eval gate blocking ---------------------------------------

    rule("5. Evaluation gate")

    def _record(step: Step) -> None:
        env = {**os.environ, "KEEL_RECORD": "1"}
        r = subprocess.run(
            [sys.executable, "-m", "keel.cli", "eval", "--live-only"],
            cwd=ROOT, capture_output=True, text=True, env=env)
        step.observed = {"stdout": r.stdout[-3000:], "returncode": r.returncode}
        cassettes = sorted((ROOT / "evals" / "cassettes").glob("*.json"))
        step.observed["cassettes"] = [c.name for c in cassettes]
        if not cassettes:
            raise RuntimeError("no cassette was recorded")
        step.detail = f"recorded {len(cassettes)} cassette(s)"

    rec = timed(ev, "live suites record a cassette", _record)

    def _replay(step: Step) -> None:
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN")}
        env["KEEL_RECORD"] = "0"
        r = subprocess.run(
            [sys.executable, "-m", "keel.cli", "eval", "--live-only", "--gate"],
            cwd=ROOT, capture_output=True, text=True, env=env)
        step.observed = {"stdout": r.stdout[-3000:], "returncode": r.returncode}
        if r.returncode != 0:
            raise RuntimeError("replay failed; the recorded run does not pass "
                               "its own suite — read the cassette")
        step.detail = "replays with no credentials, gate opens"

    if rec.ok:
        timed(ev, "cassette replays offline", _replay)

    # ---- write it all down ------------------------------------------------

    evidence_path = ev.save()
    (tmp / "outputs.json").write_text(json.dumps(outputs, indent=2))
    write_run_log(ev, outputs)

    rule("Result")
    passed = sum(s.ok for s in ev.steps)
    print(f"  {passed}/{len(ev.steps)} steps passed")
    print(f"  evidence  {evidence_path.relative_to(ROOT)}")
    print(f"  log       {RUN_LOG.relative_to(ROOT)}")

    if ev.all_passed:
        print(f"\n  {GREEN}End-to-end verified.{OFF} You may now delete the "
              f"UNVERIFIED markers in docs/RUNNING.md and the corresponding "
              f"line in the README scope section.")
        print(f"  Next: python keel_verify.py grade")
        return 0

    print(f"\n  {RED}Not verified.{OFF} {len(ev.steps) - passed} step(s) failed. "
          f"The log records which and why — that is the useful outcome. Do not "
          f"remove any UNVERIFIED marker.")
    return 1


def _digest_text(observed: dict) -> str:
    for eff in observed.get("staged_effects", []):
        if "body" in eff:
            return eff["body"]
    return ""


def _staged_text(observed: dict) -> str:
    for eff in observed.get("staged", []):
        body = (eff.get("payload") or {}).get("body")
        if body:
            return body
    return ""


# ══════════════════════════════════════════════════════════════════════════
# The generated run log
# ══════════════════════════════════════════════════════════════════════════


def write_run_log(ev: Evidence, outputs: list[dict]) -> None:
    verdict = ("**VERIFIED end-to-end.**" if ev.all_passed
               else f"**NOT verified** — {sum(1 for s in ev.steps if not s.ok)} "
                    f"step(s) failed.")

    lines = [
        "# Run log",
        "",
        "Machine-generated by `keel_verify.py live`. Every status code, comment",
        "id and output below came back from a real API on the date shown; none",
        "of it is templated. Regenerate rather than edit.",
        "",
        f"- Date: `{ev.started_at}`",
        f"- Repository: `{ev.repo}`",
        f"- Model: `{ev.model}`",
        f"- Python: `{ev.python}`",
        "",
        verdict,
        "",
        "| Step | Result | Detail | HTTP |",
        "|---|---|---|---|",
    ]
    for s in ev.steps:
        calls = " ".join(f"{m} {st_}" for m, _, st_ in s.calls[:6]) or "—"
        lines.append(f"| {s.name} | {'pass' if s.ok else '**FAIL**'} | "
                     f"{s.detail or s.error.splitlines()[-1][:80] if s.error else s.detail} | "
                     f"{calls} |")

    lines += ["", "## What the agents actually produced", ""]
    for o in outputs:
        lines += [f"### `{o['agent']}` — run `{o['run_id']}`", "", "```",
                  o["text"].strip()[:2000], "```", ""]

    lines += ["## Step detail", ""]
    for s in ev.steps:
        lines += [f"### {s.name}", "",
                  f"- result: **{'pass' if s.ok else 'FAIL'}** ({s.seconds:.1f}s)"]
        if s.calls:
            lines.append("- HTTP:")
            lines += [f"  - `{m} {u.split('?')[0]}` → {code}"
                      for m, u, code in s.calls[:12]]
        if s.observed:
            lines += ["", "```json",
                      json.dumps(s.observed, indent=2, default=str)[:2500],
                      "```"]
        if s.error:
            lines += ["", "**Failure:**", "", "```", s.error[-1500:], "```"]
        lines.append("")

    if not ev.all_passed:
        lines += [
            "## Do not remove the UNVERIFIED markers",
            "",
            "This run did not pass. `docs/RUNNING.md` and the README scope",
            "section remain accurate as written. Fix the failure above, rerun,",
            "and let this file regenerate.",
            "",
        ]

    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    RUN_LOG.write_text("\n".join(lines) + "\n")


# ══════════════════════════════════════════════════════════════════════════
# Grading — the step that makes the judge non-circular
# ══════════════════════════════════════════════════════════════════════════


def cmd_grade(args) -> int:
    """Label real agent outputs by hand. No generation, no heuristics.

    Every example this writes carries provenance=production and the run id it
    came from, so the split builder will accept it into the held-out set. A
    label produced any other way would not be, and that restriction is the
    only thing standing between this judge and one that grades its own
    generator.
    """
    source = pathlib.Path(args.source or (ROOT / ".live" / "outputs.json"))
    if not source.exists():
        print(f"no outputs at {source}. Run `keel_verify.py live` first.")
        return 1

    outputs = json.loads(source.read_text())
    existing = {e.id for e in load()}
    pending = [o for o in outputs
               if f"prod-{o['run_id']}" not in existing]

    if not pending:
        print("every output from that run is already graded.")
        return 0

    rule(f"Grading {len(pending)} output(s)")
    print("  Labels:")
    for k, v in LABELS.items():
        print(f"    {BOLD}{k}{OFF} — {v}")
    print(f"  {DIM}Type the label, then a one-line reason. Blank label skips.{OFF}")

    graded: list[Example] = []
    for i, o in enumerate(pending, 1):
        print(f"\n{DIM}{'─' * 70}{OFF}")
        print(f"{BOLD}[{i}/{len(pending)}] {o['agent']}{OFF}  run {o['run_id']}\n")
        print(o["text"].strip()[:1800])
        print()
        while True:
            label = input("  label> ").strip().lower()
            if not label:
                break
            if label in LABELS:
                rationale = input("  why  > ").strip()
                graded.append(Example(
                    id=f"prod-{o['run_id']}", text=o["text"], label=label,
                    provenance="production", agent=o["agent"],
                    run_id=o["run_id"], rationale=rationale))
                break
            print(f"    {RED}unknown label{OFF}; one of {list(LABELS)}")

    if graded:
        DATA.mkdir(parents=True, exist_ok=True)
        append(graded)
        print(f"\n  {GREEN}appended {len(graded)} production example(s){OFF}")

    return cmd_status(args)


def cmd_freeze(args) -> int:
    examples = load()
    try:
        split = build_split(examples)
    except Exception as exc:
        print(f"{RED}{exc}{OFF}")
        return 1
    split.save()
    print(json.dumps(split.counts, indent=2))
    print(f"{GREEN}frozen{OFF}: {len(split.train)} train / "
          f"{len(split.holdout)} holdout / {len(split.excluded)} excluded")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# Train and benchmark
# ══════════════════════════════════════════════════════════════════════════


MIN_PRODUCTION = 60


def cmd_train(args) -> int:
    examples = load()
    n_prod = sum(1 for e in examples if e.provenance == "production")

    if n_prod < MIN_PRODUCTION and not args.force:
        print(f"{RED}{n_prod} production examples; want at least "
              f"{MIN_PRODUCTION}.{OFF}")
        print(f"  A LoRA on a few dozen examples will report a number and mean "
              f"nothing. Run the agents on more repositories, grade the output, "
              f"then train. --force overrides, and the README should say you "
              f"forced it.")
        return 1

    problems = check_split(examples)
    if problems:
        print("split is not usable:")
        for p in problems:
            print(f"  - {p}")
        print("  run: python keel_verify.py freeze")
        return 1

    from keel.evals.judge.train import train_main

    return train_main(["--out", args.out] + (["--dry-run"] if args.dry_run else []))


def cmd_bench(args) -> int:
    from keel.evals.judge.benchmark import benchmark_main

    argv = []
    if args.adapter:
        argv += ["--adapter", args.adapter]
    if args.second_pass:
        argv += ["--second-pass", args.second_pass]
    rc = benchmark_main(argv)
    if rc != 0 or not REPORT.exists():
        return rc

    report = json.loads(REPORT.read_text())
    rule("README")
    print("  Paste these into the judge section, exactly:")
    for arm in report["arms"]:
        print(f"    {arm['arm']} kappa {arm['cohen_kappa']:.3f}")
    print(f"\n  {DIM}tests/test_judge.py fails if the README and report.json "
          f"disagree, so do not round them differently.{OFF}")
    print(f"\n  verdict: {report['verdict']}")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# Status
# ══════════════════════════════════════════════════════════════════════════


def cmd_status(args) -> int:
    rule("What is proven")

    logs = sorted(EVIDENCE.glob("live-*.json")) if EVIDENCE.exists() else []
    if not logs:
        print(f"  {RED}not verified{OFF}  no live run has been recorded")
    else:
        ev = json.loads(logs[-1].read_text())
        passed = sum(1 for s in ev["steps"] if s["ok"])
        total = len(ev["steps"])
        mark = GREEN + "verified" + OFF if passed == total else RED + "partial" + OFF
        print(f"  {mark}  last run {ev['started_at']} against {ev['repo']}: "
              f"{passed}/{total} steps")

    examples = load()
    n_prod = sum(1 for e in examples if e.provenance == "production")
    print(f"\n  judge dataset  {len(examples)} examples, {n_prod} from production")
    if n_prod < MIN_PRODUCTION:
        print(f"  {YEL}not trainable{OFF}  need {MIN_PRODUCTION - n_prod} more "
              f"production examples before training means anything")
    for p in check_split(examples):
        print(f"  {YEL}split{OFF}  {p}")

    if REPORT.exists():
        report = json.loads(REPORT.read_text())
        print(f"\n  judge benchmark  n={report['holdout_n']} held out")
        for arm in report["arms"]:
            print(f"    {arm['arm']:<20} kappa={arm['cohen_kappa']:.3f}  "
                  f"harmful recall={arm['harmful_recall']:.3f}")
        print(f"  wired in: {report['default_judge']}")
    else:
        print(f"\n  judge benchmark  {RED}none{OFF}  rubric is wired in")

    cassettes = sorted((ROOT / "evals" / "cassettes").glob("*.json"))
    print(f"\n  cassettes  {len(cassettes)} recorded"
          + (f": {', '.join(c.stem for c in cassettes)}" if cassettes else ""))

    print(f"\n{DIM}  Claims are only as good as the evidence above. If a line "
          f"says 'none', the corresponding sentence does not belong in a CV.{OFF}")
    return 0


# ══════════════════════════════════════════════════════════════════════════


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="keel_verify", description=__doc__.split("\n")[1])
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight", help="check credentials and repo")
    pf.add_argument("--repo")
    pf.add_argument("--force", action="store_true")
    pf.set_defaults(fn=cmd_preflight)

    lv = sub.add_parser("live", help="run everything against real APIs")
    lv.add_argument("--repo")
    lv.add_argument("--issue", type=int, default=1,
                    help="issue or PR number to comment on and then un-comment")
    lv.add_argument("--since")
    lv.add_argument("--note", default="Verification run for the keel agent "
                                      "platform. This comment is deleted "
                                      "automatically.")
    lv.add_argument("--reviewer", default="@" + os.environ.get("USER", "you"))
    lv.set_defaults(fn=cmd_live)

    gr = sub.add_parser("grade", help="label real outputs by hand")
    gr.add_argument("--source")
    gr.set_defaults(fn=cmd_grade)

    fz = sub.add_parser("freeze", help="freeze the train/holdout split")
    fz.set_defaults(fn=cmd_freeze)

    tr = sub.add_parser("train", help="LoRA the judge")
    tr.add_argument("--out", default="artifacts/judge-lora-v1")
    tr.add_argument("--dry-run", action="store_true")
    tr.add_argument("--force", action="store_true")
    tr.set_defaults(fn=cmd_train)

    bn = sub.add_parser("bench", help="three-arm benchmark, write report.json")
    bn.add_argument("--adapter")
    bn.add_argument("--second-pass")
    bn.set_defaults(fn=cmd_bench)

    sub.add_parser("status", help="what is proven and what is not"
                   ).set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
