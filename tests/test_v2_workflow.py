"""V2 contracts: durable HIL, split statuses, and formal quote activation."""
import json
import os
import shutil
import unittest
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from rfq_agent.models import Task
from rfq_agent.policy import DEFAULT_POLICY
from rfq_agent.service import RFQService
from rfq_agent.storage import TaskStore


def _temp_dir(prefix: str) -> Path:
    root = Path(os.environ.get("RFQ_TEST_ROOT") or Path(__file__).parents[1] / "runtime" / "tests")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{prefix}{uuid4().hex[:12]}"
    path.mkdir()
    return path


def _scaffold() -> dict:
    return json.loads((Path(__file__).parents[1] / "examples/scaffold.json").read_text())


class AllPricedProvider:
    snapshot_id = "priced-test-snapshot"

    def evaluate(self, rfq, resolutions=None):
        return [{
            "line_id": line.line_id,
            "line_number": index,
            "part_number": line.part_number,
            "description": line.description,
            "quantity": line.quantity,
            "unit": line.unit,
            "condition_accept": list(line.condition_accept),
            "due_at": line.due_at,
            "evidence": dict(line.evidence),
            "eligibility": "priced",
            "unit_price": "12.00",
            "priced_quantity": line.quantity,
            "line_total": str(Decimal("12.00") * Decimal(line.quantity)),
            "unit_cost_cny": "10.00",
            "decision": {
                "recommended_source": "internal",
                "requires_human_review": False,
                "selected_source_id": f"TEST-{line.line_id}",
                "basis": {"source_type": "internal"},
            },
            "internal_candidates": [{"source_id": f"TEST-{line.line_id}",
                                     "eligibility": "eligible"}],
            "supplier_candidates": [],
        } for index, line in enumerate(rfq.lines, start=1)]


class MissingLineProvider(AllPricedProvider):
    def evaluate(self, rfq, resolutions=None):
        return super().evaluate(rfq, resolutions)[:-1]


class V2WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _temp_dir("v2-")
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def test_start_runs_until_a_durable_human_interruption(self):
        service = RFQService(TaskStore(self.tmp))
        state = service.start_task(_scaffold())

        self.assertEqual(state["status"], "awaiting_review")
        self.assertEqual(state["process_status"], "awaiting_human")
        self.assertEqual(state["evaluation_status"], "incomplete")
        self.assertEqual(state["commercial_disposition"], "PENDING")
        pending = [item for item in state["interruptions"] if item["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["review_item_id"], "RI-TASK")
        self.assertEqual(pending[0]["proposed_action"], "resolve_review")
        self.assertTrue(pending[0]["evidence"])
        self.assertTrue(pending[0]["options"])

        resumed = service.resolve_review(
            state["task_id"], state["revision"], "RI-TASK", "request_quotation")
        self.assertEqual(resumed["revision"], 2)
        self.assertEqual(resumed["process_status"], "draft_ready")
        self.assertEqual(resumed["evaluation_status"], "complete")
        self.assertEqual(resumed["commercial_disposition"], "SUPPLIER_INQUIRY")
        self.assertFalse([item for item in resumed["interruptions"]
                          if item["status"] == "pending"])
        self.assertEqual(resumed["interruptions"][0]["status"], "resolved")
        self.assertEqual(resumed["interruptions"][0]["decision"], "request_quotation")

    def test_formal_activation_fails_closed_for_non_quotable_case(self):
        service = RFQService(TaskStore(self.tmp))
        state = service.start_task(_scaffold())
        with self.assertRaisesRegex(ValueError, "incomplete"):
            service.activate_quote(state["task_id"], state["revision"], "sales-manager")

        state = service.resolve_review(
            state["task_id"], state["revision"], "RI-TASK", "request_quotation")
        with self.assertRaisesRegex(ValueError, "not quotable"):
            service.activate_quote(state["task_id"], state["revision"], "sales-manager")

    def test_formal_activation_records_explicit_human_approval(self):
        approved_policy = {**DEFAULT_POLICY, "policy_version": "approved-test-only",
                           "commercial_approval_enabled": True}
        service = RFQService(TaskStore(self.tmp), provider=AllPricedProvider(),
                             policy=approved_policy)
        state = service.start_task(_scaffold())
        self.assertEqual(state["evaluation_status"], "complete")
        self.assertEqual(state["commercial_disposition"], "QUOTE")
        self.assertEqual(state["process_status"], "draft_ready")

        active = service.activate_quote(state["task_id"], state["revision"], "sales-manager")
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["process_status"], "quote_activated")
        self.assertEqual(active["decision_log"][-1]["action"], "activate_quote")
        self.assertEqual(active["decision_log"][-1]["actor"], "sales-manager")

    def test_demo_policy_cannot_activate_a_complete_quote(self):
        service = RFQService(TaskStore(self.tmp), provider=AllPricedProvider())
        state = service.start_task(_scaffold())
        self.assertEqual(state["evaluation_status"], "complete")
        with self.assertRaisesRegex(ValueError, "Demo pricing policy"):
            service.activate_quote(state["task_id"], state["revision"], "sales-manager")

    def test_missing_provider_line_blocks_activation_even_without_reviews(self):
        service = RFQService(TaskStore(self.tmp), provider=MissingLineProvider())
        state = service.start_task(_scaffold())
        self.assertEqual(state["evaluation_status"], "incomplete")
        self.assertEqual(state["commercial_disposition"], "PENDING")
        self.assertTrue(any("result_present:2" in issue for issue in state["evaluation_issues"]))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            service.activate_quote(state["task_id"], state["revision"], "sales-manager")

    def test_old_task_json_uses_legacy_status_as_process_status(self):
        data = _scaffold()
        old = {
            "task_id": "0" * 32,
            "rfq": data,
            "status": "awaiting_review",
        }
        restored = Task.from_dict(old)
        self.assertEqual(restored.process_status, "awaiting_review")
        self.assertEqual(restored.commercial_disposition, "PENDING")
        self.assertEqual(restored.interruptions, [])


if __name__ == "__main__":
    unittest.main()
