"""Human approval writes history + the final document atomically; every task gets an analysis report."""
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from docx import Document

from rfq_agent.application.cases import load_case, template_for
from rfq_agent.domain.policy import DEFAULT_POLICY, DEMO_APPROVED_POLICY, active_policy
from rfq_agent.application.service import RFQService
from rfq_agent.infrastructure.storage import TaskStore

ROOT = Path(__file__).resolve().parents[1]


def _tmp(prefix: str) -> Path:
    root = Path(os.environ.get("RFQ_TEST_ROOT") or ROOT / "runtime" / "tests")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{prefix}{uuid4().hex[:12]}"
    path.mkdir()
    return path


def _text(path: Path) -> str:
    document = Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for section in document.sections:
        parts += [p.text for p in section.header.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp("appr-")
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.rfq, self.provider = load_case(ROOT, "B")

    def _ready(self, policy):
        service = RFQService(TaskStore(self.tmp), provider=self.provider, policy=policy,
                             template=template_for(ROOT))
        state = service.start_task(self.rfq.to_dict())
        for item_id in ("RI-0016", "RI-0019"):
            state = service.resolve_review(state["task_id"], state["revision"],
                                           item_id, "request_quotation")
        self.assertEqual(state["commercial_disposition"], "PARTIAL_QUOTE")
        return service, state

    def _rows(self, service):
        connection = sqlite3.connect(service.history_db)
        try:
            headers = connection.execute("SELECT quote_id, approved_by, approval_mode,"
                                         " priced_lines, subtotal_cny FROM quote_header").fetchall()
            lines = connection.execute("SELECT COUNT(*) FROM quote_line").fetchone()[0]
        finally:
            connection.close()
        return headers, lines

    def test_default_policy_stays_fail_closed(self):
        service, state = self._ready(DEFAULT_POLICY)
        with self.assertRaisesRegex(ValueError, "Demo pricing policy"):
            service.activate_quote(state["task_id"], state["revision"], "sales-manager")
        self.assertFalse(service.history_db.exists() and self._rows(service)[0])

    def test_demo_approval_writes_history_and_final_document(self):
        service, state = self._ready(DEMO_APPROVED_POLICY)
        with self.assertRaisesRegex(ValueError, "system actor"):
            service.activate_quote(state["task_id"], state["revision"], "system:auto_draft")
        active = service.activate_quote(state["task_id"], state["revision"], "王经理")
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["approval"]["mode"], "demo")
        self.assertEqual(active["approval"]["label"], "演示批准")
        headers, lines = self._rows(service)
        self.assertEqual(len(headers), 1)
        self.assertEqual(headers[0][1:], ("王经理", "demo", 19, "386595.80"))
        self.assertEqual(lines, 19)
        final = service.store.directory(state["task_id"]) / f"quote-r{state['revision']}.docx"
        self.assertTrue(final.exists())
        self.assertIn(str(final), active["artifacts"])
        text = _text(final)
        self.assertIn("演示批准", text)
        self.assertIn("王经理", text)
        self.assertNotIn("待复核草案", text)
        with self.assertRaisesRegex(ValueError, "already activated"):
            service.activate_quote(state["task_id"], state["revision"], "王经理")

    def test_real_commercial_policy_is_recorded_as_human_approval(self):
        policy = {**DEFAULT_POLICY, "policy_version": "approved-test-only",
                  "commercial_approval_enabled": True}
        service, state = self._ready(policy)
        active = service.activate_quote(state["task_id"], state["revision"], "王经理")
        self.assertEqual(active["approval"]["mode"], "commercial")
        self.assertEqual(active["approval"]["label"], "人工批准")
        self.assertEqual(self._rows(service)[0][0][2], "commercial")
        final = service.store.directory(state["task_id"]) / f"quote-r{state['revision']}.docx"
        self.assertNotIn("演示批准", _text(final))

    def test_render_failure_rolls_back_history(self):
        service, state = self._ready(DEMO_APPROVED_POLICY)
        with mock.patch("rfq_agent.reporting.export_docx.render_draft", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                service.activate_quote(state["task_id"], state["revision"], "王经理")
        self.assertEqual(self._rows(service), ([], 0))
        self.assertNotEqual(service.get_task_result(state["task_id"])["status"], "active")

    def test_auto_draft_defaults_cannot_be_approved(self):
        service = RFQService(TaskStore(self.tmp), provider=self.provider,
                             policy=DEMO_APPROVED_POLICY, template=template_for(ROOT))
        state = service.start_task(self.rfq.to_dict(), autonomy="auto_draft")
        with self.assertRaisesRegex(ValueError, "Automatic review defaults"):
            service.activate_quote(state["task_id"], state["revision"], "王经理")

    def test_demo_approval_is_opt_in_by_env(self):
        with mock.patch.dict(os.environ, {"RFQ_DEMO_APPROVAL": ""}):
            self.assertIs(active_policy(), DEFAULT_POLICY)
        with mock.patch.dict(os.environ, {"RFQ_DEMO_APPROVAL": "1"}):
            self.assertIs(active_policy(), DEMO_APPROVED_POLICY)


class AnalysisReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmp("ana-")
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def test_every_official_case_gets_an_analysis_report(self):
        for key in "ABCDEF":
            with self.subTest(case=key):
                rfq, provider = load_case(ROOT, key)
                service = RFQService(TaskStore(self.tmp / key), provider=provider,
                                     template=template_for(ROOT))
                state = service.start_task(rfq.to_dict())
                exported = service.export_quote_draft(state["task_id"], state["revision"])
                names = [Path(a).name for a in exported["artifacts"]]
                self.assertIn(f"analysis-r{state['revision']}.docx", names)
                path = next(Path(a) for a in exported["artifacts"] if "analysis" in a)
                text = _text(path)
                self.assertIn("询价分析报告", text)
                self.assertNotIn("报价单", text)
                analysis = service.analysis(state["task_id"])
                self.assertEqual(analysis["summary"]["lines"], len(rfq.lines))
                self.assertTrue(all(row["locator"]["text"] for row in analysis["lines"]))
                # The report shows what was checked, passes included.
                self.assertTrue(all(len(row["checks"]) == 5 for row in analysis["lines"]))
                self.assertEqual(sum(sum(c.values()) for c in analysis["summary"]["checks"].values()),
                                 5 * len(rfq.lines))
                self.assertIn("规则核查覆盖", text)
                self.assertEqual(len(state["business_checks"]), 5 * len(rfq.lines))

    def test_unpriced_case_lists_reason_action_and_location(self):
        rfq, provider = load_case(ROOT, "D")
        service = RFQService(TaskStore(self.tmp), provider=provider, template=template_for(ROOT))
        state = service.start_task(rfq.to_dict())
        state = service.resolve_review(state["task_id"], state["revision"], "RI-TASK",
                                       "request_quotation")
        analysis = service.analysis(state["task_id"])
        self.assertEqual(analysis["summary"]["inquiry"], 122)
        first = analysis["lines"][0]
        self.assertEqual(first["status_label"], "转询价")
        self.assertIn("no_price_record", [r["rule"] for r in first["reasons"]])
        self.assertIn("向供应商询价", first["suggested_action"])
        self.assertIn("第 2 行", first["locator"]["text"])
        self.assertEqual(first["locator"]["raw_value"], "AP-2001")
        cert_lines = [row for row in analysis["findings"] if row["rule"] == "certificate"]
        self.assertEqual(len(cert_lines), 4)        # the four CERT REQUIRED rows


if __name__ == "__main__":
    unittest.main()
