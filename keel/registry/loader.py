"""Load and validate the agent registry.

Validation is not advisory. A spec that fails any rule below raises and the
process stops; there is no "load with warnings" mode, because a warning is a
thing a tired person merges on a Friday.
"""

from __future__ import annotations

import pathlib

import yaml

from .models import (
    PROHIBITED_AUTONOMY,
    AgentSpec,
    AutonomyClass,
    ContactPolicy,
    RiskTier,
    ToolGrant,
)


from ..errors import ProhibitedCapability, RegistryError  # noqa: F401


class Registry:
    def __init__(self, agents: dict[str, AgentSpec]):
        self._agents = agents

    def __len__(self) -> int:
        return len(self._agents)

    def __iter__(self):
        return iter(sorted(self._agents.values(), key=lambda a: a.id))

    def get(self, agent_id: str) -> AgentSpec:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise RegistryError(
                f"agent {agent_id!r} is not registered; unregistered agents have "
                f"no execution path"
            ) from None

    @classmethod
    def load(cls, root: str | pathlib.Path) -> "Registry":
        root = pathlib.Path(root)
        paths = sorted(root.glob("**/*.yaml")) + sorted(root.glob("**/*.yml"))
        agents: dict[str, AgentSpec] = {}
        for path in paths:
            spec = _parse(path)
            if spec.id in agents:
                raise RegistryError(f"duplicate agent id {spec.id!r} at {path}")
            agents[spec.id] = spec
        return cls(agents)


def _parse(path: pathlib.Path) -> AgentSpec:
    raw = yaml.safe_load(path.read_text()) or {}
    where = path.name

    def require(key: str):
        if key not in raw or raw[key] in (None, "", []):
            raise RegistryError(f"{where}: missing required field {key!r}")
        return raw[key]

    agent_id = require("id")
    owner = require("owner")
    purpose = require("purpose")

    try:
        tier = RiskTier[require("tier")]
    except KeyError:
        raise RegistryError(
            f"{where}: unknown tier {raw.get('tier')!r}; expected one of "
            f"{[t.name for t in RiskTier]}"
        ) from None

    try:
        autonomy = AutonomyClass(require("autonomy"))
    except ValueError:
        raise RegistryError(
            f"{where}: unknown autonomy class {raw.get('autonomy')!r}"
        ) from None

    grants = tuple(
        ToolGrant(
            tool=g["tool"],
            scopes=tuple(g.get("scopes", ())),
            rate_per_run=int(g.get("rate_per_run", 10)),
        )
        for g in raw.get("grants", [])
    )

    c = raw.get("contact", {}) or {}
    contact = ContactPolicy(
        max_interruptions_per_week=int(c.get("max_interruptions_per_week", 2)),
        bundle_window_minutes=int(c.get("bundle_window_minutes", 240)),
        quiet_hours=tuple(c.get("quiet_hours", (20, 8))),  # type: ignore[arg-type]
        must_deliver_value_first=bool(c.get("must_deliver_value_first", True)),
    )

    spec = AgentSpec(
        id=agent_id,
        owner=owner,
        tier=tier,
        autonomy=autonomy,
        purpose=purpose,
        grants=grants,
        human_gate=bool(raw.get("human_gate", True)),
        eval_suite=raw.get("eval_suite"),
        contact=contact,
        model_hint=raw.get("model_hint"),
    )
    validate(spec, where=where)
    return spec


def validate(spec: AgentSpec, where: str = "<spec>") -> None:
    """Rules the pipeline enforces. Each one exists because of a failure mode."""

    # 1. The prohibited class. Named so it can be rejected, never executed.
    if spec.autonomy in PROHIBITED_AUTONOMY:
        raise ProhibitedCapability(
            f"{where}: autonomy class {spec.autonomy.value!r} is prohibited. "
            f"An agent may read and report, carry a human-authored update into a "
            f"system of record once a human consents, and initiate contact within "
            f"budget. It may never author a commitment. There is no handler for "
            f"this class."
        )

    # 2. Anything above read-only needs a suite. Trust is earned by measurement.
    if spec.tier > RiskTier.T0_READ_INTERNAL and not spec.eval_suite:
        raise RegistryError(
            f"{where}: tier {spec.tier.name} requires eval_suite; only "
            f"T0_READ_INTERNAL may ship unevaluated"
        )

    # 3. Anything that touches a system of record or the outside world is gated.
    if spec.tier >= RiskTier.T2_SYSTEM_OF_RECORD and not spec.human_gate:
        raise RegistryError(
            f"{where}: tier {spec.tier.name} requires human_gate: true"
        )

    # 4. Autonomy must be reachable from the tier. A read-only agent that
    #    claims it will carry writes is a misconfiguration, not a permission.
    ceiling = {
        RiskTier.T0_READ_INTERNAL: {AutonomyClass.READ_ONLY},
        RiskTier.T1_DRAFT_INTERNAL: {
            AutonomyClass.READ_ONLY,
            AutonomyClass.DRAFT_FOR_HUMAN,
        },
        RiskTier.T2_SYSTEM_OF_RECORD: {
            AutonomyClass.READ_ONLY,
            AutonomyClass.DRAFT_FOR_HUMAN,
            AutonomyClass.CARRY_HUMAN_AUTHORED,
        },
        RiskTier.T3_EXTERNAL_FACING: {
            AutonomyClass.READ_ONLY,
            AutonomyClass.DRAFT_FOR_HUMAN,
            AutonomyClass.CARRY_HUMAN_AUTHORED,
            AutonomyClass.INITIATE_CONTACT,
        },
    }[spec.tier]
    if spec.autonomy not in ceiling:
        raise RegistryError(
            f"{where}: autonomy {spec.autonomy.value!r} is not reachable at tier "
            f"{spec.tier.name}"
        )

    # 5. Owner must look like a person, because an incident needs someone to page.
    if "@" not in spec.owner and not spec.owner.startswith("@"):
        raise RegistryError(
            f"{where}: owner must be an individual handle or email, got {spec.owner!r}"
        )

    # 6. Grants must resolve. Imported late to avoid a circular import at module load.
    from ..capabilities.catalog import CATALOG

    for grant in spec.grants:
        if grant.tool not in CATALOG:
            raise RegistryError(
                f"{where}: grant references unknown tool {grant.tool!r}"
            )
        tool = CATALOG[grant.tool]
        if tool.min_tier > spec.tier:
            raise RegistryError(
                f"{where}: tool {grant.tool!r} requires tier "
                f"{tool.min_tier.name} or above, agent is {spec.tier.name}"
            )
        unknown = set(grant.scopes) - set(tool.scopes)
        if unknown:
            raise RegistryError(
                f"{where}: tool {grant.tool!r} has no scope(s) {sorted(unknown)}"
            )
