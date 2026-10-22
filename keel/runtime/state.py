"""Durable run state.

A run is a row, and every step it completes is a row committed before the
next step begins. If the process dies mid-run, the rows that exist are
exactly the steps that finished. ``resume_candidates`` finds runs that were
in flight and hands them back to the executor, which replays from the last
committed step rather than from the beginning.

SQLite here for a self-contained repo; the schema is deliberately boring so
it ports to Postgres or the warehouse without a rewrite.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    tier         INTEGER NOT NULL,
    trigger      TEXT NOT NULL,
    input        TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    attempt      INTEGER NOT NULL DEFAULT 1,
    error        TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS journal (
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    effect_kind  TEXT NOT NULL,
    target       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    inverse      TEXT,
    state        TEXT NOT NULL,           -- staged | applied | reversed
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS contact_ledger (
    person     TEXT NOT NULL,
    sent_at    REAL NOT NULL,
    run_id     TEXT NOT NULL,
    subject    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS value_ledger (
    run_id       TEXT NOT NULL,
    agent_id     TEXT NOT NULL,
    minutes_saved REAL NOT NULL,
    basis        TEXT NOT NULL,
    recorded_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
"""

QUEUED = "queued"
RUNNING = "running"
AWAITING_REVIEW = "awaiting_review"
COMMITTED = "committed"
ROLLED_BACK = "rolled_back"
FAILED = "failed"
REJECTED = "rejected"


@dataclass
class Run:
    run_id: str
    agent_id: str
    tier: int
    trigger: str
    input: dict[str, Any]
    status: str
    attempt: int = 1
    error: str | None = None


class Store:
    def __init__(self, path: str | pathlib.Path = "keel.db"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    # ---------------- runs ----------------

    def enqueue(self, agent_id: str, tier: int, trigger: str, payload: dict) -> str:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        now = time.time()
        self.conn.execute(
            "INSERT INTO runs (run_id, agent_id, tier, trigger, input, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, agent_id, tier, trigger, json.dumps(payload), QUEUED, now, now),
        )
        return run_id

    def claim_next(self) -> Run | None:
        """Atomically move one queued run to running and return it."""
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM runs WHERE status=? ORDER BY created_at LIMIT 1",
                (QUEUED,),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE run_id=? AND status=?",
                (RUNNING, time.time(), row["run_id"], QUEUED),
            )
        return self._to_run(row, status=RUNNING)

    def get(self, run_id: str) -> Run | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return self._to_run(row) if row else None

    def set_status(self, run_id: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?, error=?, updated_at=? WHERE run_id=?",
            (status, error, time.time(), run_id),
        )

    def list_runs(self, status: str | None = None) -> list[Run]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs ORDER BY created_at"
            ).fetchall()
        return [self._to_run(r) for r in rows]

    def resume_candidates(self) -> list[Run]:
        """Runs that were mid-flight when the process stopped.

        Called on executor start. These are not restarted from zero: the
        executor reads their committed steps and continues after the last one.
        """
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE status=? ORDER BY created_at", (RUNNING,)
        ).fetchall()
        return [self._to_run(r) for r in rows]

    @staticmethod
    def _to_run(row: sqlite3.Row, status: str | None = None) -> Run:
        return Run(
            run_id=row["run_id"],
            agent_id=row["agent_id"],
            tier=row["tier"],
            trigger=row["trigger"],
            input=json.loads(row["input"]),
            status=status or row["status"],
            attempt=row["attempt"],
            error=row["error"],
        )

    # ---------------- steps ----------------

    def record_step(self, run_id: str, kind: str, payload: dict) -> int:
        """Commit one step. Durability boundary: this returns only once written."""
        with self.conn:
            seq = self.next_seq(run_id)
            self.conn.execute(
                "INSERT INTO steps (run_id, seq, kind, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                (run_id, seq, kind, json.dumps(payload, default=str), time.time()),
            )
        return seq

    def next_seq(self, run_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM steps WHERE run_id=?", (run_id,)
        ).fetchone()
        return row["m"] + 1

    def steps(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [
            {
                "seq": r["seq"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"]),
                "at": r["created_at"],
            }
            for r in rows
        ]

    # ---------------- journal ----------------

    def record_effect(
        self, run_id: str, seq: int, kind: str, target: str, payload: dict,
        inverse: dict | None, state: str = "staged",
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO journal (run_id, seq, effect_kind, target,"
            " payload, inverse, state) VALUES (?,?,?,?,?,?,?)",
            (
                run_id, seq, kind, target,
                json.dumps(payload, default=str),
                json.dumps(inverse, default=str) if inverse is not None else None,
                state,
            ),
        )

    def effects(self, run_id: str, state: str | None = None) -> list[dict]:
        if state:
            rows = self.conn.execute(
                "SELECT * FROM journal WHERE run_id=? AND state=? ORDER BY seq",
                (run_id, state),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM journal WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        return [
            {
                "seq": r["seq"],
                "kind": r["effect_kind"],
                "target": r["target"],
                "payload": json.loads(r["payload"]),
                "inverse": json.loads(r["inverse"]) if r["inverse"] else None,
                "state": r["state"],
            }
            for r in rows
        ]

    def set_effect_state(
        self, run_id: str, seq: int, state: str, inverse: dict | None = None
    ) -> None:
        if inverse is None:
            self.conn.execute(
                "UPDATE journal SET state=? WHERE run_id=? AND seq=?",
                (state, run_id, seq),
            )
        else:
            self.conn.execute(
                "UPDATE journal SET state=?, inverse=? WHERE run_id=? AND seq=?",
                (state, json.dumps(inverse, default=str), run_id, seq),
            )

    # ---------------- ledgers ----------------

    def record_contact(
        self, person: str, run_id: str, subject: str, now: float | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO contact_ledger (person, sent_at, run_id, subject)"
            " VALUES (?,?,?,?)",
            (person, now if now is not None else time.time(), run_id, subject),
        )

    def contacts_since(self, person: str, since: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM contact_ledger WHERE person=? AND sent_at>=?",
            (person, since),
        ).fetchone()
        return row["c"]

    def record_value(
        self, run_id: str, agent_id: str, minutes_saved: float, basis: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO value_ledger (run_id, agent_id, minutes_saved, basis,"
            " recorded_at) VALUES (?,?,?,?,?)",
            (run_id, agent_id, minutes_saved, basis, time.time()),
        )

    def value_by_agent(self) -> list[tuple[str, float, int]]:
        rows = self.conn.execute(
            "SELECT agent_id, SUM(minutes_saved) AS m, COUNT(*) AS n"
            " FROM value_ledger GROUP BY agent_id ORDER BY m DESC"
        ).fetchall()
        return [(r["agent_id"], r["m"], r["n"]) for r in rows]

    def close(self) -> None:
        self.conn.close()
