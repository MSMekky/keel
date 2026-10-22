"""Shared fixtures. Every test gets a fresh store, log and world."""

from __future__ import annotations

import pathlib
import tempfile

from keel.audit.log import AuditLog
from keel.capabilities import catalog
from keel.registry.loader import Registry
from keel.runtime import state as st
from keel.runtime.executor import Executor
from keel.runtime.providers import Completion, ScriptedProvider, ToolCall

ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "registry" / "agents"
SUITES = ROOT / "evals" / "suites"


def fresh_world() -> None:
    catalog.PUBLISHED.clear()
    catalog.SENT_MESSAGES.clear()
    catalog.WORK_ITEMS.clear()
    catalog.WORK_ITEMS["PLAT-19"] = {
        "title": "Agent platform: durable state",
        "owner": "@nadia",
        "status": "in_progress",
        "notes": [],
    }


def harness(script: list[Completion]) -> tuple[Executor, st.Store, Registry]:
    fresh_world()
    tmp = tempfile.mkdtemp()
    store = st.Store(pathlib.Path(tmp) / "t.db")
    audit = AuditLog(pathlib.Path(tmp) / "t.jsonl")
    reg = Registry.load(REGISTRY)
    return Executor(reg, store, ScriptedProvider(script), audit), store, reg


__all__ = ["Completion", "ToolCall", "harness", "fresh_world", "REGISTRY", "SUITES", "ROOT"]
