"""The human review gate and the interruption budget.

Two separate ideas that both come down to spending someone's attention.

The gate holds an agent's effects in the journal as ``pending``. Nothing
reaches the world until a named human approves, and approval is recorded
against that human in the audit chain. Rejection rolls the run back.

The budget is the rule that adoption actually turns on. An agent that is
useful and noisy gets muted, and a muted agent is worth nothing. So contact
is capped per person per week, held to a bundling window, kept out of quiet
hours, and required to lead with something useful.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..capabilities.catalog import apply_effect, apply_inverse
from ..registry.models import ContactPolicy
from ..runtime import state as st
from ..runtime.value import minutes_for

WEEK = 7 * 24 * 3600


@dataclass
class GateDecision:
    approved: bool
    reviewer: str
    reason: str = ""


class ReviewGate:
    def __init__(self, store: st.Store, audit):
        self.store = store
        self.audit = audit

    def pending(self) -> list[st.Run]:
        return self.store.list_runs(st.AWAITING_REVIEW)

    def describe(self, run_id: str) -> dict:
        run = self.store.get(run_id)
        assert run is not None
        return {
            "run_id": run_id,
            "agent_id": run.agent_id,
            "tier": run.tier,
            "effects": self.store.effects(run_id, state="staged"),
            "steps": self.store.steps(run_id),
        }

    def decide(self, run_id: str, decision: GateDecision) -> str:
        run = self.store.get(run_id)
        if run is None or run.status != st.AWAITING_REVIEW:
            raise ValueError(f"run {run_id} is not awaiting review")

        self.audit.append(
            run_id,
            "gate.decision",
            {
                "reviewer": decision.reviewer,
                "approved": decision.approved,
                "reason": decision.reason,
            },
        )

        if decision.approved:
            # The writes happen here, after a named human consents, inside one
            # transaction. Before this line they had not happened at all.
            with self.store.conn:
                for eff in self.store.effects(run_id, state="staged"):
                    inverse = apply_effect(eff["kind"], eff["payload"])
                    self.store.set_effect_state(
                        run_id, eff["seq"], "applied", inverse=inverse
                    )
            self.store.set_status(run_id, st.COMMITTED)
            self.store.record_value(
                run_id, run.agent_id,
                minutes_saved=minutes_for(run.tier, len(self.store.steps(run_id))),
                basis="registered per-tier displacement baseline (gated run)",
            )
            self.audit.append(
                run_id, "effects.applied", {"reviewer": decision.reviewer}
            )
            return st.COMMITTED

        # Rejection discards staged effects. Nothing to undo in the common
        # case, which is the point of staging rather than applying-and-reverting.
        discarded = 0
        with self.store.conn:
            for eff in self.store.effects(run_id, state="staged"):
                self.store.set_effect_state(run_id, eff["seq"], "reversed")
                discarded += 1
        rollback(self.store, self.audit, run_id)
        self.store.set_status(run_id, st.REJECTED, error=decision.reason)
        self.audit.append(run_id, "effects.discarded", {"count": discarded})
        return st.REJECTED


def rollback(store: st.Store, audit, run_id: str) -> int:
    """Undo every *applied* effect of a run, newest first, in one transaction.

    Staged effects need no undoing; they never happened. Applied effects are
    reversed in reverse order inside a single transaction, so either the run's
    whole footprint disappears or the transaction fails and nothing is marked
    reversed. A half-undone run is not a state the system can be left in.
    """
    effects = [e for e in store.effects(run_id) if e["state"] == "applied"]
    reversed_count = 0
    with store.conn:
        for eff in reversed(effects):
            if eff["inverse"] is None:
                raise RuntimeError(
                    f"run {run_id} step {eff['seq']}: applied effect {eff['kind']!r} "
                    f"has no recorded inverse; this is a bug in apply_effect, and "
                    f"the run cannot be safely unwound"
                )
            apply_inverse(eff["kind"], eff["inverse"])
            store.set_effect_state(run_id, eff["seq"], "reversed")
            reversed_count += 1
    audit.append(run_id, "rollback", {"effects_reversed": reversed_count})
    return reversed_count


class InterruptionBudget:
    """A per-person weekly cap on being interrupted, held in the ledger."""

    def __init__(self, store: st.Store):
        self.store = store

    def can_contact(
        self, person: str, policy: ContactPolicy, now: float | None = None
    ) -> tuple[bool, str]:
        now = now if now is not None else time.time()
        spent = self.store.contacts_since(person, now - WEEK)
        if spent >= policy.max_interruptions_per_week:
            return False, (
                f"{person} has used {spent}/{policy.max_interruptions_per_week} "
                f"interruptions this week; bundling into next window"
            )
        hour = time.localtime(now).tm_hour
        start, end = policy.quiet_hours
        in_quiet = hour >= start or hour < end if start > end else start <= hour < end
        if in_quiet:
            return False, f"quiet hours for {person} ({start:02d}:00–{end:02d}:00)"
        return True, "within budget"

    def spend(
        self, person: str, run_id: str, subject: str, now: float | None = None
    ) -> None:
        self.store.record_contact(person, run_id, subject, now=now)
