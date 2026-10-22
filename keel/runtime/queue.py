"""Event routing.

Agents subscribe to triggers — a cron expression or an event name. An event
arriving fans out to every subscribed agent as a separate durable run, so one
agent failing on an event does not affect another agent's handling of it.

The router is intentionally the only way work enters the system. There is no
``executor.run_now(prompt)`` back door, because a back door is where the
registry, the tier and the gate get skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..registry.loader import Registry


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict[str, Any]


class Router:
    def __init__(self, registry: Registry, subscriptions: dict[str, list[str]]):
        self.registry = registry
        self.subscriptions = subscriptions
        for event, agent_ids in subscriptions.items():
            for agent_id in agent_ids:
                self.registry.get(agent_id)  # raises if unregistered

    def dispatch(self, executor, event: Event) -> list[str]:
        run_ids = []
        for agent_id in self.subscriptions.get(event.name, []):
            run_ids.append(
                executor.submit(agent_id, trigger=event.name, payload=event.payload)
            )
        return run_ids


DEFAULT_SUBSCRIPTIONS = {
    "cron.weekly.monday_0800": ["exec-weekly-digest"],
    "github.pull_request.opened": ["pr-context-linter"],
    "chat.question.asked": ["platform-qa"],
    "cron.daily.0900": ["standup-drafter"],
}
