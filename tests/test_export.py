"""The downloadable draft must carry its own evidence — a judge opens it, not the code."""

import shutil
import unittest
from pathlib import Path

from docx import Document

from rfq_agent.cases import load_case, template_for
from rfq_agent.export_docx import render_draft
from rfq_agent.pricing import summarize
from rfq_agent.service import RFQService
from rfq_agent.storage import TaskStore

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "runtime" / "test_export"


def draft_text(key: str, target: Path) -> str:
    rfq, provider = load_case(ROOT, key)
    results = provider.evaluate(rfq)
    path = render_draft(template=template_for(ROOT), output=target / f"{key}.docx",
                        rfq=rfq, results=results, summary=summarize(results),
                        policy_version="demo-2026-11-01", evaluation_date=rfq.evaluation_date)
    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


class DraftContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(WORK, ignore_errors=True)

    def test_unpriced_case_cannot_render_a_quotation(self):
        with self.assertRaisesRegex(ValueError, "at least one priced line"):
            draft_text("C", WORK)

    def test_supplier_inquiry_exports_analysis_only(self):
        rfq, provider = load_case(ROOT, "C")
        service = RFQService(TaskStore(WORK / "tasks"), provider=provider,
                             template=template_for(ROOT))
        state = service.start_task(rfq.to_dict())
        state = service.resolve_review(state["task_id"], state["revision"],
                                       "RI-TASK", "request_quotation")
        exported = service.export_quote_draft(state["task_id"], state["revision"])
        names = [Path(item).name for item in exported["artifacts"]]
        # JSON record + readable analysis report; never a quotation document.
        self.assertEqual(names, [f"draft-r{state['revision']}.json",
                                 f"analysis-r{state['revision']}.docx"])
        self.assertFalse(any(name.startswith("quote") for name in names))

    def test_quote_draft_does_not_assert_unapproved_terms(self):
        text = draft_text("B", WORK)
        self.assertNotIn("按供应商标准质保", text)
        self.assertNotIn("自报价日起 30 天", text)
        self.assertNotIn("不含出口管制或限制扩散物项", text)

    def test_chinese_case_gets_no_compliance_section(self):
        text = draft_text("B", WORK)
        self.assertNotIn("合规声明", text)
        self.assertNotIn("客户要求与响应", text)

    def test_chinese_case_carries_its_parse_warnings(self):
        text = draft_text("A", WORK)
        self.assertIn("解析质量提示", text)
        self.assertIn("重复料号", text)
        self.assertIn("不改变任何价格", text)

    def test_case_without_terms_but_with_warnings_shows_only_the_warnings(self):
        """No empty headings: a section only appears when it has something to say."""
        text = draft_text("B", WORK)
        self.assertNotIn("客户要求与响应", text)
        self.assertIn("解析质量提示", text)
        self.assertIn("PN-0001", text)

    def test_each_heading_appears_once(self):
        """The template has no Heading 1 style; a failed lookup must not duplicate headings."""
        text = draft_text("B", WORK)
        self.assertEqual(text.count("报价依据与条件摘要（草案）"), 1)
        self.assertEqual(text.count("解析质量提示"), 1)


if __name__ == "__main__":
    unittest.main()
