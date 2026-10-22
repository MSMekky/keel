"""Domain model for the agent registry.

Every agent in the platform is a registered, typed object. There is no path
to execution for an object that is not in this registry: the executor takes
an ``AgentSpec``, not a name and a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum


class RiskTier(IntEnum):
    """Risk tier determines review depth, eval requirements and gating.

    Tiers are ordered. Requirements are monotonic: anything required at
    tier N is required at every tier above it.
    """

    T0_READ_INTERNAL = 0  # reads internal data, produces internal output
    T1_DRAFT_INTERNAL = 1  # drafts content a human will edit before it lands
    T2_SYSTEM_OF_RECORD = 2  # carries human-authored content into a real system
    T3_EXTERNAL_FACING = 3  # output leaves the company

    @property
    def label(self) -> str:
        return self.name.split("_", 1)[1].replace("_", " ").lower()


class AutonomyClass(Enum):
    """What an agent is structurally permitted to do without a human.

    ``AUTONOMOUS_COMMITMENT`` is declared here for exactly one reason: so the
    validator can name and reject it. It has no handler in the executor's
    dispatch table and no tool in the catalog can serve it. See
    ``tests/test_prohibited_capability.py``, which asserts the absence of a
    code path rather than the presence of a warning.
    """

    READ_ONLY = "read_only"
    DRAFT_FOR_HUMAN = "draft_for_human"
    CARRY_HUMAN_AUTHORED = "carry_human_authored"
    INITIATE_CONTACT = "initiate_contact"
    AUTONOMOUS_COMMITMENT = "autonomous_commitment"  # PROHIBITED — never dispatchable


#: Autonomy classes that the platform will never execute, under any tier,
#: any grant, any operator override. Enforced at load time and again at
#: dispatch time, and covered by a test that fails if a handler ever appears.
PROHIBITED_AUTONOMY = frozenset({AutonomyClass.AUTONOMOUS_COMMITMENT})


#: Field names that express a commitment in a shared work system: who owns a
#: piece of work, when it is due, and what state it is in. No tool may declare
#: these as writable parameters. Registration of such a tool raises at import
#: time, so the failure lands in CI rather than in production.
COMMITMENT_FIELDS = frozenset(
    {
        "owner",
        "owners",
        "assignee",
        "assignees",
        "due",
        "due_date",
        "duedate",
        "deadline",
        "status",
        "state",
        "stage",
        "priority",
        "sprint",
        "milestone",
    }
)


@dataclass(frozen=True)
class ToolGrant:
    """A single tool made available to a single agent.

    The grant is the credential. An agent's runtime namespace is built from
    its grants and contains nothing else, so an ungranted tool is not refused
    at call time — it does not exist to be called.
    """

    tool: str
    scopes: tuple[str, ...] = ()
    rate_per_run: int = 10


@dataclass(frozen=True)
class ContactPolicy:
    """How, and how often, this agent is allowed to interrupt a human."""

    max_interruptions_per_week: int = 2
    bundle_window_minutes: int = 240
    quiet_hours: tuple[int, int] = (20, 8)  # local hour range, inclusive start
    must_deliver_value_first: bool = True


@dataclass(frozen=True)
class AgentSpec:
    id: str
    owner: str  # a human. not a team alias, not an email list.
    tier: RiskTier
    autonomy: AutonomyClass
    purpose: str
    grants: tuple[ToolGrant, ...] = ()
    human_gate: bool = True
    eval_suite: str | None = None
    contact: ContactPolicy = field(default_factory=ContactPolicy)
    model_hint: str | None = None  # advisory only; runtime is model-agnostic

    @property
    def granted_tools(self) -> frozenset[str]:
        return frozenset(g.tool for g in self.grants)

    def grant_for(self, tool: str) -> ToolGrant | None:
        for g in self.grants:
            if g.tool == tool:
                return g
        return None
