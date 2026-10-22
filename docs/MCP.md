# Keel over MCP

An MCP server exposing the platform to any Model Context Protocol client.
JSON-RPC 2.0 over stdio, no dependencies.

```bash
python -m keel.mcp
```

Register it with a client by pointing at that command, e.g.:

```json
{
  "mcpServers": {
    "keel": {
      "command": "python",
      "args": ["-m", "keel.mcp"],
      "cwd": "/srv/keel",
      "env": {"KEEL_PROVIDER": "anthropic"}
    }
  }
}
```

## What is exposed

| Tool | Reads | Writes |
|---|---|---|
| `keel_list_agents` | registry: owner, tier, autonomy, grants, gate | — |
| `keel_submit_run` | — | queues a run |
| `keel_run_status` | run status, staged vs applied effect counts | — |
| `keel_run_log` | the full audit chain for a run, plus chain integrity | — |
| `keel_pending_approvals` | what is waiting on a human, and exactly what it would do | — |

## What is deliberately not exposed

**There is no `keel_approve`, and there is no `keel_reject`.**

An MCP client is a model. If a model could approve a run, the human review
gate would be a formality an agent satisfies on its own behalf, and every
guarantee downstream of it would be void — the staged effects, the atomic
rollback, the named reviewer in the audit chain. All of them assume a person
decided.

This is the same argument as the capability walls, one level up. A protocol
endpoint that lets a model approve is not a weaker gate; it is the absence of
one.

Rejection is excluded for the mirror reason: a client that can reject can
suppress work it dislikes without a human ever seeing that it existed.

So the surface is **read, or submit-for-review**. A client can start work and
watch it. It cannot finish it. Approval happens where a person types it:

```bash
keel review approve <run_id> --reviewer "@you"
```

Asking for it over MCP returns an error that says so, rather than a bare
"unknown tool" — a client that wanted to approve should learn why it cannot.

## Submission is safe to expose

Queuing a run is not a privileged action, because nothing downstream of it is
weakened:

- the agent must be in the registry, or `keel_submit_run` refuses
- its capability set is still built from its grants and nothing else
- a gated agent still stages its effects and stops
- every prompt and tool call still lands in the audit chain, tagged
  `trigger: mcp`, so a run started by a client is distinguishable from one
  started by cron

## The prohibition applies here too

No MCP tool may declare `owner`, `status`, `due_date`, `priority` or any other
commitment field. This is enforced by
`keel.capabilities.base.assert_no_commitment_fields` — the *same function* the
tool catalog uses, not a second copy of the rule.

`register_mcp` calls it at import, so a pull request adding such an endpoint
fails at collection in CI. `tests/test_mcp.py::TestProhibitionAppliesToMCP`
asserts both the refusal and that the two surfaces share one implementation,
which is the part that stops them drifting apart in a year.

## Errors

Transport problems are JSON-RPC errors. Domain refusals — an unregistered
agent, an unknown run — come back as a successful call with `isError: true`
and the reason in the content, so a client can read *why* rather than guessing
from a code.
