"""Perception layer: contract validation, hallucination refusal, and the fallback path.

No network: the model is stubbed, so these tests pin the *code's* guarantees — that a
value must be findable in the source, that low confidence is refused rather than
guessed, and that a dead model still yields a priced task via the deterministic parser.
"""
import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock
from uuid import uuid4

from rfq_agent import perception
from rfq_agent.cases import case_by_key
from rfq_agent.models import Requirement
from rfq_agent.sources import build_rfq

ROOT = Path(__file__).parents[1]


def _field(value, confidence=0.99, block="b3", row=2, raw=None):
    return {"value": value, "raw": raw if raw is not None else value,
            "confidence": confidence, "anchor": {"block": block, "row": row},
            "evidence": []}


def _table(rows=None):
    return perception.Block("b3", "attachment-xlsx", "询价表 · Sheet1",
                            rows=rows or [["Part Number", "Quantity", "Unit Of Measure"],
                                          ["PN-0001", "8", "EA"],
                                          ["PN-0002", "12", "EA"]])


def _line(part, quantity, unit="EA", confidence=0.99, row=2):
    return {"part_number": _field(part, confidence, row=row),
            "quantity": _field(quantity, confidence, row=row),
            "unit": _field(unit, confidence, row=row)}


class ValidateTests(unittest.TestCase):
    def test_grounded_line_is_accepted(self):
        check = perception.validate({"document_type": "rfq", "document_confidence": 0.99,
                                     "lines": [_line("PN-0001", "8")]}, [_table()])
        self.assertFalse(check.blocking)
        self.assertEqual(len(check.lines), 1)
        self.assertEqual(check.lines[0]["Part Number"], "PN-0001")
        self.assertEqual(check.lines[0]["_extraction"]["confidence"], 0.99)

    def test_invented_part_number_is_refused(self):
        # The model read a part number that exists nowhere in the source: refuse it,
        # never let it reach pricing.
        check = perception.validate({"document_type": "rfq", "document_confidence": 0.99,
                                     "lines": [_line("PN-9999", "8")]}, [_table()])
        self.assertTrue(check.blocking)
        self.assertEqual(check.lines, [])
        self.assertIn("找不到", check.rejected[0]["reasons"][0])

    def test_invented_quantity_is_refused(self):
        check = perception.validate({"document_type": "rfq", "document_confidence": 0.99,
                                     "lines": [_line("PN-0001", "999")]}, [_table()])
        self.assertTrue(check.blocking)
        self.assertEqual(check.lines, [])

    def test_non_numeric_quantity_is_refused(self):
        check = perception.validate({"document_type": "rfq", "document_confidence": 0.99,
                                     "lines": [_line("PN-0001", "八")]}, [_table()])
        self.assertTrue(check.blocking)
        self.assertTrue(any("不是正数" in reason for reason in check.rejected[0]["reasons"]))

    def test_below_review_threshold_is_refused(self):
        check = perception.validate({"document_type": "rfq", "document_confidence": 0.99,
                                     "lines": [_line("PN-0001", "8", confidence=0.55)]},
                                    [_table()])
        self.assertTrue(check.blocking)
        self.assertEqual(check.lines, [])
        self.assertIn("低于", check.rejected[0]["reasons"][0])

    def test_between_thresholds_is_used_but_flagged(self):
        check = perception.validate({"document_type": "rfq", "document_confidence": 0.99,
                                     "lines": [_line("PN-0001", "8", confidence=0.8)]},
                                    [_table()])
        self.assertFalse(check.blocking)
        self.assertEqual(len(check.lines), 1)
        self.assertEqual(len(check.review), 1)
        self.assertTrue(any(issue["kind"] == "line_best_guess" for issue in check.issues))

    def test_document_type_judgement_blocks(self):
        check = perception.validate({"document_type": "quote", "document_confidence": 0.9,
                                     "lines": [_line("PN-0001", "8")]}, [_table()])
        self.assertTrue(check.blocking)
        self.assertEqual(check.issues[0]["kind"], "not_an_rfq")

    def test_unknown_columns_and_unparsed_rows_are_reported_not_dropped(self):
        check = perception.validate(
            {"document_type": "rfq", "document_confidence": 0.99, "lines": [_line("PN-0001", "8")],
             "unknown_columns": ["客户自定义列 Z"],
             "unparsed_rows": [{"block": "b3", "row": 9, "reason": "数量为 N/A"}]},
            [_table()])
        self.assertEqual(check.unknown_columns, ["客户自定义列 Z"])
        self.assertEqual(check.unparsed_rows[0]["row"], 9)

    def test_unit_outside_known_set_is_flagged(self):
        check = perception.validate({"document_type": "rfq", "document_confidence": 0.99,
                                     "lines": [_line("PN-0001", "8", unit="PACK")]}, [_table()])
        self.assertTrue(any(issue["kind"] == "unit_unknown" for issue in check.issues))

    def test_bare_value_without_contract_is_tolerated(self):
        # A model that answers {"part_number": "PN-0001"} still gets validated.
        check = perception.validate(
            {"document_type": "rfq", "document_confidence": 0.99,
             "lines": [{"part_number": "PN-0001", "quantity": "8", "unit": "EA"}]}, [_table()])
        self.assertEqual(len(check.lines), 1)


class AuditTests(unittest.TestCase):
    """Code parsed it, model judges it — and the parser's numbers must survive untouched."""

    def _baseline(self, quantity="8"):
        return build_rfq(
            [{"Part Number": "PN-0001", "Quantity": quantity, "Unit Of Measure": "EA",
              "_container": "attachment-xlsx", "_source_row": 2}],
            rfq_number="RFQ-1", evaluation_date="2026-09-07", source_files=["x.eml"])

    CONFIG = {"GENESIS_BASE_URL": "https://x/v1", "GENESIS_MODEL": "stub",
              "GENESIS_ACCESS_TOKEN": "t"}

    def test_parser_numbers_survive_and_requirements_are_added(self):
        result = {"document_type": "rfq", "document_confidence": 0.96,
                  "requirements": ["报价须含证书"], "judgement": "标准询价"}
        with mock.patch.object(perception, "audit", return_value=result):
            rfq, report = perception._run_audit(self.CONFIG, [_table()], self._baseline())
        self.assertEqual(rfq.lines[0].quantity, "8")
        self.assertEqual(report["mode"], "audit")
        self.assertEqual(report["source"], "agent+deterministic")
        self.assertEqual(report["requirements_added"], 1)
        self.assertEqual(rfq.requirements[0].text, "报价须含证书")

    def test_wrong_document_type_is_flagged_but_does_not_block(self):
        result = {"document_type": "quote", "document_confidence": 0.9, "judgement": "像报价单"}
        with mock.patch.object(perception, "audit", return_value=result):
            rfq, report = perception._run_audit(self.CONFIG, [_table()], self._baseline())
        self.assertTrue(rfq.lines)  # advisory, not a veto
        self.assertEqual(report["issues"][0]["kind"], "not_an_rfq")
        self.assertEqual(report["issues"][0]["severity"], "review")

    def test_low_confidence_document_type_is_not_flagged(self):
        result = {"document_type": "quote", "document_confidence": 0.4}
        with mock.patch.object(perception, "audit", return_value=result):
            _, report = perception._run_audit(self.CONFIG, [_table()], self._baseline())
        self.assertEqual(report["issues"], [])

    def test_suspicious_rows_become_review_issues(self):
        result = {"document_type": "rfq", "document_confidence": 0.9,
                  "suspicious_rows": [{"row": 12, "why": "数量列看起来是单价"}]}
        with mock.patch.object(perception, "audit", return_value=result):
            _, report = perception._run_audit(self.CONFIG, [_table()], self._baseline())
        self.assertEqual(report["issues"][0]["kind"], "suspicious_row")
        self.assertIn("单价", report["issues"][0]["detail"])


class BlockTests(unittest.TestCase):
    def test_case_b_exposes_body_and_attachment_blocks(self):
        try:
            path = case_by_key(ROOT, "B").path
        except ValueError:
            self.skipTest("官方素材不在工作区")
        blocks = perception.list_blocks(path)
        kinds = {block.kind for block in blocks}
        self.assertIn("attachment-xlsx", kinds)
        self.assertTrue(all(block.line_count for block in blocks))

    def test_payload_is_numbered_for_anchoring(self):
        rendered = perception.render_blocks([_table()])
        self.assertIn("[b3]", rendered)
        self.assertIn("   2| ", rendered)


def _eml(html=None, text=None, attachments=()):
    """A real .eml, so list_blocks is tested through the same path as production."""
    import email.message

    message = email.message.EmailMessage()
    message["Subject"] = "RFQ-20260902"
    message["From"] = "Buyer <rfq-b@example.com>"
    message["Date"] = "Mon, 7 Sep 2026 10:00:00 +0000"
    if text:
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")
    elif html:
        message.set_content(html, subtype="html")
    for name, payload in attachments:
        message.add_attachment(payload, maintype="application", subtype="octet-stream",
                               filename=name)
    return message.as_bytes()


def _docx(paragraphs):
    import io
    import zipfile

    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


class EntryBlocksTest(unittest.TestCase):
    """The entry must carry the mail's wording and every attachment, not one of them."""

    def _blocks(self, **kwargs):
        """Written under runtime/ on purpose: mkdtemp() loses ACLs and the sandbox
        then denies the cleanup chmod (same reason as make_temp_dir in test_golden_b)."""
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"mail-{uuid4().hex[:12]}.eml"
        path.write_bytes(_eml(**kwargs))
        try:
            return perception.list_blocks(path)
        finally:
            path.unlink(missing_ok=True)

    def test_html_only_mail_keeps_its_wording(self):
        # No text/plain part: reading mail.text alone used to return an empty body.
        blocks = self._blocks(html="<p>Please quote DDP Shanghai by 15 Sep.</p>"
                                   "<table><tr><td>Part Number</td></tr></table>")
        prose = [block for block in blocks if block.kind == "body-text"]
        self.assertEqual(len(prose), 1)
        self.assertIn("DDP Shanghai", prose[0].text)

    def test_prose_does_not_swallow_table_cells(self):
        blocks = self._blocks(html="<p>Notes below.</p>"
                                   "<table><tr><td>PN-0001</td></tr></table>")
        prose = next(block for block in blocks if block.kind == "body-text")
        self.assertNotIn("PN-0001", prose.text)

    def test_each_table_keeps_its_own_header(self):
        html = ("<table><tr><td>Disclaimer</td><td>page 1</td></tr></table>"
                "<table><tr><td>Part Number</td><td>Quantity</td></tr>"
                "<tr><td>PN-0001</td><td>8</td></tr></table>")
        tables = [block for block in self._blocks(html=html) if block.kind == "body-html"]
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[1].rows[0][0], "Part Number")

    def test_docx_attachment_is_read_not_just_reported(self):
        blocks = self._blocks(html="<p>See attached.</p>",
                              attachments=[("quote.docx", _docx(["Payment terms: NET 30"]))])
        doct = [block for block in blocks if block.kind == "attachment-docx"]
        self.assertEqual(len(doct), 1)
        self.assertIn("NET 30", doct[0].text)

    def test_unsupported_attachment_is_reported(self):
        blocks = self._blocks(html="<p>See attached.</p>",
                              attachments=[("photo.zip", b"PK\x03\x04")])
        other = [block for block in blocks if block.kind == "attachment-other"]
        self.assertEqual(len(other), 1)
        self.assertIn(".zip", other[0].note)


class RequirementsMergeTests(unittest.TestCase):
    """Prose requirements are additive: the regex finding one must not hide the rest."""

    CONFIG = {"GENESIS_BASE_URL": "https://x/v1", "GENESIS_MODEL": "stub",
              "GENESIS_ACCESS_TOKEN": "t"}

    def _baseline(self):
        return build_rfq(
            [{"Part Number": "PN-0001", "Quantity": "8", "Unit Of Measure": "EA",
              "_container": "attachment-xlsx", "_source_row": 2}],
            rfq_number="RFQ-1", evaluation_date="2026-09-07", source_files=["x.eml"],
            requirements=[Requirement(text="1. 报价须含运费", source="邮件正文")])

    def test_model_findings_are_appended_and_deduplicated(self):
        result = {"document_type": "rfq", "document_confidence": 0.96,
                  "requirements": ["1. 报价须含运费", "付款条件 NET 30"]}
        with mock.patch.object(perception, "audit", return_value=result):
            rfq, report = perception._run_audit(self.CONFIG, [_table()], self._baseline())
        self.assertEqual(report["requirements_added"], 1)
        texts = [item.text for item in rfq.requirements]
        self.assertIn("付款条件 NET 30", texts)
        self.assertEqual(texts.count("1. 报价须含运费"), 1)


class RunTests(unittest.TestCase):
    """The main path end to end, with the model stubbed."""

    def setUp(self):
        try:
            self.case = case_by_key(ROOT, "B")
        except ValueError:
            self.skipTest("官方素材不在工作区")
        self.config = {"GENESIS_BASE_URL": "https://x/v1", "GENESIS_MODEL": "stub",
                       "GENESIS_ACCESS_TOKEN": "t"}

    def test_readable_mail_goes_through_audit(self):
        audit_result = {"document_type": "rfq", "document_confidence": 0.95,
                        "judgement": "标准询价"}
        with mock.patch.object(perception, "audit", return_value=audit_result):
            rfq, report = perception.run(self.config, self.case.path)
        self.assertEqual(report["mode"], "audit")
        self.assertEqual(report["source"], "agent+deterministic")
        self.assertTrue(rfq.lines)
        self.assertEqual(len(rfq.lines), report["lines_deterministic"])

    def test_unreadable_mail_falls_to_model_extraction(self):
        extraction = {"document_type": "rfq", "document_confidence": 0.99,
                      "lines": [_line("PN-0001", "8")]}
        with mock.patch.object(perception, "_deterministic",
                               return_value=(None, "表头无法识别")):
            with mock.patch.object(perception, "extract", return_value=extraction):
                with mock.patch.object(perception, "_model_rfq") as fake:
                    fake.return_value = "RFQ"
                    rfq, report = perception.run(self.config, self.case.path)
        self.assertEqual(report["source"], "agent")
        self.assertEqual(report["lines_model"], 1)
        self.assertEqual(rfq, "RFQ")

    def test_unusable_mail_raises_instead_of_vanishing(self):
        with mock.patch.object(perception, "_deterministic", return_value=(None, "解析失败")):
            with mock.patch.object(perception, "extract", return_value={"document_type": "other"}):
                with self.assertRaises(perception.PerceptionError):
                    perception.run(self.config, self.case.path)

    def test_missing_credentials_raise(self):
        with self.assertRaises(perception.PerceptionError):
            perception.run({"GENESIS_BASE_URL": "", "GENESIS_MODEL": "",
                            "GENESIS_ACCESS_TOKEN": ""}, self.case.path)

    def test_paging_keeps_original_row_numbers(self):
        rows = [["Part Number", "Quantity", "Unit Of Measure"]]
        rows += [[f"PN-{n:04d}", "5", "EA"] for n in range(1, 121)]
        big = perception.Block("b3", "attachment-xlsx", "big", rows=rows)
        chunks = perception._chunks([big])
        self.assertEqual(len(chunks), 3)  # 121 lines at 50 per page
        rendered = perception._render_chunk(big, 51, 100)
        self.assertIn("  51| ", rendered)
        self.assertIn("第 51–100 行", rendered)


if __name__ == "__main__":
    unittest.main()