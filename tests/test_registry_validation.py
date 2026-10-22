"""Registry validation.

Every rule here exists because of a specific way an agent programme goes
wrong. The test names say which one.
"""

import unittest

from keel.errors import ProhibitedCapability, RegistryError
from keel.registry.loader import Registry, validate
from keel.registry.models import (
    AgentSpec,
    AutonomyClass,
    ContactPolicy,
    RiskTier,
    ToolGrant,
)

from .support import REGISTRY


def spec(**kw) -> AgentSpec:
    base = dict(
        id="test-agent",
        owner="@nadia",
        tier=RiskTier.T0_READ_INTERNAL,
        autonomy=AutonomyClass.READ_ONLY,
        purpose="test",
        grants=(),
        human_gate=True,
        eval_suite=None,
        contact=ContactPolicy(),
    )
    base.update(kw)
    return AgentSpec(**base)


class TestShippedRegistryIsValid(unittest.TestCase):
    def test_registry_loads(self):
        reg = Registry.load(REGISTRY)
        self.assertGreaterEqual(len(reg), 4)

    def test_every_agent_has_an_owner_who_is_a_person(self):
        for s in Registry.load(REGISTRY):
            self.assertTrue(s.owner.startswith("@") or "@" in s.owner)

    def test_unregistered_agents_have_no_execution_path(self):
        with self.assertRaises(RegistryError):
            Registry.load(REGISTRY).get("some-agent-nobody-registered")


class TestRules(unittest.TestCase):
    def test_prohibited_autonomy_is_rejected(self):
        with self.assertRaises(ProhibitedCapability):
            validate(spec(autonomy=AutonomyClass.AUTONOMOUS_COMMITMENT))

    def test_above_tier_zero_requires_an_eval_suite(self):
        """Trust is earned by measurement; only pure readers ship unevaluated."""
        with self.assertRaises(RegistryError):
            validate(spec(tier=RiskTier.T1_DRAFT_INTERNAL,
                          autonomy=AutonomyClass.DRAFT_FOR_HUMAN,
                          eval_suite=None))
        validate(spec(tier=RiskTier.T1_DRAFT_INTERNAL,
                      autonomy=AutonomyClass.DRAFT_FOR_HUMAN,
                      eval_suite="some-suite"))

    def test_system_of_record_cannot_be_ungated(self):
        with self.assertRaises(RegistryError):
            validate(spec(tier=RiskTier.T2_SYSTEM_OF_RECORD,
                          autonomy=AutonomyClass.CARRY_HUMAN_AUTHORED,
                          eval_suite="s", human_gate=False))

    def test_autonomy_must_be_reachable_from_the_tier(self):
        with self.assertRaises(RegistryError):
            validate(spec(tier=RiskTier.T0_READ_INTERNAL,
                          autonomy=AutonomyClass.CARRY_HUMAN_AUTHORED))

    def test_grant_to_an_unknown_tool_is_rejected(self):
        with self.assertRaises(RegistryError):
            validate(spec(grants=(ToolGrant(tool="work_item.set_fields"),)))

    def test_grant_above_the_agents_tier_is_rejected(self):
        with self.assertRaises(RegistryError):
            validate(spec(tier=RiskTier.T0_READ_INTERNAL,
                          grants=(ToolGrant(tool="notify.send"),)))

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(RegistryError):
            validate(spec(grants=(
                ToolGrant(tool="warehouse.query", scopes=("payroll",)),
            )))

    def test_team_alias_as_owner_is_rejected(self):
        """An incident needs a person to page, not a mailing list."""
        with self.assertRaises(RegistryError):
            validate(spec(owner="platform-team"))


if __name__ == "__main__":
    unittest.main()
