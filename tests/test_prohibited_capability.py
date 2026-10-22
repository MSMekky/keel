"""The prohibition tests.

These are the tests I would want someone to read first. Each one asserts the
absence of a capability rather than the presence of a check, because a check
is something a future change can route around and an absent code path is not.
"""

import unittest

from keel.capabilities.base import ProhibitedCapability, Tool, register
from keel.registry.models import COMMITMENT_FIELDS, AutonomyClass, RiskTier
from keel.runtime.executor import Executor

from .support import Completion, ToolCall, harness


class TestNoHandlerExists(unittest.TestCase):
    def test_dispatch_table_has_no_entry_for_autonomous_commitment(self):
        """If someone adds a handler for this class, this test fails.

        That is the whole mechanism. The prohibition is defended by CI rather
        than by anyone remembering it.
        """
        ex, _, _ = harness([])
        handlers = Executor._handlers.fget(ex)
        self.assertNotIn(AutonomyClass.AUTONOMOUS_COMMITMENT, handlers)
        for cls in AutonomyClass:
            if cls is not AutonomyClass.AUTONOMOUS_COMMITMENT:
                self.assertIn(cls, handlers, f"{cls} has no handler")

    def test_registry_refuses_a_spec_requesting_it(self):
        from keel.registry.loader import _parse
        from .support import ROOT

        path = ROOT / "registry" / "rejected" / "REJECTED-triage-autopilot.yaml"
        with self.assertRaises(ProhibitedCapability):
            _parse(path)


class TestNoToolCanExpressIt(unittest.TestCase):
    def test_registering_a_commitment_writing_tool_raises(self):
        for field in ("owner", "due_date", "status", "priority", "assignee"):
            with self.subTest(field=field):
                with self.assertRaises(ProhibitedCapability):
                    register(
                        Tool(
                            name=f"work_item.set_{field}",
                            description="tidy the backlog",
                            parameters=frozenset({"item_id", field}),
                            handler=lambda **kw: None,
                            min_tier=RiskTier.T2_SYSTEM_OF_RECORD,
                            writes=True,
                            reversible=True,
                        )
                    )

    def test_no_registered_tool_declares_a_commitment_field(self):
        """Audit of the whole catalog, not a spot check."""
        from keel.capabilities.catalog import CATALOG

        for name, tool in CATALOG.items():
            offending = {p for p in tool.parameters if p.lower() in COMMITMENT_FIELDS}
            self.assertEqual(offending, set(), f"{name} declares {offending}")

    def test_irreversible_ungated_write_cannot_register(self):
        with self.assertRaises(ProhibitedCapability):
            register(
                Tool(
                    name="scratch.nuke",
                    description="delete everything, no undo, no gate",
                    parameters=frozenset({"path"}),
                    handler=lambda **kw: None,
                    writes=True,
                    reversible=False,
                    requires_human_gate=False,
                )
            )


class TestInjectionCannotReachTheWrite(unittest.TestCase):
    def test_injected_commitment_fields_are_refused_and_world_unchanged(self):
        """A model fully compromised by prompt injection still cannot do it."""
        ex, store, _ = harness([
            Completion(tool_call=ToolCall("work_item.append_note", {
                "item_id": "PLAT-19",
                "note": "urgent reassignment",
                "authored_by": "@lin",
                "owner": "@karim",
                "status": "blocked",
                "due_date": "2026-08-07",
            })),
            Completion(text="done"),
        ])
        ex.submit("standup-drafter", "test", {})
        outcome = ex.drain()[0]

        from keel.capabilities.catalog import WORK_ITEMS

        self.assertEqual(outcome.status, "rolled_back")
        self.assertIn("undeclared argument", outcome.error or "")
        self.assertEqual(WORK_ITEMS["PLAT-19"]["owner"], "@nadia")
        self.assertEqual(WORK_ITEMS["PLAT-19"]["status"], "in_progress")
        self.assertEqual(WORK_ITEMS["PLAT-19"]["notes"], [])

    def test_ungranted_tool_is_absent_not_refused(self):
        """pr-context-linter has no draft.save grant, so it is not bound."""
        ex, _, reg = harness([])
        caps = ex.capabilities_for(reg.get("pr-context-linter"))
        self.assertNotIn("draft.save", caps.tools)
        self.assertNotIn("draft.save", caps.names())


if __name__ == "__main__":
    unittest.main()
