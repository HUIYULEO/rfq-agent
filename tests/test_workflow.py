"""Contract tests for the review gate and the deterministic core.

Uses the synthetic scaffold only: it proves control flow, not pricing accuracy.
"""
import json
import os
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from rfq_agent.service import RFQService
from rfq_agent.storage import TaskStore


def _test_root() -> Path:
    root = Path(os.environ.get("RFQ_TEST_ROOT") or Path(__file__).parents[1] / "runtime" / "tests")
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_temp_dir(prefix: str) -> Path:
    """mkdtemp() drops ACL inheritance and the sandbox then denies child mkdir."""
    path = _test_root() / f"{prefix}{uuid4().hex[:12]}"
    path.mkdir()
    return path


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_temp_dir("w")
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.service = RFQService(TaskStore(Path(self.tmp)))
        data = json.loads((Path(__file__).parents[1] / "examples/scaffold.json").read_text())
        self.task = self.service.create_task(data)["task_id"]

    def test_preconditions(self):
        with self.assertRaises(ValueError):
            self.service.build_quote_options(self.task, 1)
        with self.assertRaises(ValueError):
            self.service.export_quote_draft(self.task, 1)

    def test_no_fabricated_price_and_persistence(self):
        self.service.analyze_rfq(self.task)
        result = self.service.build_quote_options(self.task, 1)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertTrue(all(x["unit_price"] is None for x in result["results"]))
        artifact = self.service.export_quote_draft(self.task, 1)
        self.assertTrue(Path(artifact["artifact"]).exists())
        reopened = RFQService(TaskStore(Path(self.tmp)))
        self.assertEqual(reopened.get_task_result(self.task)["results"], result["results"])

    def test_zero_coverage_asks_one_task_question_not_one_per_line(self):
        self.service.analyze_rfq(self.task)
        result = self.service.build_quote_options(self.task, 1)
        self.assertEqual(len(result["review_items"]), 1)
        self.assertEqual(result["review_items"][0]["scope"], "task")
        self.assertEqual(result["review_items"][0]["id"], "RI-TASK")

    def test_revision_invalidates_price_and_rejects_stale_export(self):
        self.service.analyze_rfq(self.task)
        self.service.build_quote_options(self.task, 1)
        self.service.export_quote_draft(self.task, 1)
        updated = self.service.apply_review(self.task, 1, "1", "3")
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["results"], [])
        self.assertEqual(updated["artifacts"], [])
        with self.assertRaises(ValueError):
            self.service.export_quote_draft(self.task, 1)
        with self.assertRaises(ValueError):
            self.service.export_quote_draft(self.task, 2)

    def test_reject_invalid_quantity_and_path(self):
        for quantity in ["0", "-1", "NaN", "Infinity"]:
            with self.assertRaises(ValueError):
                self.service.apply_review(self.task, 1, "1", quantity)
        with self.assertRaises(ValueError):
            self.service.get_task_result("../outside")
        self.assertEqual(self.service.get_task_result(self.task)["revision"], 1)

    def test_unknown_autonomy_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.create_task({"rfq_number": "X", "evaluation_date": "2026-09-07",
                                      "source_files": [], "lines": []}, autonomy="turbo")


if __name__ == "__main__":
    unittest.main()