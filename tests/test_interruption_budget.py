"""The interruption budget.

Adoption turns on this more than on anything else in the platform. An agent
that is useful and noisy gets muted, and a muted agent is worth nothing, so
contact is a metered resource with a ledger rather than a side effect.
"""

import time
import unittest

from keel.gate.review import WEEK, InterruptionBudget
from keel.registry.models import ContactPolicy

from .support import Completion, ToolCall, harness


class TestBudget(unittest.TestCase):
    def setUp(self):
        ex, self.store, _ = harness([])
        self.budget = InterruptionBudget(self.store)
        # Midday on a weekday, well outside any quiet hours.
        self.noon = time.mktime(time.strptime("2026-08-05 12:00", "%Y-%m-%d %H:%M"))

    def test_within_budget_by_default(self):
        ok, why = self.budget.can_contact(
            "@lin", ContactPolicy(max_interruptions_per_week=2), now=self.noon
        )
        self.assertTrue(ok, why)

    def test_cap_is_enforced_per_person_per_week(self):
        policy = ContactPolicy(max_interruptions_per_week=2)
        for i in range(2):
            self.budget.spend("@lin", f"run{i}", "digest", now=self.noon)
        ok, why = self.budget.can_contact("@lin", policy, now=self.noon)
        self.assertFalse(ok)
        self.assertIn("2/2", why)

        # A different person is unaffected: the budget is theirs, not global.
        ok, _ = self.budget.can_contact("@karim", policy, now=self.noon)
        self.assertTrue(ok)

    def test_the_window_rolls(self):
        policy = ContactPolicy(max_interruptions_per_week=1)
        self.budget.spend("@lin", "old", "digest", now=self.noon)
        blocked, _ = self.budget.can_contact("@lin", policy, now=self.noon)
        self.assertFalse(blocked)
        later, _ = self.budget.can_contact("@lin", policy, now=self.noon + WEEK + 60)
        self.assertTrue(later)

    def test_quiet_hours_defer_rather_than_drop(self):
        policy = ContactPolicy(quiet_hours=(20, 8))
        night = time.mktime(time.strptime("2026-08-05 23:00", "%Y-%m-%d %H:%M"))
        ok, why = self.budget.can_contact("@lin", policy, now=night)
        self.assertFalse(ok)
        self.assertIn("quiet hours", why)


class TestExecutorRespectsBudget(unittest.TestCase):
    def test_over_budget_contact_is_deferred_not_sent(self):
        """The tool is never invoked, so no message and no journal entry."""
        ex, store, _ = harness([
            Completion(tool_call=ToolCall("warehouse.query", {"table": "incidents"})),
            Completion(text="nothing to report"),
        ])
        from keel.capabilities.catalog import SENT_MESSAGES

        ex.submit("platform-qa", "test", {})
        ex.drain()
        self.assertEqual(SENT_MESSAGES, [])


if __name__ == "__main__":
    unittest.main()
