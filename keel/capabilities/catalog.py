"""The tool catalog.

These are stand-ins for the real integrations (warehouse, chat, issue
tracker, docs). The shape is what matters: each tool declares its writable
surface, its tier, whether it is reversible, and whether it needs a gate.

The interesting one is ``work_item.append_note``. The obvious design for a
work-tracker tool is ``update_issue(id, status=..., owner=..., due=...)``.
That tool cannot exist here — ``register`` rejects it at import. What exists
instead is a tool that appends human-authored text to an item and can do
nothing else. The restriction is in the signature, not in a prompt, so
prompt injection, a confused model and a malicious input all produce the same
outcome: nothing.
"""

from __future__ import annotations

from typing import Any

from ..registry.models import RiskTier
from .base import Effect, Tool, ToolResult, register

# --------------------------------------------------------------------------
# Fake backing stores, so the platform runs end to end with no network.
# --------------------------------------------------------------------------

WAREHOUSE: dict[str, list[dict[str, Any]]] = {
    "merged_prs": [
        {"repo": "supabase/postgrest", "title": "Fix connection pool leak", "author": "@nadia"},
        {"repo": "supabase/auth", "title": "MFA enrollment endpoint", "author": "@karim"},
        {"repo": "supabase/realtime", "title": "Backpressure on broadcast", "author": "@lin"},
    ],
    "incidents": [
        {"id": "INC-204", "summary": "Elevated auth latency, eu-west", "resolved": True},
    ],
}

WORK_ITEMS: dict[str, dict[str, Any]] = {
    "PLAT-19": {
        "title": "Agent platform: durable state",
        "owner": "@nadia",  # readable, never writable by an agent
        "status": "in_progress",
        "notes": [],
    }
}

SENT_MESSAGES: list[dict[str, Any]] = []
PUBLISHED: dict[str, str] = {}


# --------------------------------------------------------------------------
# T0 — read internal data
# --------------------------------------------------------------------------


def _warehouse_query(table: str, limit: int = 50) -> ToolResult:
    rows = WAREHOUSE.get(table, [])[:limit]
    return ToolResult(output=rows, cost_tokens=len(rows) * 8)


warehouse_query = register(
    Tool(
        name="warehouse.query",
        description="Read rows from an internal warehouse table.",
        parameters=frozenset({"table", "limit"}),
        handler=_warehouse_query,
        min_tier=RiskTier.T0_READ_INTERNAL,
        scopes=("merged_prs", "incidents"),
    )
)


def _work_item_read(item_id: str) -> ToolResult:
    return ToolResult(output=dict(WORK_ITEMS.get(item_id, {})))


work_item_read = register(
    Tool(
        name="work_item.read",
        description="Read a work item, including its owner, status and due date.",
        parameters=frozenset({"item_id"}),
        handler=_work_item_read,
        min_tier=RiskTier.T0_READ_INTERNAL,
    )
)


# --------------------------------------------------------------------------
# T1 — draft for a human
# --------------------------------------------------------------------------


def _draft_save(key: str, body: str) -> ToolResult:
    # Pure: describes the change, does not perform it. The executor applies it
    # now for an ungated agent, or the review gate applies it after approval.
    return ToolResult(
        output={"staged": key, "chars": len(body)},
        effects=(
            Effect(kind="draft.save", target=key, payload={"key": key, "body": body}),
        ),
    )


draft_save = register(
    Tool(
        name="draft.save",
        description="Save a draft for a human to edit. Never published directly.",
        parameters=frozenset({"key", "body"}),
        handler=_draft_save,
        min_tier=RiskTier.T1_DRAFT_INTERNAL,
        writes=True,
        reversible=True,
    )
)


# --------------------------------------------------------------------------
# T2 — carry human-authored content into a system of record
# --------------------------------------------------------------------------


def _work_item_append_note(item_id: str, note: str, authored_by: str) -> ToolResult:
    return ToolResult(
        output={"staged_for": item_id},
        effects=(
            Effect(
                kind="work_item.append_note",
                target=item_id,
                payload={"item_id": item_id, "note": note, "authored_by": authored_by},
            ),
        ),
    )


work_item_append_note = register(
    Tool(
        name="work_item.append_note",
        description=(
            "Append a human-authored note to a work item. Cannot set an owner, "
            "a due date, a status or a priority — those parameters do not exist "
            "on this tool and no tool in the catalog has them."
        ),
        # Deliberately narrow. Adding "status" here fails at import in CI.
        parameters=frozenset({"item_id", "note", "authored_by"}),
        handler=_work_item_append_note,
        min_tier=RiskTier.T2_SYSTEM_OF_RECORD,
        writes=True,
        reversible=True,
        requires_human_gate=True,
    )
)


# --------------------------------------------------------------------------
# T3 — initiate contact with a human
# --------------------------------------------------------------------------


def _notify_send(recipient: str, subject: str, body: str) -> ToolResult:
    return ToolResult(
        output={"staged_for": recipient},
        effects=(
            Effect(
                kind="notify.send",
                target=recipient,
                payload={"to": recipient, "subject": subject, "body": body},
            ),
        ),
    )


notify_send = register(
    Tool(
        name="notify.send",
        description="Contact a person. Spends from their interruption budget.",
        parameters=frozenset({"recipient", "subject", "body"}),
        handler=_notify_send,
        min_tier=RiskTier.T3_EXTERNAL_FACING,
        writes=True,
        reversible=True,
        requires_human_gate=True,
        external=True,
    )
)


# --------------------------------------------------------------------------
# Apply / un-apply. Tool handlers describe changes; these perform them.
#
# The split is what makes the human gate real. A gated agent's effects sit in
# the journal in state "staged" and this function is never called on them
# until a named human approves. "Held for review" means the write has not
# happened, not that it happened and can be undone.
# --------------------------------------------------------------------------


#: Effect kind -> (apply, un-apply). Modules that add tools register their
#: own effect handlers here, so `keel/capabilities/github.py` does not have to
#: edit this file. An effect with no registered applier cannot be journalled.
EFFECT_HANDLERS: dict[str, tuple[Any, Any]] = {}


def register_effect(kind: str, apply_fn, inverse_fn) -> None:
    if kind in EFFECT_HANDLERS:
        raise KeyError(f"duplicate effect handler {kind!r}")
    EFFECT_HANDLERS[kind] = (apply_fn, inverse_fn)


def apply_effect(effect_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Perform the change and return the inverse that undoes it."""
    try:
        apply_fn, _ = EFFECT_HANDLERS[effect_kind]
    except KeyError:
        raise KeyError(f"no apply handler for effect kind {effect_kind!r}") from None
    return apply_fn(payload)


def apply_inverse(effect_kind: str, inverse: dict[str, Any]) -> None:
    try:
        _, inverse_fn = EFFECT_HANDLERS[effect_kind]
    except KeyError:
        raise KeyError(f"no inverse handler for effect kind {effect_kind!r}") from None
    inverse_fn(inverse)


# ---- in-memory demo effects ----------------------------------------------


def _apply_draft_save(p: dict[str, Any]) -> dict[str, Any]:
    key = p["key"]
    previous = PUBLISHED.get(f"draft:{key}")
    PUBLISHED[f"draft:{key}"] = p["body"]
    return {"key": key, "body": previous}


def _inverse_draft_save(inv: dict[str, Any]) -> None:
    key, body = inv["key"], inv["body"]
    if body is None:
        PUBLISHED.pop(f"draft:{key}", None)
    else:
        PUBLISHED[f"draft:{key}"] = body


def _apply_append_note(p: dict[str, Any]) -> dict[str, Any]:
    item = WORK_ITEMS.setdefault(p["item_id"], {"title": p["item_id"], "notes": []})
    index = len(item["notes"])
    item["notes"].append({"note": p["note"], "authored_by": p["authored_by"]})
    return {"item_id": p["item_id"], "note_index": index}


def _inverse_append_note(inv: dict[str, Any]) -> None:
    notes = WORK_ITEMS[inv["item_id"]]["notes"]
    del notes[inv["note_index"]:]


def _apply_notify(p: dict[str, Any]) -> dict[str, Any]:
    SENT_MESSAGES.append(dict(p))
    return {"index": len(SENT_MESSAGES) - 1}


def _inverse_notify(inv: dict[str, Any]) -> None:
    i = inv["index"]
    if i < len(SENT_MESSAGES):
        SENT_MESSAGES[i]["retracted"] = True  # retraction, not deletion


register_effect("draft.save", _apply_draft_save, _inverse_draft_save)
register_effect("work_item.append_note", _apply_append_note, _inverse_append_note)
register_effect("notify.send", _apply_notify, _inverse_notify)


from .base import CATALOG  # noqa: E402,F401  re-exported
