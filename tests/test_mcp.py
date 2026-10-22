"""MCP server tests.

The interesting ones assert what the protocol surface *cannot* do. An MCP
client is a model; the tests below are the reason that is safe.
"""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest

from keel import mcp
from keel.capabilities.base import ProhibitedCapability
from keel.gate.review import GateDecision, ReviewGate
from keel.registry.loader import Registry
from keel.runtime import state as st

from .support import Completion, REGISTRY, ToolCall, harness


def call(name: str, **arguments):
    return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})


def payload(response) -> object:
    return json.loads(response["result"]["content"][0]["text"])


class MCPTestCase(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        mcp.CTX = mcp.Context(db=str(tmp / "m.db"), audit_path=str(tmp / "m.jsonl"))
        mcp.CTX._registry = Registry.load(REGISTRY)


class TestProtocol(MCPTestCase):
    def test_initialize_reports_protocol_and_server(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {}})
        self.assertEqual(r["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertEqual(r["result"]["serverInfo"]["name"], "keel")

    def test_notifications_get_no_response(self):
        self.assertIsNone(mcp.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_returns_valid_schemas(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = r["result"]["tools"]
        self.assertTrue(tools)
        for t in tools:
            self.assertIn("name", t)
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"]["additionalProperties"])

    def test_unknown_method_is_a_jsonrpc_error(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_serve_reads_and_writes_newline_delimited_json(self):
        out = io.StringIO()
        mcp.serve(io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"),
            out)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([x["id"] for x in lines], [1, 2])

    def test_malformed_json_returns_parse_error_without_dying(self):
        out = io.StringIO()
        mcp.serve(io.StringIO("{not json}\n"), out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)


class TestApprovalIsNotReachable(MCPTestCase):
    """The central claim of the MCP surface."""

    def test_no_tool_can_approve_or_reject(self):
        names = set(mcp.MCP_TOOLS)
        for forbidden in ("keel_approve", "keel_reject", "keel_review",
                          "keel_decide", "keel_gate"):
            self.assertNotIn(forbidden, names)
        # Nothing exposed even mentions approving as an action it performs.
        for tool in mcp.MCP_TOOLS.values():
            self.assertNotIn("approves", tool.description)

    def test_asking_to_approve_explains_why_it_cannot(self):
        r = call("keel_approve", run_id="run_x")
        self.assertEqual(r["error"]["code"], -32601)
        self.assertIn("human review gate", r["error"]["message"])

    def test_a_gated_run_submitted_over_mcp_still_stops_at_the_gate(self):
        """End to end: MCP starts it, a human still has to finish it."""
        ex, store, reg = harness([
            Completion(tool_call=ToolCall("work_item.append_note", {
                "item_id": "PLAT-19", "note": "carried", "authored_by": "@lin"})),
            Completion(text="staged")])
        mcp.CTX._registry, mcp.CTX._store = reg, store

        submitted = payload(call("keel_submit_run", agent_id="standup-drafter"))
        self.assertEqual(submitted["status"], st.QUEUED)
        self.assertTrue(submitted["gated"])

        ex.drain()  # a worker executes it; MCP did not

        pending = payload(call("keel_pending_approvals"))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["reviewer"], "@lin")
        # The client can see exactly what would happen, and cannot cause it.
        self.assertEqual(pending[0]["staged_effects"][0]["kind"],
                         "work_item.append_note")

        from keel.capabilities.catalog import WORK_ITEMS
        self.assertEqual(WORK_ITEMS["PLAT-19"]["notes"], [])

        # Only a human, outside the protocol, can complete it.
        ReviewGate(store, ex.audit).decide(
            pending[0]["run_id"], GateDecision(True, "@lin", "fine"))
        self.assertEqual(len(WORK_ITEMS["PLAT-19"]["notes"]), 1)


class TestProhibitionAppliesToMCP(MCPTestCase):
    def test_an_mcp_tool_declaring_a_commitment_field_is_refused(self):
        """Same guard as a capability, not a parallel copy of it."""
        for f in ("owner", "status", "due_date", "priority", "assignee"):
            with self.subTest(field=f):
                with self.assertRaises(ProhibitedCapability):
                    mcp.register_mcp(mcp.MCPTool(
                        name=f"keel_set_{f}", description="tidy the backlog",
                        parameters={"run_id": {"type": "string"},
                                    f: {"type": "string"}},
                        required=("run_id",), handler=lambda **kw: None))

    def test_the_guard_is_the_same_function_used_by_the_catalog(self):
        from keel.capabilities import base
        self.assertIs(mcp.assert_no_commitment_fields,
                      base.assert_no_commitment_fields)

    def test_no_registered_mcp_tool_exposes_a_commitment_field(self):
        from keel.registry.models import COMMITMENT_FIELDS
        for name, tool in mcp.MCP_TOOLS.items():
            offending = {p for p in tool.parameters if p.lower() in COMMITMENT_FIELDS}
            self.assertEqual(offending, set(), f"{name} exposes {offending}")


class TestReadTools(MCPTestCase):
    def test_list_agents_reports_governance_not_just_names(self):
        agents = payload(call("keel_list_agents"))
        self.assertGreaterEqual(len(agents), 6)
        one = next(a for a in agents if a["id"] == "standup-drafter")
        self.assertEqual(one["tier"], "T2_SYSTEM_OF_RECORD")
        self.assertTrue(one["human_gate"])
        self.assertIn("work_item.append_note", one["tools"])

    def test_submitting_an_unregistered_agent_is_refused_with_a_reason(self):
        r = call("keel_submit_run", agent_id="ghost-agent")
        self.assertTrue(r["result"]["isError"])
        self.assertIn("not registered", r["result"]["content"][0]["text"])

    def test_run_status_and_log_are_readable(self):
        ex, store, reg = harness([Completion(text="ok")])
        mcp.CTX._registry, mcp.CTX._store = reg, store
        mcp.CTX.audit_path = str(ex.audit.path)

        run_id = ex.submit("platform-qa", "test", {})
        ex.drain()

        status = payload(call("keel_run_status", run_id=run_id))
        self.assertEqual(status["status"], st.COMMITTED)

        log = payload(call("keel_run_log", run_id=run_id))
        self.assertTrue(log["chain_intact"])
        self.assertIn("run.enqueued", [e["kind"] for e in log["entries"]])

    def test_unknown_run_is_refused(self):
        r = call("keel_run_status", run_id="run_nope")
        self.assertTrue(r["result"]["isError"])

    def test_bad_arguments_are_an_invalid_params_error(self):
        r = call("keel_run_status", wrong="x")
        self.assertEqual(r["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
