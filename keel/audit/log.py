"""Append-only, hash-chained decision log.

Every prompt, tool call, tool result, gate decision and rollback is written
here before the next thing happens. Each entry carries the hash of the entry
before it, so a deleted or edited line is detectable by ``verify()`` rather
than by someone noticing the story does not add up.

The chain is the answer to "why did the agent do that". Not a summary of the
run — the run itself, replayable.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import time
from typing import Any

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: str | pathlib.Path = "audit.jsonl"):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _last_hash(self) -> str:
        last = GENESIS
        with self.path.open() as fh:
            for line in fh:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def append(self, run_id: str, kind: str, data: dict[str, Any]) -> str:
        prev = self._last_hash()
        entry = {
            "at": time.time(),
            "run_id": run_id,
            "kind": kind,
            "data": data,
            "prev": prev,
        }
        entry["hash"] = _digest(entry)
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        return entry["hash"]

    def entries(self, run_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        with self.path.open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                e = json.loads(line)
                if run_id is None or e["run_id"] == run_id:
                    out.append(e)
        return out

    def verify(self) -> tuple[bool, str | None]:
        """Walk the chain. Returns (ok, first_broken_hash)."""
        prev = GENESIS
        with self.path.open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                e = json.loads(line)
                if e["prev"] != prev:
                    return False, e["hash"]
                body = {k: e[k] for k in ("at", "run_id", "kind", "data", "prev")}
                if _digest(body) != e["hash"]:
                    return False, e["hash"]
                prev = e["hash"]
        return True, None


def _digest(entry: dict[str, Any]) -> str:
    body = {k: entry[k] for k in ("at", "run_id", "kind", "data", "prev")}
    blob = json.dumps(body, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()
