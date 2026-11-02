"""The evaluation gate must detect missing and inconsistent provider output."""

import unittest

from rfq_agent.evaluation import validate
from rfq_agent.models import RFQ


def _rfq():
    return RFQ.from_dict({
        "rfq_number": "CHECK-1", "evaluation_date": "2026-11-02",
        "source_files": ["fixture.eml"],
        "lines": [{"line_id": "L1", "part_number": "PN-1",
                   "quantity": "2", "unit": "EA"}],
    })


def _result():
    return {
        "line_id": "L1", "part_number": "PN-1", "quantity": "2", "unit": "EA",
        "eligibility": "priced", "unit_price": "12.00", "priced_quantity": "2",
        "line_total": "24.00",
        "decision": {"selected_source_id": "STOCK-1"},
        "internal_candidates": [{"source_id": "STOCK-1", "eligibility": "eligible"}],
        "supplier_candidates": [],
    }


class CompletenessTests(unittest.TestCase):
    def test_valid_result_has_no_contract_defects(self):
        self.assertEqual(validate(_rfq(), [_result()]), [])

    def test_missing_or_duplicate_line_fails(self):
        for rows in ([], [_result(), _result()]):
            with self.subTest(count=len(rows)):
                issues = validate(_rfq(), rows)
                self.assertTrue(any(x.startswith("result_present:L1") for x in issues))

    def test_changed_input_and_money_fail_independently(self):
        row = _result()
        row["quantity"] = "3"
        row["line_total"] = "25.00"
        issues = validate(_rfq(), [row])
        self.assertIn("input_identity:L1", issues)
        self.assertIn("amount_consistency:L1", issues)
        self.assertNotIn("candidate_evidence:L1", issues)

    def test_selected_rejected_candidate_fails(self):
        row = _result()
        row["internal_candidates"][0]["eligibility"] = "rejected"
        issues = validate(_rfq(), [row])
        self.assertIn("candidate_evidence:L1", issues)


if __name__ == "__main__":
    unittest.main()
