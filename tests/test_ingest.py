"""Every official mail must go through the same pipeline, whatever carries the table."""
import unittest
from pathlib import Path

from rfq_agent.cases import discover, load_case
from rfq_agent.pricing import build_review_items, summarize

ROOT = Path(__file__).parents[1]

EXPECTED = {
    "A": ("body-html", 122),
    "B": ("attachment-xlsx", 21),
    "C": ("attachment-pdf", 7),
    "D": ("body-html", 122),
    "E": ("attachment-xlsx", 21),
    "F": ("attachment-pdf", 7),
}


class IngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.key: case for case in discover(ROOT)}

    def test_all_six_mails_are_discovered(self):
        # Uploaded mails (UP-*) may exist on a machine after a demo; they must not hide
        # or rename the six official cases.
        official = sorted(key for key in self.cases if not key.startswith("UP-"))
        self.assertEqual(official, sorted(EXPECTED))

    def test_each_container_is_recognised(self):
        for key, (container, _) in EXPECTED.items():
            with self.subTest(case=key):
                self.assertEqual(self.cases[key].container, container)

    def test_line_counts_match_the_material(self):
        total = 0
        for key, (_, lines) in EXPECTED.items():
            with self.subTest(case=key):
                rfq, _ = load_case(ROOT, key)
                self.assertEqual(len(rfq.lines), lines)
                total += len(rfq.lines)
        self.assertEqual(total, 300)

    def test_pdf_rows_keep_identity_and_quantity(self):
        expected = {"C": ("GSK-001", "GSK-007"), "F": ("CPL-501", "CPL-507")}
        for key, (first, last) in expected.items():
            with self.subTest(case=key):
                rfq, _ = load_case(ROOT, key)
                self.assertEqual(rfq.lines[0].part_number, first)
                self.assertEqual(rfq.lines[-1].part_number, last)
                self.assertTrue(all(line.quantity.isdigit() for line in rfq.lines))

    def test_html_and_text_bodies_keep_priority_and_condition(self):
        for key in ("A", "D"):
            with self.subTest(case=key):
                rfq, _ = load_case(ROOT, key)
                self.assertTrue(any(line.priority for line in rfq.lines))
                self.assertTrue(all(line.condition_accept == ["FN"] for line in rfq.lines))

    def test_coverage_is_reported_honestly(self):
        """A and B have price evidence; C-F have none and must say so, not invent one."""
        expected = {"A": 109, "B": 19, "C": 0, "D": 0, "E": 0, "F": 0}
        for key, priced in expected.items():
            with self.subTest(case=key):
                rfq, provider = load_case(ROOT, key)
                results = provider.evaluate(rfq)
                self.assertEqual(summarize(results)["priced_lines"], priced)
                self.assertTrue(all(row["unit_price"] is None
                                    for row in results if row["eligibility"] != "priced"))

    def test_zero_coverage_asks_one_question_not_one_per_line(self):
        for key in ("C", "D", "E", "F"):
            with self.subTest(case=key):
                rfq, provider = load_case(ROOT, key)
                items = build_review_items(rfq, provider.evaluate(rfq))
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["id"], "RI-TASK")

    def test_pdf_customer_has_no_condition_requirement(self):
        """The Quote.pdf form leaves 件状 for the supplier to declare."""
        rfq, _ = load_case(ROOT, "C")
        self.assertTrue(all(line.condition_accept == [] for line in rfq.lines))

    def test_english_cases_keep_the_quoting_terms(self):
        """§9: C/F must yield the buyer's requirements, not just the table."""
        for key in ("C", "F"):
            with self.subTest(case=key):
                rfq, _ = load_case(ROOT, key)
                self.assertEqual(len(rfq.requirements), 7)
                body = [item for item in rfq.requirements if item.source == "邮件正文"]
                pdf = [item for item in rfq.requirements if item.source == "Quote.pdf"]
                self.assertEqual(len(body), 3)
                self.assertEqual(len(pdf), 4)
                self.assertIn("Validity period of the quotation", body[1].text)
                self.assertTrue(pdf[0].text.startswith("Use this form"))

    def test_chinese_html_cases_add_no_invented_requirements(self):
        for key in ("A", "B", "D", "E"):
            with self.subTest(case=key):
                rfq, _ = load_case(ROOT, key)
                self.assertEqual(rfq.requirements, [])


class HtmlTableSelectionTests(unittest.TestCase):
    """A body may hold more than one table; the inquiry is not always the first."""

    def test_disclaimer_table_does_not_hide_the_inquiry_table(self):
        from rfq_agent.ingest import _rows_from_html

        markup = ("<table><tr><td>Disclaimer</td><td>legal notice</td></tr></table>"
                  "<table><tr><td>Part Number</td><td>Quantity</td><td>Unit Of Measure</td></tr>"
                  "<tr><td>PN-0001</td><td>8</td><td>EA</td></tr></table>")
        rows = _rows_from_html(markup)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Part Number"], "PN-0001")
        self.assertEqual(rows[0]["_sheet"], "正文表格 2")

    def test_no_usable_table_raises_with_the_reason(self):
        from rfq_agent.ingest import _rows_from_html

        with self.assertRaises(ValueError) as caught:
            _rows_from_html("<p>no table here</p>")
        self.assertIn("没有可用的表格", str(caught.exception))


if __name__ == "__main__":
    unittest.main()