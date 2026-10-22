"""Providers: the only place that knows what a model vendor looks like.

The executor speaks a neutral message format — ``{"role", "content"}`` plus
``tool_use_id`` on results — and hands the provider the granted ``Tool``
objects. Everything vendor-shaped (tool schemas, block types, usage
accounting, name mangling) lives below this line.

A property worth stating explicitly, because it is the third wall doing real
work: **the tool schema sent to the model is generated from
``Tool.parameters``.** The model is never shown a field it cannot use. There
is no "here is the schema, please do not set owner" instruction anywhere —
``owner`` is not in the JSON schema, not in the system prompt, and not in the
dispatch signature. A model cannot be talked into using an affordance it was
never given.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..capabilities.base import Tool
from .http import Client, Transport

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

#: Anthropic tool names must match ^[a-zA-Z0-9_-]{1,64}$ — no dots. Our names
#: are dotted (``work_item.append_note``) because that reads better in the
#: registry and the audit log, so we mangle on the way out and unmangle on the
#: way back. Documented so nobody rediscovers it at 2am.
_NAME_OK = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _mangle(name: str) -> str:
    out = name.replace(".", "__")
    if not _NAME_OK.match(out):
        raise ValueError(f"tool name {name!r} cannot be encoded for the API")
    return out


def _unmangle(name: str) -> str:
    return name.replace("__", ".")


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str | None = None


@dataclass
class Completion:
    text: str | None = None
    tool_call: ToolCall | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "unknown"
    stop_reason: str | None = None
    #: Verbatim prompt sent to the provider, kept so a run can be reconstructed.
    prompt_digest: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Provider(Protocol):
    name: str

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[Tool]) -> Completion: ...


def tool_schema(tool: Tool) -> dict[str, Any]:
    """JSON schema for one tool, derived from its declared parameters.

    Nothing here is hand-written per tool, which is the point: the schema
    cannot drift from the enforcement surface, because it is generated from it.
    """
    props = {p: {"type": tool.param_types.get(p, "string")}
             for p in sorted(tool.parameters)}
    return {
        "name": _mangle(tool.name),
        "description": tool.description,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": sorted(tool.required),
            "additionalProperties": False,
        },
    }


class AnthropicProvider:
    """Real calls to the Messages API.

    Model choice is config, not code. ``max_tokens`` is capped low on purpose:
    these agents write digests and notes, and a runaway generation is a cost
    incident before it is a quality one.
    """

    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-5-5", *,
                 api_key: str | None = None, client: Client | None = None,
                 transport: Transport | None = None, max_tokens: int = 1024):
        self.model, self.max_tokens = model, max_tokens
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it, pass api_key=..., or "
                "use ScriptedProvider for offline runs.")
        self.client = client or Client(transport)
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def complete(self, messages, tools) -> Completion:
        system, turns = _split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": _to_anthropic(turns),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [tool_schema(t) for t in tools]

        data = self.client.request(
            "POST", ANTHROPIC_URL,
            headers={"x-api-key": self.api_key,
                     "anthropic-version": ANTHROPIC_VERSION},
            json_body=payload)

        completion = _from_anthropic(data, messages)
        self.total_input_tokens += completion.input_tokens
        self.total_output_tokens += completion.output_tokens
        return completion


def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    return system, [m for m in messages if m["role"] != "system"]


def _to_anthropic(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate neutral turns into Anthropic content blocks.

    A tool result is a *user* message containing a tool_result block — one of
    those API details that is obvious once you know it and costs an afternoon
    if you do not.
    """
    out: list[dict[str, Any]] = []
    for m in turns:
        if m["role"] == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_use_id") or "unknown",
                "content": str(m["content"]),
            }]})
        elif m["role"] == "assistant" and m.get("tool_call"):
            call = m["tool_call"]
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            blocks.append({"type": "tool_use",
                           "id": call.get("call_id") or "unknown",
                           "name": _mangle(call["name"]),
                           "input": call["arguments"]})
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _from_anthropic(data: dict[str, Any],
                    prompt: list[dict[str, Any]]) -> Completion:
    usage = data.get("usage") or {}
    c = Completion(model=data.get("model", "unknown"),
                   stop_reason=data.get("stop_reason"),
                   input_tokens=usage.get("input_tokens", 0),
                   output_tokens=usage.get("output_tokens", 0),
                   prompt_digest=prompt)
    texts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use" and c.tool_call is None:
            # One tool call per turn. The loop is sequential by design: a
            # parallel batch is much harder to gate, journal and roll back,
            # and these agents are not latency-bound.
            c.tool_call = ToolCall(name=_unmangle(block["name"]),
                                   arguments=dict(block.get("input") or {}),
                                   call_id=block.get("id"))
    c.text = "".join(texts) or None
    return c


class ScriptedProvider:
    """Replays a fixed sequence. Deterministic, offline, free.

    Unit tests run against this. The evaluation suite no longer does — see
    ``keel/evals/live.py``, which drives the real provider through a recorded
    cassette so the suite grades real model output.
    """

    name = "scripted"

    def __init__(self, script: list[Completion], model: str = "scripted-1"):
        self._script, self._i, self.model = list(script), 0, model
        #: What tools the executor offered on each turn — asserted in tests to
        #: prove the model is never shown an ungranted tool.
        self.seen_tools: list[list[str]] = []

    def complete(self, messages, tools) -> Completion:
        self.seen_tools.append(sorted(t.name for t in tools))
        if self._i >= len(self._script):
            return Completion(text="", model=self.model, prompt_digest=messages)
        c = self._script[self._i]
        self._i += 1
        c.model, c.prompt_digest = self.model, messages
        return c


def provider_from_env(transport: Transport | None = None) -> Provider:
    """Pick a provider from configuration, not from a code change.

    KEEL_PROVIDER=anthropic   real API (needs ANTHROPIC_API_KEY)
    KEEL_PROVIDER=scripted    offline, default
    """
    if os.environ.get("KEEL_PROVIDER", "scripted").lower() == "anthropic":
        return AnthropicProvider(
            model=os.environ.get("KEEL_MODEL", "claude-sonnet-5-5"),
            transport=transport)
    return ScriptedProvider([])
