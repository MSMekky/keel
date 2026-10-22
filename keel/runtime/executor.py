"""The run loop.

Shape of a run:

    claim -> build capability set from grants -> loop(provider -> tool) ->
    hold effects -> gate (if required) -> commit or roll back

Two properties worth calling out.

Resume. Every step is committed to the store before the next one starts, so
a crash leaves a run whose completed steps are exactly its durable steps.
``resume`` replays those steps as context and continues from the next one; it
does not re-execute them, because re-executing a write is how you get two of
something.

The dispatch table. ``_HANDLERS`` maps autonomy class to the function that
runs it. ``AutonomyClass.AUTONOMOUS_COMMITMENT`` has no entry, deliberately.
A test asserts its absence. If someone adds one, the test fails, which is the
point: the prohibition lives in the code and is defended by CI, rather than
living in a document and being defended by memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..audit.log import AuditLog
from ..capabilities.base import (
    CapabilitySet,
    ProhibitedCapability,
    ToolContractViolation,
    ToolNotGranted,
)
from ..capabilities.catalog import CATALOG, apply_effect
from ..gate.review import InterruptionBudget, rollback
from ..registry.loader import Registry
from ..registry.models import AgentSpec, AutonomyClass
from . import state as st
from .value import minutes_for
from .providers import Completion, Provider

MAX_TURNS = 12


@dataclass
class RunOutcome:
    run_id: str
    status: str
    output: str
    turns: int
    effects_pending: int = 0
    error: str | None = None


class Executor:
    def __init__(
        self,
        registry: Registry,
        store: st.Store,
        provider: Provider,
        audit: AuditLog | None = None,
    ):
        self.registry = registry
        self.store = store
        self.provider = provider
        self.audit = audit or AuditLog()
        self.budget = InterruptionBudget(store)

    # ------------------------------------------------------------------
    # Capability binding
    # ------------------------------------------------------------------

    def capabilities_for(self, spec: AgentSpec) -> CapabilitySet:
        """Bind exactly the granted tools. Nothing ambient, no fallback."""
        tools = {}
        budgets = {}
        for grant in spec.grants:
            tool = CATALOG[grant.tool]
            if tool.min_tier > spec.tier:
                raise ProhibitedCapability(
                    f"{spec.id}: tool {tool.name} exceeds tier {spec.tier.name}"
                )
            tools[grant.tool] = tool
            budgets[grant.tool] = grant.rate_per_run
        return CapabilitySet(agent_id=spec.id, tools=tools, budgets=budgets)

    # ------------------------------------------------------------------
    # Dispatch by autonomy class. Note the missing key.
    # ------------------------------------------------------------------

    @property
    def _handlers(self) -> dict[AutonomyClass, Callable[..., RunOutcome]]:
        return {
            AutonomyClass.READ_ONLY: self._run_loop,
            AutonomyClass.DRAFT_FOR_HUMAN: self._run_loop,
            AutonomyClass.CARRY_HUMAN_AUTHORED: self._run_loop,
            AutonomyClass.INITIATE_CONTACT: self._run_loop,
            # AutonomyClass.AUTONOMOUS_COMMITMENT: intentionally absent.
            # See tests/test_prohibited_capability.py.
        }

    # ------------------------------------------------------------------

    def submit(self, agent_id: str, trigger: str, payload: dict) -> str:
        spec = self.registry.get(agent_id)
        run_id = self.store.enqueue(spec.id, int(spec.tier), trigger, payload)
        self.audit.append(
            run_id,
            "run.enqueued",
            {"agent": spec.id, "trigger": trigger, "tier": spec.tier.name,
             "owner": spec.owner, "input": payload},
        )
        return run_id

    def drain(self, limit: int = 50) -> list[RunOutcome]:
        out = []
        while len(out) < limit:
            run = self.store.claim_next()
            if run is None:
                break
            out.append(self.execute(run))
        return out

    def resume(self) -> list[RunOutcome]:
        """Pick up runs that were in flight when the process stopped."""
        out = []
        for run in self.store.resume_candidates():
            done = self.store.steps(run.run_id)
            self.audit.append(
                run.run_id, "run.resumed", {"committed_steps": len(done)}
            )
            out.append(self.execute(run, resumed_from=len(done)))
        return out

    def execute(self, run: st.Run, resumed_from: int = 0) -> RunOutcome:
        spec = self.registry.get(run.agent_id)
        handler = self._handlers.get(spec.autonomy)
        if handler is None:
            # Unreachable via the loader, which rejects prohibited classes.
            # Kept as the second wall, and as the thing an audit points at.
            self.store.set_status(run.run_id, st.FAILED, "no handler for autonomy")
            self.audit.append(
                run.run_id,
                "dispatch.refused",
                {"autonomy": spec.autonomy.value,
                 "reason": "no handler exists for this autonomy class"},
            )
            raise ProhibitedCapability(
                f"no handler for autonomy class {spec.autonomy.value!r}"
            )
        try:
            return handler(run, spec, resumed_from=resumed_from)
        except (ToolNotGranted, ToolContractViolation, ProhibitedCapability) as exc:
            self.audit.append(
                run.run_id, "run.blocked",
                {"error": type(exc).__name__, "detail": str(exc)},
            )
            rollback(self.store, self.audit, run.run_id)
            self.store.set_status(run.run_id, st.ROLLED_BACK, str(exc))
            return RunOutcome(run.run_id, st.ROLLED_BACK, "", 0, error=str(exc))

    # ------------------------------------------------------------------

    def _run_loop(
        self, run: st.Run, spec: AgentSpec, resumed_from: int = 0
    ) -> RunOutcome:
        caps = self.capabilities_for(spec)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(spec, caps)},
            {"role": "user", "content": json.dumps(run.input)},
        ]

        # Replay committed steps as context rather than re-executing them.
        for step in self.store.steps(run.run_id):
            if step["kind"] == "tool_result":
                messages.append({"role": "tool", "tool_use_id": None,
                                 "content": json.dumps(step["payload"])})
                caps.used[step["payload"]["tool"]] = (
                    caps.used.get(step["payload"]["tool"], 0) + 1
                )

        output, turns = "", 0
        for turn in range(MAX_TURNS):
            turns = turn + 1
            completion: Completion = self.provider.complete(
                messages, list(caps.tools.values()))
            self.store.record_step(
                run.run_id,
                "model_turn",
                {"text": completion.text,
                 "tool_call": completion.tool_call.__dict__ if completion.tool_call else None,
                 "model": completion.model},
            )
            self.audit.append(
                run.run_id,
                "model.completion",
                {"model": completion.model,
                 "input_tokens": completion.input_tokens,
                 "output_tokens": completion.output_tokens,
                 "stop_reason": completion.stop_reason,
                 "prompt": completion.prompt_digest,
                 "text": completion.text,
                 "tool_call": completion.tool_call.__dict__ if completion.tool_call else None},
            )

            if completion.tool_call is None:
                output = completion.text or ""
                break

            call = completion.tool_call
            # The assistant's tool_use turn has to appear in the transcript, or
            # the next request has a tool_result with nothing to attach to.
            messages.append({"role": "assistant", "content": completion.text,
                             "tool_call": {"name": call.name,
                                           "arguments": call.arguments,
                                           "call_id": call.call_id}})
            self.audit.append(
                run.run_id, "tool.call",
                {"tool": call.name, "arguments": call.arguments},
            )

            if call.name == "notify.send":
                ok, reason = self.budget.can_contact(
                    call.arguments.get("recipient", ""), spec.contact
                )
                if not ok:
                    self.audit.append(
                        run.run_id, "contact.deferred", {"reason": reason}
                    )
                    messages.append({"role": "tool", "tool_use_id": call.call_id,
                                     "content": f"deferred: {reason}"})
                    continue

            result = caps.dispatch(call.name, **call.arguments)

            seq = self.store.record_step(
                run.run_id,
                "tool_result",
                {"tool": call.name, "arguments": call.arguments,
                 "output": result.output},
            )
            # Gated agents stage; ungated agents apply now. Either way the
            # journal records the intent before anything touches the world,
            # so a crash between the two leaves a recoverable trail.
            for eff in result.effects:
                self.store.record_effect(
                    run.run_id, seq, eff.kind, eff.target, dict(eff.payload),
                    inverse=None, state="staged",
                )
                if not spec.human_gate:
                    inverse = apply_effect(eff.kind, dict(eff.payload))
                    self.store.set_effect_state(
                        run.run_id, seq, "applied", inverse=inverse
                    )
            if call.name == "notify.send":
                self.budget.spend(
                    call.arguments["recipient"], run.run_id,
                    call.arguments.get("subject", ""),
                )
            self.audit.append(
                run.run_id, "tool.result",
                {"tool": call.name, "output": result.output,
                 "effects": [e.kind for e in result.effects]},
            )
            messages.append({"role": "tool", "tool_use_id": call.call_id,
                             "content": json.dumps(result.output, default=str)})

        staged = self.store.effects(run.run_id, state="staged")
        needs_gate = spec.human_gate and bool(staged)

        if needs_gate:
            self.store.set_status(run.run_id, st.AWAITING_REVIEW)
            self.audit.append(
                run.run_id, "gate.held",
                {"effects": len(staged), "reviewer_hint": spec.owner,
                 "applied": False},
            )
            return RunOutcome(
                run.run_id, st.AWAITING_REVIEW, output, turns, len(staged)
            )

        self.store.set_status(run.run_id, st.COMMITTED)
        self.store.record_value(
            run.run_id, spec.id,
            minutes_saved=minutes_for(spec.tier, turns),
            basis="registered per-tier displacement baseline",
        )
        self.audit.append(run.run_id, "run.committed", {"turns": turns})
        return RunOutcome(run.run_id, st.COMMITTED, output, turns)


def _system_prompt(spec: AgentSpec, caps: CapabilitySet) -> str:
    return (
        f"You are {spec.id}, owned by {spec.owner}.\n"
        f"Purpose: {spec.purpose}\n"
        f"Risk tier: {spec.tier.name}. Autonomy: {spec.autonomy.value}.\n"
        f"Tools available to you: {', '.join(caps.names()) or 'none'}.\n"
        f"You cannot set an owner, a due date or a status on any work item. "
        f"This is not a rule you are being asked to follow — no such tool "
        f"exists in your capability set."
    )
