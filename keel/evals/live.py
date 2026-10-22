"""Live evaluation: grade the agent, not a string somebody typed.

The previous harness asserted against ``scripted_output`` — text written by
hand and pasted into the suite. It tested the assertions. It did not test the
agent, and the difference is the whole point of an evaluation layer.

This runner drives the **real** executor, with the **real** provider, against
the **real** tools, and grades whatever comes out.

To keep that deterministic and free in CI, it uses record/replay:

    KEEL_RECORD=1 ANTHROPIC_API_KEY=... GITHUB_TOKEN=... \\
        python -m keel.cli eval --live --suite repo-weekly-digest

records every HTTP exchange — model calls and GitHub calls alike — into
``evals/cassettes/<suite>.json``. Every later run replays the cassette. No key
needed, no network, byte-identical every time.

Why record/replay rather than calling the API in CI:

- A suite that hits a live model is non-deterministic, so a regression and a
  sampling artefact look identical, and people learn to re-run until green.
- It costs money per PR and needs a production key in CI.
- It fails when the network does, which trains everyone to ignore it.

What it costs: a cassette can go stale. So the suite records the model and the
prompt hash it was captured with, and ``--check-stale`` fails when the agent's
prompt or tool grants have changed since — the cassette is then re-recorded
deliberately rather than drifting silently.

Re-record on any prompt change, any tool-grant change, and on a schedule
(monthly is enough) so model drift is caught on purpose rather than in
production.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Any

from ..audit.log import AuditLog
from ..capabilities import github
from ..registry.loader import Registry
from ..runtime import state as st
from ..runtime.executor import Executor
from ..runtime.http import Client, RecordingTransport, ReplayTransport, UrllibTransport
from ..runtime.providers import AnthropicProvider
from .harness import ASSERTIONS, CaseResult, SuiteReport

CASSETTES = pathlib.Path(__file__).resolve().parent.parent.parent / "evals" / "cassettes"


@dataclass
class LiveCase:
    id: str
    agent: str
    input: dict[str, Any]
    assertions: list[dict[str, Any]] = field(default_factory=list)
    safety: bool = False
    expect: str = "pass"
    #: Assertions applied to the tool calls the agent made, not its prose.
    #: Behavioural checks on *actions* are stronger than checks on text.
    expect_tools: list[str] | None = None
    forbid_tools: list[str] = field(default_factory=list)


@dataclass
class LiveSuite:
    name: str
    threshold: float
    cases: list[LiveCase]


def prompt_fingerprint(registry: Registry, agent_id: str) -> str:
    """Hash of everything that determines what the model sees.

    If this changes, the cassette is describing a different agent and must be
    re-recorded. Cheap insurance against a suite that passes because it is
    replaying answers to questions nobody asks any more.
    """
    spec = registry.get(agent_id)
    material = json.dumps({
        "purpose": spec.purpose,
        "tier": spec.tier.name,
        "autonomy": spec.autonomy.value,
        "tools": sorted(spec.granted_tools),
    }, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


class LiveRunner:
    def __init__(self, registry: Registry, *, record: bool | None = None):
        self.registry = registry
        self.record = (os.environ.get("KEEL_RECORD") == "1"
                       if record is None else record)
        CASSETTES.mkdir(parents=True, exist_ok=True)

    def cassette_path(self, suite: str) -> pathlib.Path:
        return CASSETTES / f"{suite}.json"

    # -- one case -----------------------------------------------------------

    def run_case(self, case: LiveCase, tape: dict[str, Any]) -> tuple[str, list[str]]:
        """Execute one case. Returns (output_text, tool_names_called)."""
        key = case.id
        if self.record:
            transport = RecordingTransport(UrllibTransport())
        else:
            if key not in tape["cases"]:
                raise AssertionError(
                    f"no recorded exchanges for case {key!r}; re-record with "
                    f"KEEL_RECORD=1")
            transport = ReplayTransport(tape["cases"][key])

        github.configure(github.GitHub(
            token=os.environ.get("GITHUB_TOKEN", "replay"),
            client=Client(transport, max_retries=0)))
        provider = AnthropicProvider(
            model=os.environ.get("KEEL_MODEL", "claude-sonnet-4-6"),
            api_key=os.environ.get("ANTHROPIC_API_KEY", "replay"),
            client=Client(transport, max_retries=0))

        tmp = pathlib.Path(tempfile.mkdtemp())
        store, audit = st.Store(tmp / "eval.db"), AuditLog(tmp / "eval.jsonl")
        ex = Executor(self.registry, store, provider, audit)
        run_id = ex.submit(case.agent, "eval", case.input)
        outcome = ex.drain()[0]

        called = [s["payload"]["tool"] for s in store.steps(run_id)
                  if s["kind"] == "tool_result"]

        if self.record:
            tape["cases"][key] = transport.exchanges

        # Nothing an eval run stages ever reaches the world: the suite never
        # approves a gate. A T2 agent under test posts nothing.
        if outcome.status == st.AWAITING_REVIEW:
            staged = store.effects(run_id, state="staged")
            output = outcome.output or json.dumps(
                [e["payload"] for e in staged], default=str)
        else:
            output = outcome.output or ""
        return output, called

    # -- one suite ----------------------------------------------------------

    def run(self, suite: LiveSuite) -> SuiteReport:
        path = self.cassette_path(suite.name)
        if self.record:
            tape: dict[str, Any] = {"cases": {}, "fingerprints": {}}
        else:
            if not path.exists():
                raise AssertionError(
                    f"no cassette at {path}. Record one with KEEL_RECORD=1.")
            tape = json.loads(path.read_text())

        results: list[CaseResult] = []
        for case in suite.cases:
            fp = prompt_fingerprint(self.registry, case.agent)
            if self.record:
                tape["fingerprints"][case.id] = fp
            elif tape.get("fingerprints", {}).get(case.id) not in (fp, None):
                results.append(CaseResult(
                    case.id, False, case.safety,
                    [f"cassette is stale: the agent's prompt or grants changed "
                     f"since it was recorded. Re-record with KEEL_RECORD=1."]))
                continue

            output, called = self.run_case(case, tape)
            failures: list[str] = []

            for spec in case.assertions:
                fn = ASSERTIONS.get(spec["type"])
                if fn is None:
                    failures.append(f"unknown assertion type {spec['type']!r}")
                    continue
                err = fn(output, **{k: v for k, v in spec.items() if k != "type"})
                if err:
                    failures.append(f"{spec['type']}: {err}")

            # Behavioural assertions on actions. Stronger than prose checks:
            # they say what the agent *did*, not what it wrote about doing.
            for tool in case.forbid_tools:
                if tool in called:
                    failures.append(f"called forbidden tool {tool!r}")
            if case.expect_tools is not None:
                missing = set(case.expect_tools) - set(called)
                if missing:
                    failures.append(f"did not call expected tool(s) {sorted(missing)}")

            if case.expect == "caught":
                passed = bool(failures)
                detail = [] if passed else [
                    "expected at least one assertion to fire; none did, so the "
                    "detector is blind"]
            else:
                passed, detail = not failures, failures
            results.append(CaseResult(case.id, passed, case.safety, detail))

        if self.record:
            path.write_text(json.dumps(tape, indent=2))

        return SuiteReport(suite.name, suite.cases[0].agent if suite.cases else "",
                           results, suite.threshold)


def load_suite(path: str | pathlib.Path) -> LiveSuite:
    raw = json.loads(pathlib.Path(path).read_text())
    return LiveSuite(
        name=raw["suite"],
        threshold=float(raw.get("threshold", 1.0)),
        cases=[LiveCase(
            id=c["id"], agent=c["agent"], input=c.get("input", {}),
            assertions=c.get("assert", []), safety=bool(c.get("safety", False)),
            expect=c.get("expect", "pass"),
            expect_tools=c.get("expect_tools"),
            forbid_tools=c.get("forbid_tools", []),
        ) for c in raw["cases"]],
    )
