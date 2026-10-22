"""Tool definitions and the capability set bound at run start.

The central claim of this platform is that an agent cannot write a commitment
(an owner, a due date, a status) into a shared work system. That claim is
enforced in three places, in descending order of how much I trust them:

1. ``register`` refuses, at import time, to admit any tool whose writable
   parameters include a commitment field. A tool that could express the
   forbidden write cannot enter the catalog, so no grant can reference it and
   no prompt can reach it. This fires in CI, before deploy.
2. ``Tool.invoke`` rejects any argument key outside its declared parameters,
   so a model cannot smuggle an extra field into a payload the transport
   would happily forward.
3. ``CapabilitySet`` binds only the tools an agent was granted. An ungranted
   tool is absent from the dispatch table rather than refused by it.

Layer 1 is the real control. Layers 2 and 3 exist so that a mistake in layer 1
is still contained, and so that the failure is observable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..registry.models import COMMITMENT_FIELDS, RiskTier


from ..errors import (  # noqa: F401  re-exported for callers of this module
    ProhibitedCapability,
    ToolContractViolation,
    ToolNotGranted,
)


@dataclass(frozen=True)
class Effect:
    """An intended change to the world. Described by a tool, applied elsewhere.

    Tool handlers are pure: they compute an ``Effect`` and return. Application
    is the executor's job for an ungated agent and the review gate's job for a
    gated one. That separation is what lets "held for review" mean the write
    has not happened yet, rather than that it happened and can be undone.

    ``inverse`` is filled in at apply time by ``catalog.apply_effect``, which
    knows the state it overwrote and therefore how to put it back.
    """

    kind: str
    target: str
    payload: Mapping[str, Any]
    inverse: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ToolResult:
    output: Any
    effects: tuple[Effect, ...] = ()
    cost_tokens: int = 0


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    #: Exactly the parameter names a caller may supply. Not a suggestion to a
    #: model — the enforced surface area of the tool, and the source the JSON
    #: schema sent to the model is generated from, so the two cannot drift.
    parameters: frozenset[str]
    handler: Callable[..., ToolResult]
    min_tier: RiskTier = RiskTier.T0_READ_INTERNAL
    scopes: tuple[str, ...] = ()
    writes: bool = False
    reversible: bool = False
    requires_human_gate: bool = False
    external: bool = False
    #: JSON-schema types per parameter; anything omitted defaults to string.
    param_types: Mapping[str, str] = field(default_factory=dict)
    #: Parameters the model must supply. Subset of ``parameters``.
    required: frozenset[str] = frozenset()

    def invoke(self, **kwargs: Any) -> ToolResult:
        extra = set(kwargs) - set(self.parameters)
        if extra:
            raise ToolContractViolation(
                f"{self.name}: undeclared argument(s) {sorted(extra)}; "
                f"declared parameters are {sorted(self.parameters)}"
            )
        # Belt and braces: layer 1 should make this unreachable. If it ever
        # fires, the catalog invariant has been broken and we want a loud
        # crash and an audit entry, not a filtered payload.
        smuggled = {k for k in kwargs if k.lower() in COMMITMENT_FIELDS}
        if smuggled:
            raise ProhibitedCapability(
                f"{self.name}: commitment field(s) {sorted(smuggled)} reached a "
                f"tool invocation. This should be unreachable — the catalog "
                f"invariant has been violated."
            )
        return self.handler(**kwargs)


CATALOG: dict[str, Tool] = {}


def assert_no_commitment_fields(name: str, parameters: Iterable[str]) -> None:
    """The prohibition, as a reusable check.

    Extracted so the MCP server enforces the identical rule rather than a
    parallel copy of it that drifts. One definition, one place to change it,
    and a single test can cover every surface that calls it.
    """
    offending = {p for p in parameters if p.lower() in COMMITMENT_FIELDS}
    if offending:
        raise ProhibitedCapability(
            f"{name!r} declares commitment field(s) {sorted(offending)}. Nothing "
            f"in this platform — tool, MCP endpoint or otherwise — may write an "
            f"owner, a due date or a status into a shared work system. Carry "
            f"human-authored text instead, or route the change through the "
            f"human review gate."
        )


def register(tool: Tool) -> Tool:
    """WALL 1. Admit a tool to the catalog, or refuse at import time.

    This runs when the module defining the tool is imported, which means a
    pull request adding a commitment-writing tool fails at collection time in
    CI. It is not a runtime check a clever input might route around; it is a
    constraint on what can exist in the process at all.
    """
    assert_no_commitment_fields(tool.name, tool.parameters)
    if tool.writes and not tool.reversible and not tool.requires_human_gate:
        raise ProhibitedCapability(
            f"tool {tool.name!r} writes, is irreversible, and has no human gate. "
            f"Pick two at most."
        )
    if tool.name in CATALOG:
        raise ProhibitedCapability(f"duplicate tool name {tool.name!r}")
    # The schema the model sees is generated from `parameters`, so anything
    # describing a field outside it would be a lie to the model and a gap in
    # the enforcement surface at the same time.
    stray = (set(tool.param_types) | set(tool.required)) - set(tool.parameters)
    if stray:
        raise ProhibitedCapability(
            f"tool {tool.name!r} describes undeclared parameter(s) {sorted(stray)}")
    CATALOG[tool.name] = tool
    return tool


@dataclass
class CapabilitySet:
    """The complete set of callables available to one agent, for one run.

    Built from the agent's grants and nothing else. There is no ambient
    namespace, no fallback tool, no "unknown tool" handler that tries to be
    helpful. Dispatch on an ungranted name raises.
    """

    agent_id: str
    tools: Mapping[str, Tool]
    budgets: dict[str, int] = field(default_factory=dict)
    used: dict[str, int] = field(default_factory=dict)

    def names(self) -> list[str]:
        return sorted(self.tools)

    def dispatch(self, name: str, **kwargs: Any) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolNotGranted(
                f"agent {self.agent_id!r} has no grant for tool {name!r}; "
                f"granted: {self.names()}"
            )
        n = self.used.get(name, 0)
        limit = self.budgets.get(name, 0)
        if n >= limit:
            raise ToolNotGranted(
                f"agent {self.agent_id!r} exhausted its per-run budget for "
                f"{name!r} ({limit})"
            )
        self.used[name] = n + 1
        return tool.invoke(**kwargs)
