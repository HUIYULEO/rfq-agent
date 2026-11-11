"""Input provenance survives fallback, persistence, the desk and report export."""
import io
import json
import shutil
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from docx import Document

from rfq_agent import web
from rfq_agent.application.cases import case_by_key, load_case
from rfq_agent.application.service import RFQService
from rfq_agent.domain.input_status import fallback_report, input_status
from rfq_agent.domain.models import RFQ
from rfq_agent.infrastructure.storage import TaskStore
from rfq_agent.llm import perception
from rfq_agent.llm.genesis import Genesis, GenesisError
from rfq_agent.reporting import analysis

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {"GENESIS_BASE_URL": "https://example.invalid/v1", "GENESIS_MODEL": "stub",
          "GENESIS_ACCESS_TOKEN": "test-secret"}


class TransportStatusTests(unittest.TestCase):
    def test_transport_failures_have_stable_codes(self):
        errors = [(TimeoutError(), "timeout"),
                  (urllib.error.URLError(TimeoutError()), "timeout"),
                  (urllib.error.URLError("offline"), "network_error"),
                  (urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {},
                                          io.BytesIO(b"upstream detail")), "http_error")]
        for error, code in errors:
            with self.subTest(code=code), mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(GenesisError) as raised:
                    Genesis(CONFIG).chat([])
                self.assertEqual(raised.exception.code, code)

    def test_invalid_envelope_and_json_are_classified(self):
        for raw, code in [(b"<html>proxy error</html>", "invalid_json"),
                          (b"null", "invalid_response"), (b"{}", "invalid_response"),
                          (b'{"choices":[{"message":null}]}', "invalid_response")]:
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = raw
            with self.subTest(raw=raw), mock.patch("urllib.request.urlopen", return_value=response):
                with self.assertRaises(GenesisError) as raised:
                    Genesis(CONFIG).chat([])
                self.assertEqual(raised.exception.code, code)

    def test_perception_invalid_content_is_classified(self):
        for content, code in [("not JSON", "invalid_json"), ("{broken}", "invalid_json"),
                              ([{"text": "hello"}], "invalid_response")]:
            with self.subTest(content=content), mock.patch.object(
                    Genesis, "chat", return_value={"content": content}):
                with self.assertRaises(perception.PerceptionError) as raised:
                    perception._chat_json(CONFIG, "system", "input", 1)
                self.assertEqual(raised.exception.code, code)


class InputStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = ROOT / "runtime" / "tests" / f"input-status-{uuid4().hex}"
        self.tmp.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp)
        self.service = RFQService(TaskStore(self.tmp))
        self.data = json.loads((ROOT / "examples/scaffold.json").read_text(encoding="utf-8"))

    def test_missing_config_skips_model_and_keeps_parser_result(self):
        rfq = RFQ.from_dict(self.data)
        with mock.patch.object(web, "case_by_key", return_value=SimpleNamespace(path=Path("mail.eml"))), \
             mock.patch.object(web, "load_config", return_value={}), \
             mock.patch.object(perception, "deterministic", return_value=(rfq, "")), \
             mock.patch.object(perception, "run") as run:
            actual, report = web._understand("B")
        run.assert_not_called()
        self.assertIs(actual, rfq)
        self.assertFalse(report["model_ran"])
        self.assertTrue(report["degraded"])
        self.assertEqual(report["fallback_code"], "not_configured")

    def test_failed_model_falls_back_without_persisting_raw_exception(self):
        rfq = RFQ.from_dict(self.data)
        for error in (GenesisError("private upstream detail test-secret", "timeout"),
                      GenesisError("private upstream detail test-secret", "invalid_json"),
                      perception.PerceptionError("private source text")):
            with self.subTest(code=error.code), \
                 mock.patch.object(web, "case_by_key", return_value=SimpleNamespace(path=Path("mail.eml"))), \
                 mock.patch.object(web, "load_config", return_value=CONFIG), \
                 mock.patch.object(perception, "deterministic", return_value=(rfq, "")), \
                 mock.patch.object(perception, "run", side_effect=error):
                actual, report = web._understand("B")
                self.assertIs(actual, rfq)
                self.assertEqual(report["fallback_code"], error.code)
                self.assertNotIn("private", json.dumps(report))
                self.assertNotIn("test-secret", json.dumps(report))

    def test_both_paths_fail_without_creating_empty_rfq(self):
        # Neither the parser nor the model read anything: refuse, and never hand a
        # zero-line RFQ to pricing (that is what the old silent 400 hid).
        with mock.patch.object(web, "case_by_key", return_value=SimpleNamespace(path=Path("mail.eml"))), \
             mock.patch.object(web, "load_config", return_value=CONFIG), \
             mock.patch.object(perception, "run", side_effect=GenesisError("offline")), \
             mock.patch.object(perception, "deterministic", return_value=(None, "unreadable")), \
             mock.patch.object(perception, "readable", return_value=(False, [])):
            with self.assertRaisesRegex(ValueError, "unreadable"):
                web._understand("B")

    def test_legacy_and_missing_records_are_not_claimed_as_success(self):
        for report in (None, {}, {"model": "configured-is-not-proof"}, {"model_ran": False}):
            self.assertEqual(input_status(report)["status"], "unknown")
            self.assertIsNone(input_status(report)["model_ran"])
        old = input_status({"source": "deterministic", "reason": "raw private failure"})
        self.assertEqual(old["status"], "degraded")
        self.assertNotIn("private", old["reason"])
        self.assertTrue(input_status({"source": "agent+deterministic"})["model_ran"])

    def test_status_reaches_trace_desk_paginated_report_json_and_docx(self):
        reports = [fallback_report("not_configured"), fallback_report("timeout"),
                   {"source": "agent+deterministic", "mode": "audit", "model_ran": True,
                    "degraded": False}, {}, {"source": "deterministic"}]
        for report in reports:
            with self.subTest(report=report):
                state = self.service.start_task(self.data)
                original_results = json.dumps(state["results"], sort_keys=True)
                expected = input_status(report)
                state = self.service.record_extraction(state["task_id"], report)
                self.assertEqual(json.dumps(state["results"], sort_keys=True), original_results)
                trace = state["agent_trace"][-1]
                self.assertEqual(trace["model_ran"], expected["model_ran"])
                self.assertEqual(trace["reason"], expected["reason"])
                desk = self.service.store.get_desk(state["task_id"])
                with mock.patch.object(web, "_service", return_value=self.service), \
                     mock.patch.object(web, "_data_trace", return_value={}):
                    self.assertEqual(web._view(desk)["input_status"], expected)
                self.assertEqual(web._report_page(desk, 0, 1)["input_status"], expected)
                self.assertEqual(analysis.build(desk)["input_status"], expected)
                self.service.export_quote_draft(state["task_id"], 1)
                directory = self.service.store.directory(state["task_id"])
                payload = json.loads((directory / "draft-r1.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["analysis"]["input_status"], expected)
                document = Document(directory / "analysis-r1.docx")
                text = "\n".join(p.text for p in document.paragraphs)
                self.assertIn(expected["label"], text)
                self.assertIn(expected["reason"], text)

    def test_official_cases_keep_identical_pricing_with_model_status(self):
        try:
            case_by_key(ROOT, "B")
        except ValueError:
            self.skipTest("官方素材不在工作区")
        for key in "ABCDEF":
            with self.subTest(case=key):
                baseline, provider = load_case(ROOT, key)
                service = RFQService(TaskStore(self.tmp / key), provider=provider)
                expected = service.start_task(baseline.to_dict())
                with mock.patch.object(perception, "audit", return_value={
                        "document_type": "rfq", "document_confidence": 0.99}):
                    rfq, report = perception.run(CONFIG, case_by_key(ROOT, key).path)
                self.assertTrue(report["model_ran"])
                self.assertFalse(report["degraded"])
                actual = service.start_task(rfq.to_dict())
                actual = service.record_extraction(actual["task_id"], report)
                self.assertEqual(json.dumps(actual["results"], sort_keys=True),
                                 json.dumps(expected["results"], sort_keys=True))
