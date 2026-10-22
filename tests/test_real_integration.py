"""Tests for the real integration layer.

These exercise the actual request-construction and response-parsing code
against a fake transport. What that proves and what it does not:

PROVEN HERE
  request shape, headers, tool-schema generation, name mangling, tool_use
  parsing, usage accounting, retry policy, GitHub URL construction, the
  staged-then-applied ordering of a real write, and rollback by deletion.

NOT PROVEN HERE
  that Anthropic and GitHub accept these requests. No socket is opened in this
  suite. That is verified by recording a cassette against the live APIs — see
  docs/RUNNING.md — which is a step a human runs once with real credentials.

Being explicit about the boundary matters more than pretending it is not there.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from keel.audit.log import AuditLog
from keel.capabilities import github
from keel.capabilities.base import ProhibitedCapability, Tool, register
from keel.capabilities.catalog import CATALOG
from keel.capabilities.github import GitHub
from keel.gate.review import GateDecision, ReviewGate, rollback
from keel.registry.models import COMMITMENT_FIELDS, RiskTier
from keel.runtime import state as st
from keel.runtime.executor import Executor
from keel.runtime.http import Client, HTTPError, Response
from keel.runtime.providers import AnthropicProvider, tool_schema

from .support import REGISTRY
from keel.registry.loader import Registry


class FakeTransport:
    """Returns queued responses and records what it was asked to send."""

    def __init__(self, responses: list[tuple[int, dict]]):
        self.responses = list(responses)
        self.sent: list[dict] = []

    def send(self, method, url, headers, body, timeout) -> Response:
        self.sent.append({"method": method, "url": url, "headers": headers,
                          "body": json.loads(body.decode()) if body else None})
        status, payload = self.responses.pop(0)
        return Response(status, {}, json.dumps(payload).encode())


def _msg(text=None, tool=None, args=None, tokens=(10, 5)) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "toolu_1", "name": tool,
                        "input": args or {}})
    return {"model": "claude-sonnet-4-6", "content": content,
            "stop_reason": "tool_use" if tool else "end_turn",
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}}


# ---------------------------------------------------------------------------


class TestHTTPClient(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        t = FakeTransport([(429, {}), (200, {"ok": True})])
        c = Client(t, max_retries=3, sleep=lambda _: None)
        self.assertEqual(c.request("GET", "https://x/y", headers={}), {"ok": True})
        self.assertEqual(len(t.sent), 2)

    def test_retries_on_5xx(self):
        t = FakeTransport([(503, {}), (500, {}), (200, {"ok": 1})])
        c = Client(t, max_retries=3, sleep=lambda _: None)
        c.request("GET", "https://x/y", headers={})
        self.assertEqual(len(t.sent), 3)

    def test_does_not_retry_a_400(self):
        """A 4xx that is not 429 is our bug; retrying repeats it expensively."""
        t = FakeTransport([(400, {"error": "bad request"})])
        c = Client(t, max_retries=3, sleep=lambda _: None)
        with self.assertRaises(HTTPError) as ctx:
            c.request("POST", "https://x/y", headers={}, json_body={})
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(t.sent), 1)

    def test_gives_up_after_max_retries(self):
        t = FakeTransport([(500, {})] * 4)
        c = Client(t, max_retries=3, sleep=lambda _: None)
        with self.assertRaises(HTTPError):
            c.request("GET", "https://x/y", headers={})
        self.assertEqual(len(t.sent), 4)


class TestAnthropicProvider(unittest.TestCase):
    def setUp(self):
        self.reg = Registry.load(REGISTRY)

    def _provider(self, responses):
        t = FakeTransport(responses)
        return AnthropicProvider(api_key="test-key",
                                 client=Client(t, max_retries=0)), t

    def test_request_shape_and_auth_headers(self):
        p, t = self._provider([(200, _msg(text="hi"))])
        p.complete([{"role": "system", "content": "you are x"},
                    {"role": "user", "content": "go"}], [])
        sent = t.sent[0]
        self.assertEqual(sent["method"], "POST")
        self.assertIn("api.anthropic.com/v1/messages", sent["url"])
        self.assertEqual(sent["headers"]["x-api-key"], "test-key")
        self.assertEqual(sent["headers"]["anthropic-version"], "2023-06-01")
        # system is a top-level field, not a message
        self.assertEqual(sent["body"]["system"], "you are x")
        self.assertEqual([m["role"] for m in sent["body"]["messages"]], ["user"])

    def test_tool_names_are_mangled_because_dots_are_illegal(self):
        """Anthropic requires ^[a-zA-Z0-9_-]{1,64}$ — our names are dotted."""
        p, t = self._provider([(200, _msg(text="ok"))])
        p.complete([{"role": "user", "content": "go"}],
                   [CATALOG["github.list_merged_prs"]])
        names = [x["name"] for x in t.sent[0]["body"]["tools"]]
        self.assertEqual(names, ["github__list_merged_prs"])

    def test_tool_call_is_unmangled_on_the_way_back(self):
        p, _ = self._provider([(200, _msg(tool="work_item__append_note",
                                          args={"item_id": "X"}))])
        c = p.complete([{"role": "user", "content": "go"}], [])
        self.assertEqual(c.tool_call.name, "work_item.append_note")
        self.assertEqual(c.tool_call.call_id, "toolu_1")

    def test_usage_is_accounted_for_cost_attribution(self):
        p, _ = self._provider([(200, _msg(text="a", tokens=(120, 40))),
                               (200, _msg(text="b", tokens=(80, 20)))])
        p.complete([{"role": "user", "content": "1"}], [])
        p.complete([{"role": "user", "content": "2"}], [])
        self.assertEqual(p.total_input_tokens, 200)
        self.assertEqual(p.total_output_tokens, 60)

    def test_tool_results_are_user_messages_with_tool_result_blocks(self):
        """The API detail that costs an afternoon if you do not know it."""
        p, t = self._provider([(200, _msg(text="done"))])
        p.complete([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None,
             "tool_call": {"name": "work_item.read", "arguments": {"item_id": "X"},
                           "call_id": "toolu_9"}},
            {"role": "tool", "tool_use_id": "toolu_9", "content": "{}"},
        ], [])
        msgs = t.sent[0]["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[2]["content"][0]["type"], "tool_result")
        self.assertEqual(msgs[2]["content"][0]["tool_use_id"], "toolu_9")
        self.assertEqual(msgs[1]["content"][0]["id"], "toolu_9")


class TestSchemaCannotLeakACommitmentField(unittest.TestCase):
    def test_no_generated_schema_exposes_a_commitment_field(self):
        """The model is never shown a field it cannot use — audit of all tools."""
        for name, tool in CATALOG.items():
            props = set(tool_schema(tool)["input_schema"]["properties"])
            offending = {p for p in props if p.lower() in COMMITMENT_FIELDS}
            self.assertEqual(offending, set(), f"{name} exposes {offending}")

    def test_a_github_patch_tool_cannot_be_registered(self):
        """The obvious triage tool, refused at import. This is wall 1."""
        for fields in (frozenset({"repo", "number", "assignees"}),
                       frozenset({"repo", "number", "state"}),
                       frozenset({"repo", "number", "milestone"})):
            with self.subTest(fields=sorted(fields)):
                with self.assertRaises(ProhibitedCapability):
                    register(Tool(name=f"github.patch_issue_{sorted(fields)[0]}",
                                  description="tidy the backlog",
                                  parameters=fields, handler=lambda **kw: None,
                                  min_tier=RiskTier.T2_SYSTEM_OF_RECORD,
                                  writes=True, reversible=True))

    def test_describing_an_undeclared_parameter_is_refused(self):
        with self.assertRaises(ProhibitedCapability):
            register(Tool(name="x.y", description="d",
                          parameters=frozenset({"a"}),
                          param_types={"a": "string", "b": "string"},
                          handler=lambda **kw: None))


class TestGitHubTools(unittest.TestCase):
    def test_list_merged_prs_builds_a_search_query(self):
        t = FakeTransport([(200, {"items": [
            {"number": 7, "title": "Fix leak", "user": {"login": "nadia"},
             "closed_at": "2026-07-28T10:00:00Z",
             "html_url": "https://github.com/o/r/pull/7"}]})])
        github.configure(GitHub(token="tok", client=Client(t, max_retries=0)))
        rows = CATALOG["github.list_merged_prs"].invoke(
            repo="o/r", since="2026-07-20").output
        self.assertIn("q=repo%3Ao/r%20is%3Apr%20is%3Amerged%20merged%3A%3E%3D2026-07-20",
                      t.sent[0]["url"])
        self.assertEqual(t.sent[0]["headers"]["authorization"], "Bearer tok")
        self.assertEqual(rows[0], {"number": 7, "title": "Fix leak",
                                   "author": "@nadia",
                                   "merged_at": "2026-07-28T10:00:00Z",
                                   "url": "https://github.com/o/r/pull/7"})

    def test_read_issue_surfaces_owner_and_state_as_readable_not_writable(self):
        t = FakeTransport([(200, {
            "number": 3, "title": "T", "state": "open",
            "assignees": [{"login": "lin"}], "milestone": {"title": "M1"},
            "labels": [{"name": "bug"}], "body": "b",
            "html_url": "https://github.com/o/r/issues/3"})])
        github.configure(GitHub(token="tok", client=Client(t, max_retries=0)))
        out = CATALOG["github.read_issue"].invoke(repo="o/r", number=3).output
        self.assertEqual(out["assignees"], ["@lin"])
        self.assertEqual(out["state"], "open")
        # readable, but no tool anywhere can set them
        self.assertNotIn("assignees", CATALOG["github.comment"].parameters)
        self.assertNotIn("state", CATALOG["github.comment"].parameters)

    def test_comment_is_staged_not_posted(self):
        t = FakeTransport([])  # any HTTP call here would raise IndexError
        github.configure(GitHub(token="tok", client=Client(t, max_retries=0)))
        result = CATALOG["github.comment"].invoke(
            repo="o/r", number=3, body="hi", authored_by="@karim")
        self.assertEqual(t.sent, [], "the tool handler must not touch the network")
        self.assertEqual(result.effects[0].kind, "github.comment")

    def test_apply_posts_and_inverse_deletes(self):
        t = FakeTransport([(201, {"id": 991}), (204, {})])
        github.configure(GitHub(token="tok", client=Client(t, max_retries=0)))
        from keel.capabilities.catalog import apply_effect, apply_inverse

        inverse = apply_effect("github.comment",
                               {"repo": "o/r", "number": 3, "body": "hi",
                                "authored_by": "@karim"})
        self.assertEqual(t.sent[0]["method"], "POST")
        self.assertIn("/repos/o/r/issues/3/comments", t.sent[0]["url"])
        # attribution is added by the platform, not composed by the model
        self.assertIn("on behalf of @karim", t.sent[0]["body"]["body"])
        self.assertEqual(inverse, {"repo": "o/r", "comment_id": 991})

        apply_inverse("github.comment", inverse)
        self.assertEqual(t.sent[1]["method"], "DELETE")
        self.assertIn("/repos/o/r/issues/comments/991", t.sent[1]["url"])

    def test_missing_token_fails_loudly_with_the_scopes_needed(self):
        github.configure(GitHub(token="", client=Client(FakeTransport([]))))
        with self.assertRaises(RuntimeError) as ctx:
            CATALOG["github.read_issue"].invoke(repo="o/r", number=1)
        self.assertIn("Issues: write", str(ctx.exception))


class TestEndToEndAgainstFakeAPIs(unittest.TestCase):
    """The whole platform, real code paths, fake sockets."""

    def _run(self, responses):
        t = FakeTransport(responses)
        client = Client(t, max_retries=0)
        github.configure(GitHub(token="tok", client=client))
        provider = AnthropicProvider(api_key="k", client=client)
        tmp = pathlib.Path(tempfile.mkdtemp())
        store, audit = st.Store(tmp / "t.db"), AuditLog(tmp / "t.jsonl")
        ex = Executor(Registry.load(REGISTRY), store, provider, audit)
        return ex, store, t

    def test_gated_github_write_is_staged_then_posted_on_approval(self):
        ex, store, t = self._run([
            # 1. model asks to comment
            (200, _msg(tool="github__comment",
                       args={"repo": "o/r", "number": 3, "body": "Rebased.",
                             "authored_by": "@karim"})),
            # 2. model finishes
            (200, _msg(text="Comment staged for review.")),
            # 3. the POST, only after approval
            (201, {"id": 42}),
        ])
        ex.submit("pr-note-carrier", "test", {})
        outcome = ex.drain()[0]

        self.assertEqual(outcome.status, st.AWAITING_REVIEW)
        self.assertEqual([s["method"] for s in t.sent], ["POST", "POST"],
                         "only the two model calls; nothing posted to GitHub yet")

        ReviewGate(store, ex.audit).decide(
            outcome.run_id, GateDecision(True, "@karim", "verbatim"))

        self.assertIn("/issues/3/comments", t.sent[2]["url"])
        self.assertEqual(store.get(outcome.run_id).status, st.COMMITTED)

    def test_rejection_never_touches_github(self):
        ex, store, t = self._run([
            (200, _msg(tool="github__comment",
                       args={"repo": "o/r", "number": 3, "body": "Wrong PR.",
                             "authored_by": "@karim"})),
            (200, _msg(text="staged")),
        ])
        ex.submit("pr-note-carrier", "test", {})
        outcome = ex.drain()[0]
        ReviewGate(store, ex.audit).decide(
            outcome.run_id, GateDecision(False, "@karim", "wrong PR"))

        self.assertEqual(len(t.sent), 2, "no GitHub call was ever made")
        self.assertEqual(store.get(outcome.run_id).status, st.REJECTED)

    def test_rollback_deletes_a_comment_that_was_already_posted(self):
        ex, store, t = self._run([
            (200, _msg(tool="github__comment",
                       args={"repo": "o/r", "number": 3, "body": "oops",
                             "authored_by": "@karim"})),
            (200, _msg(text="staged")),
            (201, {"id": 77}),   # approved and posted
            (204, {}),           # then rolled back
        ])
        ex.submit("pr-note-carrier", "test", {})
        outcome = ex.drain()[0]
        g = ReviewGate(store, ex.audit)
        g.decide(outcome.run_id, GateDecision(True, "@karim", "ok"))

        self.assertEqual(rollback(store, ex.audit, outcome.run_id), 1)
        self.assertEqual(t.sent[-1]["method"], "DELETE")
        self.assertIn("/issues/comments/77", t.sent[-1]["url"])

    def test_the_model_is_only_offered_tools_the_agent_was_granted(self):
        ex, store, t = self._run([(200, _msg(text="nothing to do"))])
        ex.submit("repo-weekly-digest", "test", {})
        ex.drain()
        offered = {x["name"] for x in t.sent[0]["body"]["tools"]}
        self.assertEqual(offered, {"github__list_merged_prs", "draft__save"})
        self.assertNotIn("github__comment", offered)

    def test_an_injected_commitment_field_dies_at_the_capability_layer(self):
        """The real integration version of the demo's step 5."""
        ex, store, t = self._run([
            (200, _msg(tool="github__comment",
                       args={"repo": "o/r", "number": 3, "body": "reassigning",
                             "authored_by": "@karim",
                             "assignees": ["lin"], "state": "closed"})),
        ])
        ex.submit("pr-note-carrier", "test", {})
        outcome = ex.drain()[0]

        self.assertEqual(outcome.status, st.ROLLED_BACK)
        self.assertIn("undeclared argument", outcome.error or "")
        self.assertEqual(len(t.sent), 1, "no GitHub call was attempted")


if __name__ == "__main__":
    unittest.main()
