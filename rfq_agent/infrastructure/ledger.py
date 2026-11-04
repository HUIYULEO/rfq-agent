"""Append-only operational ledger for RFQ agent runs.

The ledger is deliberately separate from pricing policy.  It records what the
system observed and decided, but never promotes feedback into a pricing rule.
Callers may later build reviewed datasets and evaluations from these events.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4


TASK_SNAPSHOT = "task_snapshot"
SOURCE_EVIDENCE = "source_evidence"
AGENT_TRACE = "agent_trace"
RULE_DECISION = "rule_decision"
HUMAN_DECISION = "human_decision"
ARTIFACT = "artifact"
OUTCOME_FEEDBACK = "outcome_feedback"


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS operational_event (
    sequence          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL UNIQUE,
    task_id           TEXT NOT NULL,
    revision          INTEGER NOT NULL CHECK (revision > 0),
    event_type        TEXT NOT NULL,
    occurred_at       TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    policy_version    TEXT NOT NULL DEFAULT '',
    data_snapshot_id  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_operational_event_task_sequence
    ON operational_event(task_id, sequence);
CREATE INDEX IF NOT EXISTS ix_operational_event_task_type_sequence
    ON operational_event(task_id, event_type, sequence);

CREATE TRIGGER IF NOT EXISTS operational_event_no_update
BEFORE UPDATE ON operational_event
BEGIN
    SELECT RAISE(ABORT, 'operational_event is append-only');
END;

CREATE TRIGGER IF NOT EXISTS operational_event_no_delete
BEFORE DELETE ON operational_event
BEGIN
    SELECT RAISE(ABORT, 'operational_event is append-only');
END;
"""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _json_payload(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise TypeError("event payload must be a mapping")
    # Round-trip before inserting so failed serialization cannot leave a row.
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class OperationalLedger:
    """Small SQLite event ledger with database-enforced immutability."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self._lock:
            self.connection.executescript(_SCHEMA)
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> "OperationalLedger":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def append_event(
        self,
        *,
        task_id: str,
        revision: int,
        event_type: str,
        payload: Mapping[str, Any],
        policy_version: str = "",
        data_snapshot_id: str = "",
        occurred_at: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one immutable event and return its decoded representation."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id is required")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("revision must be a positive integer")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type is required")

        encoded = _json_payload(payload)
        identity = event_id or uuid4().hex
        timestamp = occurred_at or _utc_now()
        with self._lock:
            with self.connection:
                cursor = self.connection.execute(
                    """INSERT INTO operational_event(
                           event_id, task_id, revision, event_type, occurred_at,
                           payload_json, policy_version, data_snapshot_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (identity, task_id, revision, event_type, timestamp, encoded,
                     policy_version or "", data_snapshot_id or ""),
                )
        return {
            "sequence": cursor.lastrowid,
            "event_id": identity,
            "task_id": task_id,
            "revision": revision,
            "event_type": event_type,
            "occurred_at": timestamp,
            "payload": json.loads(encoded),
            "policy_version": policy_version or "",
            "data_snapshot_id": data_snapshot_id or "",
        }

    def events_for_task(
        self,
        task_id: str,
        *,
        event_type: str | None = None,
        after_sequence: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read a task's events in the exact order in which they were appended."""
        clauses = ["task_id = ?"]
        params: list[Any] = [task_id]
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if after_sequence is not None:
            clauses.append("sequence > ?")
            params.append(after_sequence)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM operational_event WHERE " + " AND ".join(clauses) +
                " ORDER BY sequence ASC",
                params,
            ).fetchall()
        return [self._decode(row) for row in rows]

    def record_task_snapshot(self, task: Mapping[str, Any] | Any) -> dict[str, Any]:
        """Record the full versioned task state used for an agent decision."""
        data = task.to_dict() if hasattr(task, "to_dict") else dict(task)
        task_id = data.get("task_id")
        revision = data.get("revision")
        return self.append_event(
            task_id=task_id,
            revision=revision,
            event_type=TASK_SNAPSHOT,
            payload={"task": data},
            policy_version=data.get("policy_version") or "",
            data_snapshot_id=data.get("data_snapshot_id") or "",
        )

    def record_feedback(
        self,
        *,
        task_id: str,
        revision: int,
        actor: str,
        feedback: Mapping[str, Any],
        outcome: str | None = None,
        policy_version: str = "",
        data_snapshot_id: str = "",
    ) -> dict[str, Any]:
        """Record reviewed outcome/feedback as evidence, never as a policy update."""
        if not actor:
            raise ValueError("feedback actor is required")
        payload: dict[str, Any] = {"actor": actor, "feedback": dict(feedback)}
        if outcome is not None:
            payload["outcome"] = outcome
        return self.append_event(
            task_id=task_id,
            revision=revision,
            event_type=OUTCOME_FEEDBACK,
            payload=payload,
            policy_version=policy_version,
            data_snapshot_id=data_snapshot_id,
        )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": row["sequence"],
            "event_id": row["event_id"],
            "task_id": row["task_id"],
            "revision": row["revision"],
            "event_type": row["event_type"],
            "occurred_at": row["occurred_at"],
            "payload": json.loads(row["payload_json"]),
            "policy_version": row["policy_version"],
            "data_snapshot_id": row["data_snapshot_id"],
        }
