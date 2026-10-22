"""Real GitHub tools.

This is where the governance claim stops being a thought experiment. GitHub's
REST API *does* expose the forbidden write:

    PATCH /repos/{owner}/{repo}/issues/{number}
        { "assignees": [...], "state": "closed", "milestone": 4, "labels": [...] }

One call sets an owner, a status and a milestone. It is well documented, it is
one line of code, and every issue-triage agent anyone has ever demoed uses it.

**This module does not wrap it.** There is no function here that issues a
PATCH to an issue. The only write is a comment, and ``register`` would refuse
the PATCH tool anyway the moment it declared ``assignees`` or ``state`` as a
parameter. So the forbidden action is absent at three levels: no wrapper, no
registrable schema, no granted capability.

That is the code-level wall. In deployment there is a second one, and it is
the stronger of the two — see ``docs/RUNNING.md``: the fine-grained personal
access token this module authenticates with is issued with **Issues: write**
only for comments and **Contents: read**, and *without* the permissions that
back the PATCH. An audit of the credential then shows the process cannot
perform the write even if this file were replaced wholesale.

Wall in code fails in CI. Wall in the token fails at GitHub's edge. Both.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from ..registry.models import RiskTier
from ..runtime.http import Client, Transport
from .base import Effect, Tool, ToolResult, register
from .catalog import register_effect

API = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
GH_VERSION = "2022-11-28"


class GitHub:
    """Thin authenticated wrapper. One instance per process, set at startup."""

    def __init__(self, token: str | None = None, *, client: Client | None = None,
                 transport: Transport | None = None):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.client = client or Client(transport)

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise RuntimeError(
                "GITHUB_TOKEN is not set. Create a fine-grained PAT with "
                "Contents: read and Issues: write, and export it. See "
                "docs/RUNNING.md for the exact scopes and why they are that narrow.")
        return {"authorization": f"Bearer {self.token}",
                "accept": ACCEPT,
                "x-github-api-version": GH_VERSION,
                "user-agent": "keel-agent-platform"}

    def get(self, path: str) -> Any:
        return self.client.request("GET", f"{API}{path}", headers=self._headers())

    def post(self, path: str, body: Any) -> Any:
        return self.client.request("POST", f"{API}{path}",
                                   headers=self._headers(), json_body=body)

    def delete(self, path: str) -> Any:
        return self.client.request("DELETE", f"{API}{path}", headers=self._headers())


#: Module-level handle. Tests and the CLI swap this; nothing else touches it.
_gh: GitHub | None = None


def configure(gh: GitHub) -> None:
    global _gh
    _gh = gh


def client() -> GitHub:
    global _gh
    if _gh is None:
        _gh = GitHub()
    return _gh


# ---------------------------------------------------------------------------
# T0 — reads
# ---------------------------------------------------------------------------


def _list_merged_prs(repo: str, since: str, limit: int = 30) -> ToolResult:
    """Merged pull requests since an ISO date.

    Uses the search API rather than /pulls because /pulls cannot filter on
    merge date and would need client-side paging through every closed PR — a
    rate-limit problem on any busy repository.
    """
    q = urllib.parse.quote(f"repo:{repo} is:pr is:merged merged:>={since}")
    data = client().get(f"/search/issues?q={q}&per_page={int(limit)}&sort=updated")
    rows = [{
        "number": i["number"],
        "title": i["title"],
        "author": "@" + i["user"]["login"],
        "merged_at": i.get("closed_at"),
        "url": i["html_url"],
    } for i in data.get("items", [])]
    return ToolResult(output=rows)


register(Tool(
    name="github.list_merged_prs",
    description=("List pull requests merged into a repository since a date. "
                 "Returns number, title, author and merge time."),
    parameters=frozenset({"repo", "since", "limit"}),
    param_types={"repo": "string", "since": "string", "limit": "integer"},
    required=frozenset({"repo", "since"}),
    handler=_list_merged_prs,
    min_tier=RiskTier.T0_READ_INTERNAL,
))


def _read_issue(repo: str, number: int) -> ToolResult:
    i = client().get(f"/repos/{repo}/issues/{int(number)}")
    # Owner, state and milestone are returned. An agent may *read* a
    # commitment and report on it; it simply has no way to write one.
    return ToolResult(output={
        "number": i["number"],
        "title": i["title"],
        "state": i["state"],
        "assignees": ["@" + a["login"] for a in i.get("assignees", [])],
        "milestone": (i.get("milestone") or {}).get("title"),
        "labels": [lbl["name"] for lbl in i.get("labels", [])],
        "body": (i.get("body") or "")[:4000],
        "url": i["html_url"],
    })


register(Tool(
    name="github.read_issue",
    description="Read one issue or pull request, including its current owner, "
                "state, milestone and labels.",
    parameters=frozenset({"repo", "number"}),
    param_types={"repo": "string", "number": "integer"},
    required=frozenset({"repo", "number"}),
    handler=_read_issue,
    min_tier=RiskTier.T0_READ_INTERNAL,
))


# ---------------------------------------------------------------------------
# T2 — the only write, and it is a comment
# ---------------------------------------------------------------------------


def _comment(repo: str, number: int, body: str, authored_by: str) -> ToolResult:
    """Stage a comment. Pure — the POST happens in the effect applier, after
    the human gate, so a held run has genuinely not touched the repository."""
    return ToolResult(
        output={"staged_for": f"{repo}#{number}"},
        effects=(Effect(
            kind="github.comment",
            target=f"{repo}#{number}",
            payload={"repo": repo, "number": int(number), "body": body,
                     "authored_by": authored_by},
        ),),
    )


register(Tool(
    name="github.comment",
    description=(
        "Post a human-authored comment on an issue or pull request. This tool "
        "cannot assign anyone, close or reopen anything, set a milestone, or "
        "change labels — those parameters do not exist on it, and no tool in "
        "this platform has them."
    ),
    # Adding "assignees" or "state" here raises ProhibitedCapability at import.
    parameters=frozenset({"repo", "number", "body", "authored_by"}),
    param_types={"repo": "string", "number": "integer",
                 "body": "string", "authored_by": "string"},
    required=frozenset({"repo", "number", "body", "authored_by"}),
    handler=_comment,
    min_tier=RiskTier.T2_SYSTEM_OF_RECORD,
    writes=True, reversible=True, requires_human_gate=True,
))


def _apply_comment(p: dict[str, Any]) -> dict[str, Any]:
    # Attribution is appended by the platform, not composed by the model, so
    # an agent cannot post as though it were a person.
    body = f"{p['body']}\n\n---\n*Posted by keel on behalf of {p['authored_by']}.*"
    created = client().post(
        f"/repos/{p['repo']}/issues/{p['number']}/comments", {"body": body})
    return {"repo": p["repo"], "comment_id": created["id"]}


def _inverse_comment(inv: dict[str, Any]) -> None:
    # A real undo: GitHub lets us delete a comment we created. Rollback of a
    # posted comment therefore leaves no trace, unlike a notification.
    client().delete(f"/repos/{inv['repo']}/issues/comments/{inv['comment_id']}")


register_effect("github.comment", _apply_comment, _inverse_comment)
