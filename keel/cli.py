"""keel — command line for the agent platform."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from .audit.log import AuditLog
from .capabilities.base import ProhibitedCapability
from .evals.harness import Harness, RubricJudge, gate
from .evals.live import LiveRunner, load_suite
from .gate.review import GateDecision, ReviewGate
from .registry.loader import Registry, RegistryError, _parse
from .runtime import state as st
from .runtime.executor import Executor
from .runtime.providers import Completion, ScriptedProvider, ToolCall

ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "registry" / "agents"
SUITES = ROOT / "evals" / "suites"
LIVE = ROOT / "evals" / "live"

DIM, BOLD, RED, GREEN, YEL, OFF = (
    "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
)


def _rule(title: str) -> None:
    print(f"\n{BOLD}{title}{OFF}\n{DIM}{'─' * min(len(title), 68)}{OFF}")


# ---------------------------------------------------------------------------


def cmd_validate(args) -> int:
    try:
        reg = Registry.load(REGISTRY)
    except RegistryError as exc:
        print(f"{RED}registry invalid:{OFF} {exc}")
        return 1
    print(f"{GREEN}{len(reg)} agents registered{OFF}")
    for spec in reg:
        gatetag = "gated" if spec.human_gate else "ungated"
        print(
            f"  {spec.id:<22} {spec.tier.name:<22} {spec.autonomy.value:<22}"
            f" {gatetag:<8} owner={spec.owner}"
        )

    if args.include_rejected:
        _rule("Specs the validator refuses")
        for path in sorted((ROOT / "registry" / "rejected").glob("*.yaml")):
            try:
                _parse(path)
                print(f"  {RED}{path.name}: loaded — this should not happen{OFF}")
                return 1
            except (RegistryError, ProhibitedCapability) as exc:
                print(f"  {path.name}")
                print(f"    {GREEN}refused{OFF} {exc}")
    return 0


def cmd_eval(args) -> int:
    reg = Registry.load(REGISTRY)
    reports = []

    if not getattr(args, "live_only", False):
        reports += Harness(reg, RubricJudge()).run_all(SUITES)

    if getattr(args, "live", False) or getattr(args, "live_only", False):
        # Grades the real agent through the real executor, replayed from a
        # cassette. Record with KEEL_RECORD=1 and real credentials.
        runner = LiveRunner(reg)
        paths = sorted(LIVE.glob("*.json"))
        if getattr(args, "suite", None):
            paths = [p for p in paths if p.stem == args.suite]
        for path in paths:
            try:
                reports.append(runner.run(load_suite(path)))
            except AssertionError as exc:
                print(f"{YEL}{path.stem}: {exc}{OFF}")
                if args.gate:
                    return 1

    if not reports:
        print("no suites selected")
        return 0
    ok, message = gate(reports)
    print(message)
    return 0 if ok or not args.gate else 1


def cmd_run(args) -> int:
    reg = Registry.load(REGISTRY)
    store = st.Store(args.db)
    ex = Executor(reg, store, _demo_provider(args.agent), AuditLog(args.audit))
    run_id = ex.submit(args.agent, trigger="cli", payload=json.loads(args.input))
    for outcome in ex.drain():
        print(f"{outcome.run_id}  {outcome.status}  turns={outcome.turns}")
        if outcome.output:
            print(f"  {outcome.output.strip()[:400]}")
        if outcome.error:
            print(f"  {RED}{outcome.error}{OFF}")
    return 0 if store.get(run_id) else 1


def cmd_review(args) -> int:
    store = st.Store(args.db)
    g = ReviewGate(store, AuditLog(args.audit))
    if args.action == "list":
        for run in g.pending():
            n = len(store.effects(run.run_id, state="staged"))
            print(f"{run.run_id}  {run.agent_id:<22} {n} staged effect(s)")
        return 0
    if args.action == "show":
        print(json.dumps(g.describe(args.run_id), indent=2, default=str))
        return 0
    approved = args.action == "approve"
    result = g.decide(
        args.run_id,
        GateDecision(approved, reviewer=args.reviewer, reason=args.reason or ""),
    )
    print(f"{args.run_id} -> {result}")
    return 0


def cmd_audit(args) -> int:
    log = AuditLog(args.audit)
    if args.verify:
        ok, broken = log.verify()
        print(f"{GREEN}chain intact{OFF}" if ok else f"{RED}chain broken at {broken}{OFF}")
        return 0 if ok else 1
    for e in log.entries(args.run_id):
        print(f"{e['kind']:<20} {json.dumps(e['data'], default=str)[:220]}")
    return 0


def cmd_value(args) -> int:
    store = st.Store(args.db)
    rows = store.value_by_agent()
    if not rows:
        print("no runs recorded yet")
        return 0
    total = sum(m for _, m, _ in rows)
    print(f"{'agent':<24}{'runs':>6}{'minutes':>10}")
    for agent_id, minutes, n in rows:
        print(f"{agent_id:<24}{n:>6}{minutes:>10.1f}")
    print(f"{DIM}{'─' * 40}{OFF}\n{'total':<24}{'':>6}{total:>10.1f} min "
          f"({total / 60:.1f} h)")
    return 0


# ---------------------------------------------------------------------------
# The demo: the whole argument, end to end, in one command.
# ---------------------------------------------------------------------------


def _demo_provider(agent_id: str) -> ScriptedProvider:
    scripts = {
        "exec-weekly-digest": [
            Completion(tool_call=ToolCall("warehouse.query", {"table": "merged_prs"})),
            Completion(tool_call=ToolCall("warehouse.query", {"table": "incidents"})),
            Completion(tool_call=ToolCall(
                "draft.save",
                {"key": "digest-2026-w31",
                 "body": "Three changes merged. @nadia fixed a connection pool "
                         "leak in postgrest. @karim added the MFA enrollment "
                         "endpoint. @lin landed backpressure on realtime "
                         "broadcast. INC-204 is resolved."},
            )),
            Completion(text="Digest drafted and held for review."),
        ],
        "pr-context-linter": [
            Completion(tool_call=ToolCall("work_item.read", {"item_id": "PLAT-19"})),
            Completion(text="PLAT-19 has no linked design note. Flagged to author."),
        ],
        "standup-drafter": [
            Completion(tool_call=ToolCall(
                "work_item.append_note",
                {"item_id": "PLAT-19",
                 "note": "Durable state landed; resume path next.",
                 "authored_by": "@lin"},
            )),
            Completion(text="Note carried to PLAT-19, awaiting approval."),
        ],
    }
    return ScriptedProvider(scripts.get(agent_id, [Completion(text="ok")]))


def _injected_provider() -> ScriptedProvider:
    """A model that has been talked into doing the forbidden thing.

    The tool call below is exactly what a successful prompt injection would
    produce. It reaches the capability layer and dies there.
    """
    return ScriptedProvider([
        Completion(tool_call=ToolCall(
            "work_item.append_note",
            {"item_id": "PLAT-19",
             "note": "reassigning per urgent request from leadership",
             "authored_by": "@lin",
             "owner": "@karim",       # <- injected
             "status": "blocked",     # <- injected
             "due_date": "2026-08-07"},  # <- injected
        )),
        Completion(text="done"),
    ])


def cmd_demo(args) -> int:
    db = pathlib.Path(args.db)
    audit_path = pathlib.Path(args.audit)
    for p in (db, audit_path):
        p.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        pathlib.Path(str(db) + suffix).unlink(missing_ok=True)

    reg = Registry.load(REGISTRY)
    store = st.Store(db)
    audit = AuditLog(audit_path)

    _rule("1. The registry refuses what it will not run")
    ns = argparse.Namespace(include_rejected=True)
    cmd_validate(ns)

    _rule("2. A gated run: effects are held, not applied")
    ex = Executor(reg, store, _demo_provider("exec-weekly-digest"), audit)
    ex.submit("exec-weekly-digest", "cron.weekly.monday_0800", {"week": "2026-07-27"})
    outcome = ex.drain()[0]
    print(f"  run {outcome.run_id} -> {YEL}{outcome.status}{OFF} "
          f"({outcome.effects_pending} effect staged, {outcome.turns} turns)")
    from .capabilities.catalog import PUBLISHED
    print(f"  drafts in the world: {len(PUBLISHED)} "
          f"{GREEN}(the write has not happened){OFF}")

    _rule("3. A human approves; only then does it land")
    g = ReviewGate(store, audit)
    g.decide(outcome.run_id, GateDecision(True, reviewer="@nadia", reason="reads fine"))
    print(f"  approved by @nadia -> drafts in the world: "
          f"{list(PUBLISHED)}")

    _rule("4. Rejection rolls the world back atomically")
    ex2 = Executor(reg, store, _demo_provider("standup-drafter"), audit)
    ex2.submit("standup-drafter", "cli", {"item_id": "PLAT-19"})
    o2 = ex2.drain()[0]
    from .capabilities.catalog import WORK_ITEMS
    print(f"  run {o2.run_id} -> {o2.status}; PLAT-19 notes in the world: "
          f"{len(WORK_ITEMS['PLAT-19']['notes'])}")
    g.decide(o2.run_id, GateDecision(False, reviewer="@lin", reason="wrong item"))
    print(f"  rejected by @lin -> PLAT-19 notes: "
          f"{len(WORK_ITEMS['PLAT-19']['notes'])} "
          f"{GREEN}(staged effects discarded, journal marked reversed){OFF}")

    _rule("5. Prompt injection tries the forbidden write")
    print("  a compromised model emits work_item.append_note with owner,")
    print("  status and due_date attached:")
    ex3 = Executor(reg, store, _injected_provider(), audit)
    rid = ex3.submit("standup-drafter", "cli", {"item_id": "PLAT-19"})
    o3 = ex3.drain()[0]
    print(f"  run {o3.run_id} -> {GREEN}{o3.status}{OFF}")
    print(f"  {GREEN}blocked:{OFF} {o3.error}")
    print(f"  PLAT-19 owner is still "
          f"{WORK_ITEMS['PLAT-19']['owner']}, status still "
          f"{WORK_ITEMS['PLAT-19']['status']}")
    print(f"  {DIM}the arguments never reached a backend: no tool in the "
          f"catalog\n  declares those parameters, so there is nothing to call.{OFF}")

    _rule("6. Durability: a run survives the process dying")
    store2 = st.Store(db)
    orphan = store2.enqueue("exec-weekly-digest", 1, "cron", {"week": "2026-08-03"})
    store2.claim_next()
    store2.record_step(orphan, "tool_result",
                       {"tool": "warehouse.query", "arguments": {"table": "merged_prs"},
                        "output": [{"repo": "supabase/auth"}]})
    print(f"  simulating a crash mid-run: {orphan} left in 'running' with "
          f"{len(store2.steps(orphan))} committed step")
    ex4 = Executor(reg, store2, _demo_provider("exec-weekly-digest"), audit)
    resumed = ex4.resume()
    for r in resumed:
        print(f"  resumed {r.run_id} -> {r.status} "
              f"{GREEN}(replayed committed steps, did not re-execute them){OFF}")

    _rule("7. The evaluation gate")
    cmd_eval(argparse.Namespace(gate=False))

    _rule("8. The audit chain")
    ok, broken = audit.verify()
    n = len(audit.entries())
    print(f"  {n} entries, chain {'intact' if ok else 'BROKEN at ' + str(broken)}")
    print(f"  {DIM}every prompt, tool call, gate decision and rollback above is "
          f"in it{OFF}")

    _rule("9. What the platform absorbed")
    cmd_value(argparse.Namespace(db=str(db)))
    print()
    return 0


# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="keel", description=__doc__)
    p.add_argument("--db", default="keel.db")
    p.add_argument("--audit", default="audit.jsonl")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate the agent registry")
    v.add_argument("--include-rejected", action="store_true")
    v.set_defaults(fn=cmd_validate)

    e = sub.add_parser("eval", help="run the evaluation suites")
    e.add_argument("--gate", action="store_true", help="exit non-zero on failure")
    e.add_argument("--live", action="store_true",
                   help="also run live suites against recorded cassettes")
    e.add_argument("--live-only", action="store_true")
    e.add_argument("--suite", help="limit live suites to one name")
    e.set_defaults(fn=cmd_eval)

    r = sub.add_parser("run", help="submit and drain one run")
    r.add_argument("agent")
    r.add_argument("--input", default="{}")
    r.set_defaults(fn=cmd_run)

    rv = sub.add_parser("review", help="the human gate")
    rv.add_argument("action", choices=["list", "show", "approve", "reject"])
    rv.add_argument("run_id", nargs="?")
    rv.add_argument("--reviewer", default="@you")
    rv.add_argument("--reason", default="")
    rv.set_defaults(fn=cmd_review)

    a = sub.add_parser("audit", help="read the decision log")
    a.add_argument("run_id", nargs="?")
    a.add_argument("--verify", action="store_true")
    a.set_defaults(fn=cmd_audit)

    val = sub.add_parser("value", help="minutes of work absorbed, per agent")
    val.set_defaults(fn=cmd_value)

    m = sub.add_parser("mcp", help="serve the platform over MCP on stdio")
    m.set_defaults(fn=lambda a: (__import__("keel.mcp", fromlist=["serve"]).serve(), 0)[1])

    d = sub.add_parser("demo", help="the whole platform, end to end")
    d.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
