"""§8.1.3 parse-quality findings, checked against the real material."""

import unittest
from pathlib import Path

from rfq_agent.application.cases import load_case
from rfq_agent.domain.quality import inspect, summary

ROOT = Path(__file__).resolve().parents[1]


def findings(key: str) -> list[dict]:
    rfq, _ = load_case(ROOT, key)
    return inspect(rfq.to_dict())


def kinds(key: str) -> list[str]:
    return [item["kind"] for item in findings(key)]


class QualityTest(unittest.TestCase):
    def test_case_a_flags_repetition_within_the_same_inquiry_number(self):
        duplicate = next(item for item in findings("A") if item["kind"] == "duplicate_part")
        self.assertEqual(duplicate["severity"], "warn")
        self.assertIn("L079", duplicate["lines"])
        self.assertIn("L088", duplicate["lines"])
        self.assertIn("询价号", duplicate["detail"])

    def test_case_a_lists_ea_and_inch_without_calling_the_mix_an_error(self):
        mixed = next(item for item in findings("A") if item["kind"] == "unit_mix")
        self.assertIn("EA 116 行", mixed["detail"])
        self.assertIn("IN 6 行", mixed["detail"])
        self.assertEqual(mixed["severity"], "info")

    def test_case_b_part_is_repeated_across_distinct_inquiry_numbers(self):
        self.assertNotIn("duplicate_part", kinds("B"))
        repeated = next(item for item in findings("B")
                        if item["kind"] == "repeated_part_across_inquiries")
        self.assertEqual(repeated["severity"], "info")
        self.assertEqual(repeated["examples"], ["PN-0001 ×2"])
        self.assertEqual(summary(findings("B"))["warn"], 0)

    def test_case_d_mixes_ea_and_feet_and_has_no_conflict(self):
        self.assertIn("unit_mix", kinds("D"))
        self.assertNotIn("duplicate_part", kinds("D"))
        self.assertNotIn("unit_conflict", kinds("D"))

    def test_pdf_cases_report_carrier_gaps_without_calling_them_defects(self):
        for key in ("C", "F"):
            self.assertEqual(summary(findings(key))["warn"], 0, key)
            info = {item["kind"] for item in findings(key) if item["severity"] == "info"}
            self.assertEqual(info, {"missing_due_at", "missing_condition"})

    def test_findings_point_back_at_rows(self):
        duplicate = next(item for item in findings("A") if item["kind"] == "duplicate_part")
        self.assertTrue(duplicate["lines"])
        self.assertTrue(all(str(line).startswith("L") for line in duplicate["lines"]))

    def test_unstated_priority_is_not_a_review_finding(self):
        self.assertNotIn("missing_priority", kinds("A"))
        self.assertNotIn("missing_priority", kinds("B"))

    def test_same_inquiry_and_part_with_different_units_is_a_warning(self):
        rfq = {"lines": [
            {"line_id": "L001", "inquiry_no": "9", "part_number": "P1", "unit": "EA",
             "priority": "", "due_at": "", "condition_accept": []},
            {"line_id": "L002", "inquiry_no": "9", "part_number": "P1", "unit": "IN",
             "priority": "", "due_at": "", "condition_accept": []}],
               "evaluation_date": "2026-09-01"}
        issue = next(item for item in inspect(rfq) if item["kind"] == "unit_conflict")
        self.assertEqual(issue["severity"], "warn")
        self.assertEqual(issue["lines"], ["L001", "L002"])

    def test_every_case_is_inspectable_and_findings_are_unique(self):
        for key in ("A", "B", "C", "D", "E", "F"):
            listed = kinds(key)
            self.assertEqual(len(listed), len(set(listed)), key)


if __name__ == "__main__":
    unittest.main()
