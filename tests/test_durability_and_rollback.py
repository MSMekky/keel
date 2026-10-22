"""Durability and rollback.

The property under test is not "the happy path works" — it is what the system
looks like after it is interrupted at the worst moment.
"""

import unittest

from keel.gate.review import GateDecision, ReviewGate, rollback
from keel.runtime import state as st

from .support import Completion, ToolCall, harness


class TestDurableState(unittest.TestCase):
    def test_committed_steps_survive_a_dead_process(self):
        ex, store, _ = harness([
            Completion(tool_call=ToolCall("warehouse.query", {"table": "merged_prs"})),
            Completion(text="done"),
        ])
        ex.submit("platform-qa", "test", {})
        ex.drain()
        run = store.list_runs()[0]
        steps = store.steps(run.run_id)

        # A new Store over the same file is what a restarted process sees.
        reopened = st.Store(store.path)
        self.assertEqual(len(reopened.steps(run.run_id)), len(steps))
        self.assertEqual(reopened.get(run.run_id).status, st.COMMITTED)

    def test_resume_continues_rather_than_restarting(self):
        ex, store, _ = harness([Completion(text="finished after resume")])
        run_id = store.enqueue("platform-qa", 0, "cron", {})
        store.claim_next()
        store.record_step(run_id, "tool_result", {
            "tool": "warehouse.query",
            "arguments": {"table": "merged_prs"},
            "output": [{"repo": "supabase/auth"}],
        })
        before = len(store.steps(run_id))

        outcomes = ex.resume()

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, st.COMMITTED)
        # The already-executed warehouse read was replayed as context, not
        # re-run: exactly one tool_result step exists.
        results = [s for s in store.steps(run_id) if s["kind"] == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertGreater(len(store.steps(run_id)), before)

    def test_a_run_left_running_is_found_by_resume_candidates(self):
        ex, store, _ = harness([])
        run_id = store.enqueue("platform-qa", 0, "cron", {})
        store.claim_next()
        self.assertEqual([r.run_id for r in store.resume_candidates()], [run_id])


class TestGateAndRollback(unittest.TestCase):
    def test_staged_effects_do_not_touch_the_world_before_approval(self):
        ex, store, _ = harness([
            Completion(tool_call=ToolCall("work_item.append_note", {
                "item_id": "PLAT-19", "note": "resume path next", "authored_by": "@lin",
            })),
            Completion(text="staged"),
        ])
        ex.submit("standup-drafter", "test", {})
        outcome = ex.drain()[0]

        from keel.capabilities.catalog import WORK_ITEMS

        self.assertEqual(outcome.status, st.AWAITING_REVIEW)
        self.assertEqual(outcome.effects_pending, 1)
        self.assertEqual(WORK_ITEMS["PLAT-19"]["notes"], [])

        ReviewGate(store, ex.audit).decide(
            outcome.run_id, GateDecision(True, "@lin", "verbatim, fine")
        )
        self.assertEqual(len(WORK_ITEMS["PLAT-19"]["notes"]), 1)
        self.assertEqual(store.get(outcome.run_id).status, st.COMMITTED)

    def test_rejection_discards_without_ever_writing(self):
        ex, store, _ = harness([
            Completion(tool_call=ToolCall("work_item.append_note", {
                "item_id": "PLAT-19", "note": "wrong item", "authored_by": "@lin",
            })),
            Completion(text="staged"),
        ])
        ex.submit("standup-drafter", "test", {})
        outcome = ex.drain()[0]

        ReviewGate(store, ex.audit).decide(
            outcome.run_id, GateDecision(False, "@lin", "wrong item")
        )

        from keel.capabilities.catalog import WORK_ITEMS

        self.assertEqual(WORK_ITEMS["PLAT-19"]["notes"], [])
        self.assertEqual(store.get(outcome.run_id).status, st.REJECTED)
        self.assertTrue(
            all(e["state"] == "reversed" for e in store.effects(outcome.run_id))
        )

    def test_rollback_of_an_applied_effect_restores_prior_state(self):
        """Ungated agent applies immediately; rollback must undo it."""
        ex, store, reg = harness([
            Completion(tool_call=ToolCall("warehouse.query", {"table": "merged_prs"})),
            Completion(text="ok"),
        ])
        # Force the applied path with a gate-free write.
        from keel.capabilities.catalog import PUBLISHED, apply_effect

        run_id = store.enqueue("exec-weekly-digest", 1, "test", {})
        PUBLISHED["draft:x"] = "original"
        inverse = apply_effect("draft.save", {"key": "x", "body": "overwritten"})
        store.record_effect(run_id, 1, "draft.save", "x",
                            {"key": "x", "body": "overwritten"},
                            inverse=inverse, state="applied")
        self.assertEqual(PUBLISHED["draft:x"], "overwritten")

        n = rollback(store, ex.audit, run_id)

        self.assertEqual(n, 1)
        self.assertEqual(PUBLISHED["draft:x"], "original")

    def test_audit_chain_detects_tampering(self):
        ex, store, _ = harness([Completion(text="ok")])
        ex.submit("platform-qa", "test", {})
        ex.drain()
        ok, broken = ex.audit.verify()
        self.assertTrue(ok, broken)

        lines = ex.audit.path.read_text().splitlines()
        lines[1] = lines[1].replace("platform-qa", "some-other-agent")
        ex.audit.path.write_text("\n".join(lines) + "\n")

        ok, broken = ex.audit.verify()
        self.assertFalse(ok)
        self.assertIsNotNone(broken)


if __name__ == "__main__":
    unittest.main()
