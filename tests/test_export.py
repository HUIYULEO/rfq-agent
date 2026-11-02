"""The downloadable draft must carry its own evidence — a judge opens it, not the code."""

import shutil
import unittest
from pathlib import Path

from docx import Document

from rfq_agent.cases import load_case, template_for
from rfq_agent.export_docx import render_draft
from rfq_agent.pricing import summarize

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

    def test_english_case_carries_the_buyer_terms_and_their_verdicts(self):
        text = draft_text("C", WORK)
        self.assertIn("客户要求与响应", text)
        self.assertIn("Validity period of the quotation", text)
        self.assertIn("Quote.pdf", text)
        self.assertIn("待补齐", text)          # 0 priced rows: not claimed as satisfied
        self.assertIn("已满足", text)          # the template requirement is met

    def test_english_case_gets_a_compliance_section_to_sign(self):
        text = draft_text("C", WORK)
        self.assertIn("合规声明", text)
        self.assertIn("未经签署不得随报价对外发出", text)

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