"""Customer terms must be checked against the draft, not just quoted back."""

import unittest
from dataclasses import replace
from pathlib import Path

from rfq_agent import terms
from rfq_agent.cases import load_case
from rfq_agent.models import Requirement

ROOT = Path(__file__).resolve().parents[1]


class TermsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rfq = {}
        cls.results = {}
        for key in ("B", "C", "F"):
            rfq, provider = load_case(ROOT, key)
            cls.rfq[key] = rfq
            cls.results[key] = provider.evaluate(rfq)

    def checks(self, key, rfq=None, results=None):
        return terms.check(rfq or self.rfq[key], results or self.results[key])

    def test_chinese_cases_carry_no_software_checks(self):
        for key in ("A", "B", "D", "E"):
            with self.subTest(case=key):
                rfq, provider = load_case(ROOT, key)
                self.assertEqual(terms.check(rfq, provider.evaluate(rfq)), [])

    def test_each_english_requirement_maps_to_its_check(self):
        items = self.checks("C")
        self.assertEqual([item["id"] for item in items],
                         [f"RQ-{n:02d}" for n in range(1, 8)])
        self.assertEqual([item["check"] for item in items],
                         ["lead_time", "validity", "compliance", "form",
                          "columns", "remarks", "serialized"])

    def test_form_requirement_is_met_by_construction(self):
        form = next(item for item in self.checks("C") if item["check"] == "form")
        self.assertEqual(form["status"], terms.MET)

    def test_compliance_is_never_claimed_as_satisfied(self):
        compliance = next(item for item in self.checks("C") if item["check"] == "compliance")
        self.assertEqual(compliance["status"], terms.MANUAL)
        self.assertIn("合规声明", compliance["detail"])

    def test_unpriced_case_reports_todo_instead_of_pretending(self):
        summary = terms.summary(self.checks("C"))
        self.assertEqual(summary["total"], 7)
        self.assertEqual(summary["met"], 1)
        self.assertEqual(summary["todo"], 4)
        self.assertEqual(summary["na"], 1)

    def test_checks_read_the_real_priced_rows(self):
        """Attach C's requirements to B: delivery lead time is missing on 5 of 19 rows."""
        rfq = replace(self.rfq["B"], requirements=self.rfq["C"].requirements)
        items = terms.check(rfq, self.results["B"])
        lead_time = next(item for item in items if item["check"] == "lead_time")
        self.assertEqual(lead_time["status"], terms.TODO)
        self.assertIn("5/19", lead_time["detail"])

        columns = next(item for item in items if item["check"] == "columns")
        self.assertEqual(columns["status"], terms.TODO)
        self.assertIn("交期", columns["detail"])

    def test_remarks_stay_not_applicable_until_an_alternate_is_quoted(self):
        rfq = replace(self.rfq["B"], requirements=self.rfq["C"].requirements)
        items = terms.check(rfq, self.results["B"])
        remarks = next(item for item in items if item["check"] == "remarks")
        self.assertEqual(remarks["status"], terms.NA)
        self.assertIn("没有使用替代件状", remarks["detail"])

    def test_unrecognised_requirement_asks_a_human(self):
        rfq = replace(self.rfq["C"], requirements=[
            Requirement(text="Please include a certificate of origin.",
                        source="邮件正文")])
        items = terms.check(rfq, self.results["C"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], terms.MANUAL)
        self.assertEqual(items[0]["check"], "unmatched")

    def test_summary_counts_every_status(self):
        summary = terms.summary(self.checks("C"))
        self.assertEqual(sum(summary[key] for key in ("met", "todo", "manual", "na")),
                         summary["total"])

    def test_needs_compliance_detects_the_english_cases(self):
        self.assertTrue(terms.needs_compliance(self.checks("C")))
        self.assertTrue(terms.needs_compliance(self.checks("F")))
        self.assertFalse(terms.needs_compliance(self.checks("B")))


if __name__ == "__main__":
    unittest.main()