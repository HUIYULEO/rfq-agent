import json
import tempfile
import unittest
from pathlib import Path
from rfq_agent.service import RFQService
from rfq_agent.storage import TaskStore


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).parents[1] / "runtime" / "tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.tmp.cleanup)
        self.service = RFQService(TaskStore(Path(self.tmp.name)))
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
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(all(x["unit_price"] is None for x in result["results"]))
        artifact = self.service.export_quote_draft(self.task, 1)
        self.assertTrue(Path(artifact["artifact"]).exists())
        reopened = RFQService(TaskStore(Path(self.tmp.name)))
        self.assertEqual(reopened.get_task_result(self.task)["results"], result["results"])

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


if __name__ == "__main__":
    unittest.main()
