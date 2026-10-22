"""An MCP server exposing Keel to any MCP client.

Speaks JSON-RPC 2.0 over stdio, per the Model Context Protocol. No SDK: the
protocol surface needed here is four methods, and a dependency that has to be
pinned and upgraded is worse than sixty lines of dispatch.

────────────────────────────────────────────────────────────────────────────
WHAT IS DELIBERATELY NOT EXPOSED, AND WHY

There is no ``keel_approve`` tool. An MCP client — which is a model — must not
be able to approve a run. If it could, the human review gate would be a
formality that an agent satisfies on its own behalf, and every guarantee
downstream of the gate would be void.

This is the same argument as the capability walls, one level up. The gate
exists because a human decides. A protocol endpoint that lets a model decide
is not a smaller version of that; it is its absence. So approval stays in the
CLI, where a person types it, and the audit chain records which person.

Nothing here can reject a run either, for the same reason inverted: a model
that can reject can suppress work it dislikes without a human ever seeing it.

Everything exposed is read, or submit-for-review. A client can start work and
watch it. It cannot finish it.
────────────────────────────────────────────────────────────────────────────

The prohibition applies here too, and via the *same* function. Every tool
below is checked by ``assert_no_commitment_fields`` from
``keel.capabilities.base`` — not a copy of the rule, the rule itself. A test
asserts an MCP tool declaring ``owner`` fails at registration exactly as a
capability does.

Run it:

    python -m keel.mcp                # stdio, for a local MCP client

Register it with a client by pointing at that command. See docs/MCP.md.
"""

from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from .audit.log import AuditLog
from .capabilities.base import assert_no_commitment_fields
from .errors import KeelError
from .registry.loader import Registry
from .runtime import state as st
from .runtime.executor import Executor
from .runtime.providers import provider_from_env

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "keel", "version": "0.2.0"}

ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY_DIR = ROOT / "registry" / "agents"


@dataclass(frozen=True)
class MCPTool:
    name: str
    description: str
    parameters: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    handler: Callable[..., Any]

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": self.parameters,
                "required": list(self.required),
                "additionalProperties": False,
            },
        }


MCP_TOOLS: dict[str, MCPTool] = {}


def register_mcp(tool: MCPTool) -> MCPTool:
    """Admit an MCP tool, or refuse at import.

    Reuses ``assert_no_commitment_fields`` rather than reimplementing it, so
    there is exactly one definition of what a commitment field is and one
    place to change it.
    """
    assert_no_commitment_fields(f"mcp tool {tool.name}", tool.parameters)
    if tool.name in MCP_TOOLS:
        raise KeelError(f"duplicate MCP tool {tool.name!r}")
    MCP_TOOLS[tool.name] = tool
    return tool


# ---------------------------------------------------------------------------
# Server state. Built lazily so importing this module is cheap and testable.
# ---------------------------------------------------------------------------


@dataclass
class Context:
    db: str = "keel.db"
    audit_path: str = "audit.jsonl"
    _registry: Registry | None = None
    _store: st.Store | None = None

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            self._registry = Registry.load(REGISTRY_DIR)
        return self._registry

    @property
    def store(self) -> st.Store:
        if self._store is None:
            self._store = st.Store(self.db)
        return self._store

    @property
    def audit(self) -> AuditLog:
        return AuditLog(self.audit_path)


CTX = Context()


# ---------------------------------------------------------------------------
# Tools — read and submit only
# ---------------------------------------------------------------------------


def _list_agents() -> Any:
    return [{
        "id": s.id,
        "owner": s.owner,
        "tier": s.tier.name,
        "autonomy": s.autonomy.value,
        "human_gate": s.human_gate,
        "eval_suite": s.eval_suite,
        "tools": sorted(s.granted_tools),
        "purpose": s.purpose.strip(),
    } for s in CTX.registry]


register_mcp(MCPTool(
    name="keel_list_agents",
    description="List every registered agent with its owner, risk tier, "
                "autonomy class, granted tools and whether it is gated.",
    parameters={}, required=(), handler=_list_agents,
))


def _submit_run(agent_id: str, input_json: str = "{}") -> Any:
    """Queue a run. Does not execute it and does not approve anything.

    Submission is safe to expose because everything downstream still applies:
    the agent must be registered, its grants bind its capabilities, and if it
    is gated its effects stage and wait for a human.
    """
    spec = CTX.registry.get(agent_id)  # raises if unregistered
    payload = json.loads(input_json or "{}")
    ex = Executor(CTX.registry, CTX.store, provider_from_env(), CTX.audit)
    run_id = ex.submit(spec.id, trigger="mcp", payload=payload)
    return {"run_id": run_id, "status": st.QUEUED, "agent": spec.id,
            "gated": spec.human_gate,
            "note": "queued. A gated agent will stop at the human review gate; "
                    "approval is not available over MCP."}


register_mcp(MCPTool(
    name="keel_submit_run",
    description="Queue a run of a registered agent. The run is not executed by "
                "this call and cannot be approved through this server.",
    parameters={
        "agent_id": {"type": "string", "description": "A registered agent id."},
        "input_json": {"type": "string",
                       "description": "JSON object of run input."},
    },
    required=("agent_id",), handler=_submit_run,
))


def _run_status(run_id: str) -> Any:
    run = CTX.store.get(run_id)
    if run is None:
        raise KeelError(f"no such run {run_id!r}")
    return {"run_id": run.run_id, "agent": run.agent_id, "status": run.status,
            "trigger": run.trigger, "error": run.error,
            "staged_effects": len(CTX.store.effects(run_id, state="staged")),
            "applied_effects": len(CTX.store.effects(run_id, state="applied"))}


register_mcp(MCPTool(
    name="keel_run_status",
    description="Current status of a run, including how many effects are "
                "staged awaiting human approval and how many were applied.",
    parameters={"run_id": {"type": "string"}},
    required=("run_id",), handler=_run_status,
))


def _run_log(run_id: str, limit: int = 100) -> Any:
    """The audit chain for one run: every prompt, tool call and decision."""
    entries = CTX.audit.entries(run_id)[: int(limit)]
    ok, broken = CTX.audit.verify()
    return {"run_id": run_id, "chain_intact": ok, "broken_at": broken,
            "entries": [{"kind": e["kind"], "at": e["at"], "data": e["data"]}
                        for e in entries]}


register_mcp(MCPTool(
    name="keel_run_log",
    description="Full decision log for a run — every prompt, tool call, tool "
                "result and gate decision — plus whether the hash chain "
                "verifies.",
    parameters={"run_id": {"type": "string"},
                "limit": {"type": "integer"}},
    required=("run_id",), handler=_run_log,
))


def _pending_approvals() -> Any:
    """What is waiting on a human. Readable so a client can *tell* someone.

    Surfacing the queue is useful and safe. Acting on it is not, and there is
    no endpoint here that does.
    """
    out = []
    for run in CTX.store.list_runs(st.AWAITING_REVIEW):
        spec = CTX.registry.get(run.agent_id)
        out.append({
            "run_id": run.run_id, "agent": run.agent_id,
            "reviewer": spec.owner, "tier": spec.tier.name,
            "staged_effects": [
                {"kind": e["kind"], "target": e["target"], "payload": e["payload"]}
                for e in CTX.store.effects(run.run_id, state="staged")],
            "approve_with": f"keel review approve {run.run_id} --reviewer <you>",
        })
    return out


register_mcp(MCPTool(
    name="keel_pending_approvals",
    description="Runs waiting at the human review gate, with the exact effects "
                "each would apply and who owns the decision. Read-only: "
                "approval and rejection are not exposed over this protocol.",
    parameters={}, required=(), handler=_pending_approvals,
))


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 dispatch
# ---------------------------------------------------------------------------


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    method, req_id = request.get("method"), request.get("id")
    params = request.get("params") or {}

    def ok(result: Any) -> dict[str, Any] | None:
        return None if req_id is None else {
            "jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict[str, Any] | None:
        return None if req_id is None else {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({"protocolVersion": PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": SERVER_INFO})

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": [t.schema() for t in MCP_TOOLS.values()]})

    if method == "tools/call":
        name = params.get("name")
        tool = MCP_TOOLS.get(name)
        if tool is None:
            # Named explicitly so a client asking for approval learns why.
            hint = ("" if name not in ("keel_approve", "keel_reject",
                                       "keel_review")
                    else " Approval is deliberately not exposed over MCP: the "
                         "human review gate exists because a person decides. "
                         "Use `keel review approve <run_id>` in a terminal.")
            return err(-32601, f"unknown tool {name!r}.{hint}")
        try:
            result = tool.handler(**(params.get("arguments") or {}))
        except TypeError as exc:
            return err(-32602, f"bad arguments for {name}: {exc}")
        except KeelError as exc:
            # Domain refusals are results, not transport errors: the client
            # should see *why* it was refused, in the content.
            return ok({"content": [{"type": "text", "text": str(exc)}],
                       "isError": True})
        return ok({"content": [{"type": "text",
                                "text": json.dumps(result, indent=2, default=str)}],
                   "isError": False})

    return err(-32601, f"unknown method {method!r}")


def serve(stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        response = handle(request)
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
