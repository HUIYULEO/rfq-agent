"""Agent layer tests: config loading, tool schemas and dispatch — no network needed."""
import json
import os
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from rfq_agent.application.cases import provider_for, template_for
from rfq_agent.llm.genesis import (CONFIG_KEYS, Session, dispatch, load_config,
                               missing_keys, tool_specs)
from rfq_agent.application.service import RFQService
from rfq_agent.infrastructure.storage import TaskStore

ROOT = Path(__file__).parents[1]


def _temp_dir(prefix: str) -> Path:
    root = Path(os.environ.get("RFQ_TEST_ROOT") or ROOT / "runtime" / "tests")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{prefix}{uuid4().hex[:12]}"
    path.mkdir()
    return path


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _temp_dir("cfg")
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.saved = {key: os.environ.pop(key, None) for key in CONFIG_KEYS}
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self.saved.items():
            if value is not None:
                os.environ[key] = value

    def test_dotenv_fills_missing_values(self):
        (self.tmp / ".env").write_text(
            "GENESIS_MODEL=test-model\nGENESIS_ACCESS_TOKEN=abc123\n", encoding="utf-8")
        config = load_config(self.tmp)
        self.assertEqual(config["GENESIS_MODEL"], "test-model")
        self.assertEqual(config["GENESIS_ACCESS_TOKEN"], "abc123")
        self.assertEqual(missing_keys(config), [])

    def test_default_base_url_and_missing_report(self):
        config = load_config(self.tmp)
        self.assertTrue(config["GENESIS_BASE_URL"].startswith("https://"))
        self.assertEqual(sorted(missing_keys(config)),
                         ["GENESIS_ACCESS_TOKEN", "GENESIS_MODEL"])

    def test_environment_wins_over_dotenv(self):
        (self.tmp / ".env").write_text("GENESIS_MODEL=from-file\n", encoding="utf-8")
        os.environ["GENESIS_MODEL"] = "from-env"
        self.assertEqual(load_config(self.tmp)["GENESIS_MODEL"], "from-env")

    def test_session_without_credentials_is_disabled(self):
        session = Session(None, self.tmp, load_config(self.tmp))
        self.assertFalse(session.enabled)


class ToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = _temp_dir("tool")
        service = RFQService(TaskStore(cls.tmp), provider=provider_for(ROOT),
                             template=template_for(ROOT))
        cls.service = service

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def test_every_tool_has_a_schema_and_a_route(self):
        specs = tool_specs(ROOT)
        names = [spec["function"]["name"] for spec in specs]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            schema = next(s for s in specs if s["function"]["name"] == name)
            self.assertTrue(schema["function"]["description"])
            self.assertEqual(schema["function"]["parameters"]["type"], "object")

    def test_list_cases_reads_the_material(self):
        result = dispatch(self.service, ROOT, "list_cases", {})
        # A user may have uploaded extra mails (key prefix UP-); the six official ones
        # must always be there, and nothing else may impersonate them.
        keys = [case["key"] for case in result["cases"] if not case["key"].startswith("UP-")]
        self.assertEqual(keys, ["A", "B", "C", "D", "E", "F"])

    def test_start_case_is_compact_and_actionable(self):
        result = dispatch(self.service, ROOT, "start_case", {"case_key": "B"})
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["summary"]["priced_lines"], 19)
        self.assertEqual(result["open_review_count"], 2)
        self.assertNotIn("review_items", result)
        self.assertNotIn("interruptions", result)
        issues = dispatch(self.service, ROOT, "list_task_issues", {"task_id": result["task_id"]})
        self.assertEqual([item["id"] for item in issues["items"] if item["source"] == "review"],
                         ["RI-0016", "RI-0019"])
        # the model must not receive 21 raw rows
        self.assertNotIn("results", result)
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 4000)

    def test_get_lines_paginates(self):
        task = dispatch(self.service, ROOT, "start_case", {"case_key": "A"})
        page = dispatch(self.service, ROOT, "get_lines",
                        {"task_id": task["task_id"], "offset": 120, "limit": 50})
        self.assertEqual(page["total"], 122)
        self.assertEqual(len(page["lines"]), 3)

    def test_unknown_tool_raises(self):
        with self.assertRaises(ValueError):
            dispatch(self.service, ROOT, "drop_table", {})


if __name__ == "__main__":
    unittest.main()
