import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from rfq_agent.ledger import (AGENT_TRACE, HUMAN_DECISION, OUTCOME_FEEDBACK,
                              TASK_SNAPSHOT, OperationalLedger)
from rfq_agent.service import RFQService
from rfq_agent.storage import TaskStore


ROOT = Path(__file__).parents[1]


class ServiceLedgerIntegrationTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "runtime" / "tests"
        base.mkdir(parents=True, exist_ok=True)
        self.work = base / f"service-ledger-{uuid4().hex[:12]}"
        self.work.mkdir()
        self.ledger = OperationalLedger(self.work / "operations.db")
        self.service = RFQService(TaskStore(self.work / "tasks"), ledger=self.ledger)
        self.data = json.loads((ROOT / "examples" / "scaffold.json").read_text())

    def tearDown(self):
        self.ledger.close()
        shutil.rmtree(self.work, ignore_errors=True)

    def test_run_human_resume_and_feedback_are_replayable(self):
        state = self.service.start_task(self.data)
        state = self.service.resolve_review(
            state["task_id"], state["revision"], "RI-TASK", "request_quotation")
        feedback = self.service.record_feedback(
            state["task_id"], state["revision"], "reviewer@example.com",
            {"field": "commercial_disposition", "accepted": True}, "reviewed")

        history = self.service.task_history(state["task_id"])
        kinds = [event["event_type"] for event in history]
        self.assertIn(TASK_SNAPSHOT, kinds)
        self.assertIn(AGENT_TRACE, kinds)
        self.assertIn(HUMAN_DECISION, kinds)
        self.assertIn(OUTCOME_FEEDBACK, kinds)
        self.assertEqual(feedback["payload"]["outcome"], "reviewed")
        self.assertEqual(history, sorted(history, key=lambda event: event["sequence"]))


if __name__ == "__main__":
    unittest.main()
