"""§8.1.3 parse-quality findings, checked against the real material."""

import unittest
from pathlib import Path

from rfq_agent.cases import load_case
from rfq_agent.quality import inspect, summary

ROOT = Path(__file__).resolve().parents[1]


def findings(key: str) -> list[dict]:
    rfq, _ = load_case(ROOT, key)
    return inspect(rfq.to_dict())


def kinds(key: str) -> list[str]:
    return [item["kind"] for item in findings(key)]


class QualityTest(unittest.TestCase):
    def test_case_a_reports_the_ten_duplicated_part_numbers(self):
        duplicate = next(item for item in findings("A") if item["kind"] == "duplicate_part")
        self.assertIn("10 个料号", duplicate["detail"])
        self.assertIn("14 行重复", duplicate["detail"])
        self.assertIn("PN-0080 ×4", duplicate["examples"])

    def test_case_a_mixes_ea_and_inch(self):
        mixed = next(item for item in findings("A") if item["kind"] == "unit_mix")
        self.assertIn("EA 116 行", mixed["detail"])
        self.assertIn("IN 6 行", mixed["detail"])

    def test_case_b_has_one_duplicate_pair(self):
        duplicate = next(item for item in findings("B") if item["kind"] == "duplicate_part")
        self.assertEqual(duplicate["count"], 1)
        self.assertEqual(duplicate["examples"], ["PN-0001 ×2"])

    def test_case_d_mixes_ea_and_feet_and_has_no_duplicates(self):
        self.assertIn("unit_mix", kinds("D"))
        self.assertNotIn("duplicate_part", kinds("D"))

    def test_pdf_cases_report_carrier_gaps_without_calling_them_defects(self):
        for key in ("C", "F"):
            self.assertEqual(summary(findings(key))["warn"], 0, key)
            info = {item["kind"] for item in findings(key) if item["severity"] == "info"}
            self.assertEqual(info, {"missing_priority", "missing_due_at", "missing_condition"})

    def test_findings_point_back_at_rows(self):
        duplicate = next(item for item in findings("A") if item["kind"] == "duplicate_part")
        self.assertTrue(duplicate["lines"])
        self.assertTrue(all(str(line).startswith("L") for line in duplicate["lines"]))

    def test_partial_missing_field_is_a_warning_not_a_carrier_gap(self):
        missing = next(item for item in findings("A") if item["kind"] == "missing_priority")
        self.assertEqual(missing["severity"], "warn")
        self.assertIn("34/122", missing["detail"])

    def test_every_case_is_inspectable_and_findings_are_unique(self):
        for key in ("A", "B", "C", "D", "E", "F"):
            listed = kinds(key)
            self.assertEqual(len(listed), len(set(listed)), key)


if __name__ == "__main__":
    unittest.main()