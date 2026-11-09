"""Unrecognised inquiries must pass preview and reach model extraction.

The deterministic parser still owns every format it already understands. A mail
it cannot read is no longer a preview 400: the UI is told the model will extract
it, and confirm runs that path. A missing or failing model produces an analysis
report that says so. No test here opens a network connection.
"""
import json
import os
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from uuid import uuid4

from docx import Document

from rfq_agent.application import cases
from rfq_agent.llm.genesis import GenesisError
from rfq_agent import web

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG = {"GENESIS_BASE_URL": "https://example.invalid/v1", "GENESIS_MODEL": "fake",
          "GENESIS_ACCESS_TOKEN": "test-token"}
UNCONFIGURED = {"GENESIS_BASE_URL": "https://example.invalid/v1", "GENESIS_MODEL": "",
                "GENESIS_ACCESS_TOKEN": ""}


def _anchored(part, quantity, block, row, unit="EA"):
    def field(value):
        return {"value": value, "raw": value, "confidence": 0.99,
                "anchor": {"block": block, "row": row}, "evidence": []}

    return {"part_number": field(part), "quantity": field(quantity), "unit": field(unit)}


class FakeGenesis:
    """Stand-in for the chat transport. Answers from the fixture text only."""

    payloads: list[str] = []

    def __init__(self, config, timeout=60):
        self.config = config
        self.timeout = timeout

    def chat(self, messages, tools=None):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        FakeGenesis.payloads.append(user)
        if "询价审核员" in system:
            body = {"document_type": "rfq", "document_confidence": 0.97,
                    "requirements": [], "judgement": "标准询价"}
        else:
            lines = []
            if "物料编码" in user and "PN-0001" in user:
                lines = [_anchored("PN-0001", "8", "a1-2", 3),
                         _anchored("PN-MOCK-ZZ9", "1", "a1-2", 4)]
            elif "PN-0002" in user and "Qty 15" in user:
                lines = [_anchored("PN-0002", "15", "b1", 2),
                         _anchored("PN-0004", "20", "b1", 3)]
            body = {"document_type": "rfq", "document_confidence": 0.99, "lines": lines}
        return {"content": json.dumps(body, ensure_ascii=False)}


class BoomGenesis:
    def __init__(self, config, timeout=60):
        pass

    def chat(self, messages, tools=None):
        raise GenesisError("连不上模型接口：timed out")


def _docx_text(path: Path) -> str:
    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


class UnrecognizedPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ.get("RFQ_TEST_ROOT") or ROOT / "runtime" / "tests")
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.root = cls.root / f"unrec-{uuid4().hex[:8]}"
        cls.root.mkdir()
        os.symlink(ROOT / "data", cls.root / "data")
        cls._saved_root = web.ROOT
        web.ROOT = cls.root
        web.SERVICE = None
        cases._CACHE.clear()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        web.ROOT = cls._saved_root
        web.SERVICE = None
        cases._CACHE.clear()
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        FakeGenesis.payloads = []

    def _request(self, method, path, data=None, content_type=None):
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, json.loads(raw) if raw else {}

    def _preview(self, name):
        payload = (FIXTURES / name).read_bytes()
        return self._request("POST", "/api/preview?name=" + urllib.request.quote(name), payload)

    def _upload(self, token):
        body = json.dumps({"token": token}).encode("utf-8")
        return self._request("POST", "/api/upload", body, "application/json")

    def _task(self, task_id):
        status, task = self._request("GET", "/api/task?id=" + task_id)
        self.assertEqual(status, 200, task)
        return task

    def test_hard_mocks_preview_then_extract_with_locators(self):
        expectations = {
            "mock1_hard_preview_blocked.eml": ("物料编码", {"PN-0001", "PN-MOCK-ZZ9"}, "询价明细"),
            "mock2_hard_body_only_preview_blocked.eml": ("Qty 15", {"PN-0002", "PN-0004"}, "邮件正文"),
        }
        with mock.patch("rfq_agent.web.load_config", return_value=CONFIG), \
                mock.patch("rfq_agent.llm.perception.Genesis", FakeGenesis):
            for name, (seen, parts, locator_bit) in expectations.items():
                with self.subTest(name=name):
                    FakeGenesis.payloads = []
                    status, preview = self._preview(name)
                    self.assertEqual(status, 200, preview)
                    self.assertFalse(preview["recognized"])
                    self.assertIn("未识别", preview["notice"])
                    self.assertIn("模型", preview["notice"])
                    self.assertTrue(preview["baseline_error"])
                    self.assertRegex(preview["token"], r"^[0-9a-f]{32}$")

                    status, uploaded = self._upload(preview["token"])
                    self.assertEqual(status, 200, uploaded)
                    task = self._task(uploaded["task_id"])
                    extraction = task["extraction"]
                    self.assertEqual(extraction["mode"], "extract")
                    self.assertEqual(extraction["source"], "agent")
                    self.assertGreater(extraction["lines_model"], 0)
                    self.assertEqual(extraction["lines_deterministic"], 0)
                    blob = json.dumps(extraction, ensure_ascii=False)
                    self.assertNotIn("unit_price", blob)
                    self.assertNotIn("line_total", blob)
                    self.assertTrue(any(seen in payload for payload in FakeGenesis.payloads))

                    status, report = self._request(
                        "GET", "/api/report?id=" + uploaded["task_id"])
                    self.assertEqual(status, 200, report)
                    self.assertGreater(report["total"], 0)
                    located = "\n".join(row["locator"] for row in report["lines"])
                    self.assertIn(locator_bit, located)
                    for part in parts:
                        self.assertIn(part, located)

    def test_model_unconfigured_writes_a_degraded_report(self):
        with mock.patch("rfq_agent.web.load_config", return_value=UNCONFIGURED), \
                mock.patch("rfq_agent.llm.perception.Genesis", BoomGenesis):
            status, preview = self._preview("mock2_hard_body_only_preview_blocked.eml")
            self.assertEqual(status, 200, preview)
            self.assertFalse(preview["recognized"])
            status, uploaded = self._upload(preview["token"])
            self.assertEqual(status, 200, uploaded)
            task = self._task(uploaded["task_id"])
            extraction = task["extraction"]
            self.assertEqual(extraction["mode"], "degraded")
            self.assertEqual(extraction["source"], "none")
            self.assertIn("未配置", extraction["reason"])
            self.assertIn("未识别", extraction["reason"])
            self.assertEqual(extraction["lines_model"], 0)
            self.assertNotEqual(extraction["source"], "deterministic")
            self.assertEqual(task["status"], "evaluation_incomplete")
            stored = web._service().get_task_result(uploaded["task_id"])
            self.assertEqual(stored["results"], [])

            status, report = self._request("GET", "/api/report?id=" + uploaded["task_id"])
            self.assertEqual(status, 200, report)
            self.assertEqual(report["failure"]["mode"], "degraded")
            self.assertIn("未配置", report["failure"]["reason"])
            self.assertIn(".eml", report["failure"]["locator"]["text"])
            self.assertEqual(report["total"], 0)

            docx = next(Path(name) for name in stored["artifacts"] if str(name).endswith(".docx"))
            text = _docx_text(docx)
            self.assertIn("询价分析报告", text)
            self.assertIn("未配置", text)
            self.assertIn("定位：", text)
            self.assertNotIn("报价单", text)

    def test_model_failure_is_reported_not_silently_parsed(self):
        with mock.patch("rfq_agent.web.load_config", return_value=CONFIG), \
                mock.patch("rfq_agent.llm.perception.Genesis", BoomGenesis):
            status, preview = self._preview("mock1_hard_preview_blocked.eml")
            self.assertEqual(status, 200, preview)
            status, uploaded = self._upload(preview["token"])
            self.assertEqual(status, 200, uploaded)
            task = self._task(uploaded["task_id"])
            extraction = task["extraction"]
            self.assertEqual(extraction["mode"], "failed")
            self.assertEqual(extraction["source"], "none")
            self.assertIn("连不上模型接口", extraction["reason"])
            self.assertIn("未识别", extraction["reason"])
            self.assertEqual(task["status"], "evaluation_incomplete")
            status, report = self._request("GET", "/api/report?id=" + uploaded["task_id"])
            self.assertEqual(report["failure"]["mode"], "failed")
            self.assertTrue(report["failure"]["locator"]["text"])

    def test_normal_mocks_stay_on_the_deterministic_audit_path(self):
        expected = {
            "mock1_nonstandard_headers.eml": (5, "PN-0008", "BOX of 100"),
            "mock2_body_requirements.eml": (4, "PN-UNKNOWN-42", "EA"),
        }
        with mock.patch("rfq_agent.web.load_config", return_value=CONFIG), \
                mock.patch("rfq_agent.llm.perception.Genesis", FakeGenesis):
            for name, (count, part, unit) in expected.items():
                with self.subTest(name=name):
                    status, preview = self._preview(name)
                    self.assertEqual(status, 200, preview)
                    self.assertTrue(preview["recognized"])
                    self.assertEqual(preview["lines"], count)
                    self.assertNotIn("notice", preview)
                    status, uploaded = self._upload(preview["token"])
                    self.assertEqual(status, 200, uploaded)
                    task = self._task(uploaded["task_id"])
                    extraction = task["extraction"]
                    self.assertEqual(extraction["mode"], "audit")
                    self.assertEqual(extraction["source"], "agent+deterministic")
                    self.assertEqual(extraction["lines_model"], 0)
                    self.assertEqual(extraction["lines_deterministic"], count)
                    status, page = self._request(
                        "GET", "/api/extraction?id=" + uploaded["task_id"])
                    found = next(row for row in page["lines"] if row["part_number"] == part)
                    self.assertEqual(found["unit"], unit)

    def test_unreadable_input_is_still_rejected(self):
        status, body = self._request(
            "POST", "/api/preview?name=" + urllib.request.quote("notes.txt"), b"not an email")
        self.assertEqual(status, 400)
        self.assertIn(".eml", body["error"])

        status, body = self._request(
            "POST", "/api/preview?name=" + urllib.request.quote("broken.eml"),
            b"\x00\x01this is not an email")
        self.assertEqual(status, 400)
        self.assertIn("不是可读取的询价邮件", body["error"])
        pending = self.root / "runtime" / "uploads" / ".pending"
        if pending.is_dir():
            self.assertFalse(any(pending.glob("*.eml")))


if __name__ == "__main__":
    unittest.main()
