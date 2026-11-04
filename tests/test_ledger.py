"""Contract tests for the append-only v2 operational ledger."""
import shutil
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from rfq_agent.infrastructure.ledger import (AGENT_TRACE, HUMAN_DECISION, OUTCOME_FEEDBACK,
                              RULE_DECISION, TASK_SNAPSHOT, OperationalLedger)


ROOT = Path(__file__).parents[1] / "runtime" / "tests"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.work = ROOT / f"ledger-{uuid4().hex[:12]}"
        self.work.mkdir()
        self.path = self.work / "operations.db"
        self.ledger = OperationalLedger(self.path)

    def tearDown(self):
        self.ledger.close()
        shutil.rmtree(self.work, ignore_errors=True)

    def test_persists_order_and_json_roundtrip(self):
        first = self.ledger.append_event(
            task_id="task-a", revision=1, event_type=AGENT_TRACE,
            payload={"tool": "extract", "nested": {"中文": [1, True, None]}},
            policy_version="p1", data_snapshot_id="s1",
        )
        second = self.ledger.append_event(
            task_id="task-a", revision=1, event_type=RULE_DECISION,
            payload={"rule": "price-present", "result": "pass"},
        )
        self.assertLess(first["sequence"], second["sequence"])

        self.ledger.close()
        self.ledger = OperationalLedger(self.path)
        rows = self.ledger.events_for_task("task-a")
        self.assertEqual([row["event_type"] for row in rows], [AGENT_TRACE, RULE_DECISION])
        self.assertEqual(rows[0]["payload"]["nested"]["中文"], [1, True, None])
        self.assertEqual(rows[0]["policy_version"], "p1")
        self.assertEqual(rows[0]["data_snapshot_id"], "s1")

    def test_filters_and_isolates_tasks(self):
        one = self.ledger.append_event(
            task_id="task-a", revision=1, event_type=HUMAN_DECISION,
            payload={"choice": "approve"},
        )
        self.ledger.append_event(
            task_id="task-a", revision=2, event_type=AGENT_TRACE,
            payload={"tool": "resume"},
        )
        self.ledger.append_event(
            task_id="task-b", revision=1, event_type=HUMAN_DECISION,
            payload={"choice": "reject"},
        )
        filtered = self.ledger.events_for_task("task-a", event_type=HUMAN_DECISION)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["payload"]["choice"], "approve")
        after = self.ledger.events_for_task("task-a", after_sequence=one["sequence"])
        self.assertEqual([row["revision"] for row in after], [2])
        self.assertEqual(len(self.ledger.events_for_task("task-b")), 1)

    def test_snapshot_and_feedback_conveniences(self):
        snapshot = self.ledger.record_task_snapshot({
            "task_id": "task-a", "revision": 3, "status": "awaiting_human",
            "policy_version": "policy-7", "data_snapshot_id": "facts-9",
        })
        feedback = self.ledger.record_feedback(
            task_id="task-a", revision=3, actor="reviewer@example.com",
            feedback={"field": "currency", "before": "", "after": "USD"},
            outcome="corrected", policy_version="policy-7", data_snapshot_id="facts-9",
        )
        self.assertEqual(snapshot["event_type"], TASK_SNAPSHOT)
        self.assertEqual(snapshot["payload"]["task"]["status"], "awaiting_human")
        self.assertEqual(feedback["event_type"], OUTCOME_FEEDBACK)
        self.assertEqual(feedback["payload"]["outcome"], "corrected")

    def test_database_rejects_update_and_delete(self):
        row = self.ledger.append_event(
            task_id="task-a", revision=1, event_type=AGENT_TRACE,
            payload={"tool": "extract"},
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.connection:
                self.ledger.connection.execute(
                    "UPDATE operational_event SET payload_json = '{}' WHERE event_id = ?",
                    (row["event_id"],),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.connection:
                self.ledger.connection.execute(
                    "DELETE FROM operational_event WHERE event_id = ?", (row["event_id"],)
                )

    def test_rejects_non_json_or_invalid_identity_without_partial_row(self):
        with self.assertRaises((TypeError, ValueError)):
            self.ledger.append_event(
                task_id="task-a", revision=1, event_type=AGENT_TRACE,
                payload={"not_json": object()},
            )
        with self.assertRaises(ValueError):
            self.ledger.append_event(
                task_id="", revision=1, event_type=AGENT_TRACE, payload={},
            )
        self.assertEqual(self.ledger.events_for_task("task-a"), [])

    def test_connection_is_safe_across_http_style_worker_threads(self):
        def append(index: int):
            return self.ledger.append_event(
                task_id="task-threaded", revision=1, event_type=AGENT_TRACE,
                payload={"worker_event": index},
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(append, range(20)))
        self.assertEqual(len(rows), 20)
        persisted = self.ledger.events_for_task("task-threaded")
        self.assertEqual(len(persisted), 20)
        self.assertEqual(len({row["event_id"] for row in persisted}), 20)


if __name__ == "__main__":
    unittest.main()
