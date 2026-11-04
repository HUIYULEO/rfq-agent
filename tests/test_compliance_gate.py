"""Compliance gate: certificates, PMA, SPECIAL REQUIREMENT, units and part-number matching.

Synthetic rows isolate one rule at a time; the official cases check the rules do not
misfire on real material.
"""
import unittest
from decimal import Decimal
from pathlib import Path

from rfq_agent.application.cases import load_case
from rfq_agent.domain.compliance import match_type, normalize_pn, required_certificates
from rfq_agent.domain.business_rules import check_business
from rfq_agent.domain.evaluation import validate
from rfq_agent.domain.models import Line, RFQ
from rfq_agent.domain.policy import line_amount
from rfq_agent.domain.pricing import PriceTableQuoteProvider, build_review_items

ROOT = Path(__file__).resolve().parents[1]


def rfq_with(**line_fields) -> RFQ:
    fields = {"line_id": "L001", "part_number": "PN-9", "quantity": "2", "unit": "EA",
              "condition_accept": ["FN"]}
    fields.update(line_fields)
    if "product_requirement" in fields and "required_certificates" not in fields:
        fields["required_certificates"] = required_certificates(fields["product_requirement"])
    return RFQ(rfq_number="GATE", evaluation_date="2026-09-07", source_files=["gate.eml"],
               lines=[Line(**fields)])


def supplier(source_id, certificate, price, pn="PN-9", **extra):
    row = {"_source_row": int(source_id[-2:]), "Part Number": pn, "价格资源编号": source_id,
           "供应商编码": "S1", "供应商名称": "Supplier", "价格状态": "有效", "最小起订量": 1,
           "件状条件": "FN", "币种": "CNY", "单价": price, "证书类型": certificate}
    row.update(extra)
    return row


def stock(pn="PN-9", unit="EA", qty=10, condition="FN", row=3):
    return {"_source_row": row, "Part Number": pn, "库存数量": qty, "库存件状": condition,
            "标准成本": 50, "币种": "CNY", "Unit Of Measure": unit, "仓库": "WH"}


class CertificateTests(unittest.TestCase):
    def test_requirement_text_maps_to_certificates(self):
        self.assertEqual(required_certificates("CERT REQUIRED"), ["EASA Form 1", "FAA 8130-3"])
        self.assertEqual(required_certificates("need 8130-3"), ["FAA 8130-3"])
        self.assertEqual(required_certificates("SPECIAL REQUIREMENT"), [])
        self.assertEqual(required_certificates(""), [])

    def test_cert_requirement_rejects_pma_and_coc_and_picks_airworthy_source(self):
        rfq = rfq_with(product_requirement="CERT REQUIRED")
        provider = PriceTableQuoteProvider([
            supplier("PR01", "FAA 8130-3 (PMA)", 10),
            supplier("PR02", "CoC", 11),
            supplier("PR03", "FAA 8130-3", 30),
        ], [])
        row = provider.evaluate(rfq)[0]
        self.assertEqual(row["eligibility"], "priced")
        self.assertEqual(row["decision"]["selected_source_id"], "PR03")
        rejected = {c["source_id"]: [r["rule"] for r in c["rejection_reasons"]]
                    for c in row["supplier_candidates"]}
        self.assertIn("cert_mismatch", rejected["PR01"])
        self.assertIn("cert_mismatch", rejected["PR02"])
        findings, issues = check_business(rfq, [row])
        cert = next(f for f in findings if f["rule"] == "certificate")
        self.assertEqual(cert["status"], "pass")
        self.assertEqual(issues, [])

    def test_no_compliant_source_blocks_with_concrete_options(self):
        rfq = rfq_with(product_requirement="CERT REQUIRED")
        provider = PriceTableQuoteProvider([supplier("PR01", "FAA 8130-3 (PMA)", 10)], [])
        row = provider.evaluate(rfq)[0]
        self.assertEqual(row["eligibility"], "needs_review")
        items = build_review_items(rfq, [row])
        # Single unpriced line collapses to RI-TASK; price a second line to see line options.
        self.assertEqual(items[0]["id"], "RI-TASK")
        two = RFQ(rfq_number="GATE", evaluation_date="2026-09-07", source_files=["g.eml"],
                  lines=[rfq.lines[0], Line(line_id="L002", part_number="PN-8", quantity="1",
                                            unit="EA", condition_accept=["FN"])])
        provider = PriceTableQuoteProvider([supplier("PR01", "FAA 8130-3 (PMA)", 10),
                                            supplier("PR05", "EASA Form 1", 10, pn="PN-8")], [])
        results = provider.evaluate(two)
        item = next(i for i in build_review_items(two, results) if i["id"] == "RI-0001")
        options = [o["id"] for o in item["options"]]
        self.assertEqual(options, ["request_quotation", "request_cert", "accept_noncert"])
        self.assertIn("cert_mismatch", item["kinds"])
        self.assertTrue(item["locator"])
        accepted = provider.evaluate(two, {"L001": "accept_noncert"})[0]
        self.assertEqual(accepted["eligibility"], "priced")
        self.assertTrue(any("已人工接受非要求证书" in w for w in accepted["decision"]["warnings"]))
        findings, issues = check_business(two, provider.evaluate(two, {"L001": "accept_noncert"}))
        self.assertEqual(issues, [])
        self.assertEqual(next(f for f in findings if f["line_id"] == "L001"
                              and f["rule"] == "certificate")["status"], "warn")

    def test_stock_cannot_prove_a_required_certificate(self):
        rfq = rfq_with(product_requirement="CERT REQUIRED")
        provider = PriceTableQuoteProvider([supplier("PR04", "EASA Form 1", 90)], [stock()])
        row = provider.evaluate(rfq)[0]
        self.assertEqual(row["decision"]["recommended_source"], "supplier")
        self.assertIn("cert_unverified",
                      [r["rule"] for r in row["internal_candidates"][0]["rejection_reasons"]])
        confirmed = provider.evaluate(rfq, {"L001": "confirm_stock_cert"})[0]
        self.assertEqual(confirmed["decision"]["recommended_source"], "internal")

    def test_stock_without_certificate_is_never_high_confidence(self):
        rfq = rfq_with()
        row = PriceTableQuoteProvider([], [stock()]).evaluate(rfq)[0]
        self.assertEqual(row["decision"]["recommended_source"], "internal")
        self.assertEqual(row["decision"]["confidence"], "medium")
        self.assertIn("stock_no_cert", [f["kind"] for f in row["decision"]["flags"]])
        findings, _ = check_business(rfq, [row])
        cert = next(f for f in findings if f["rule"] == "certificate")
        self.assertEqual(cert["status"], "warn")
        self.assertIn("证书", cert["suggested_action"])

    def test_pma_choice_is_a_blocking_question(self):
        rfq = rfq_with()
        provider = PriceTableQuoteProvider([supplier("PR01", "FAA 8130-3 (PMA)", 10),
                                            supplier("PR02", "EASA Form 1", 14)], [])
        results = provider.evaluate(rfq)
        self.assertEqual(results[0]["decision"]["selected_source_id"], "PR01")
        items = build_review_items(rfq, results)
        self.assertEqual([i["id"] for i in items], ["RI-0001-PMA"])
        self.assertEqual([o["id"] for o in items[0]["options"]],
                         ["accept_pma", "use_certified", "request_cert", "skip_line"])
        certified = provider.evaluate(rfq, {"L001:pma_selected": "use_certified"})[0]
        self.assertEqual(certified["decision"]["selected_source_id"], "PR02")
        skipped = provider.evaluate(rfq, {"L001:pma_selected": "skip_line"})
        self.assertEqual(skipped[0]["eligibility"], "needs_review")
        answered = build_review_items(rfq, skipped, {"L001:pma_selected": "skip_line"})
        self.assertEqual([(i["id"], i["status"]) for i in answered], [("RI-0001-PMA", "answered")])


    def test_pma_review_can_be_switched_off_by_policy(self):
        from rfq_agent.domain.policy import DEFAULT_POLICY

        rfq = rfq_with()
        rows = [supplier("PR01", "FAA 8130-3 (PMA)", 10), supplier("PR02", "EASA Form 1", 14)]
        relaxed = PriceTableQuoteProvider(rows, [], policy={**DEFAULT_POLICY,
                                                            "pma_requires_review": False})
        self.assertEqual(build_review_items(rfq, relaxed.evaluate(rfq)), [])


class SpecialRequirementTests(unittest.TestCase):
    def test_priced_special_line_gets_a_dedicated_review_item(self):
        rfq = rfq_with(product_requirement="SPECIAL REQUIREMENT")
        provider = PriceTableQuoteProvider([supplier("PR02", "EASA Form 1", 14)], [])
        results = provider.evaluate(rfq)
        items = build_review_items(rfq, results)
        self.assertEqual([i["id"] for i in items], ["RI-0001-SR"])
        self.assertEqual(items[0]["resolution_key"], "L001:special_requirement")
        self.assertEqual([o["id"] for o in items[0]["options"]],
                         ["confirm_requirement", "request_quotation"])

    def test_official_case_a_flags_its_special_requirement_lines(self):
        rfq, provider = load_case(ROOT, "A")
        special = [line for line in rfq.lines if line.product_requirement]
        self.assertEqual([line.part_number for line in special], ["PN-0029", "PN-0058", "PN-0087"])
        self.assertTrue(all(line.mpn and line.manual_pn for line in special))
        results = provider.evaluate(rfq)
        ids = {item["id"]: item for item in build_review_items(rfq, results)}
        self.assertIn("RI-0030-SR", ids)
        self.assertIn("RI-0060-SR", ids)
        self.assertIn("special_requirement", ids["RI-0101"]["kinds"])   # also blocked by MOQ
        self.assertEqual(ids["RI-0030-SR"]["locator"]["raw_value"], "SPECIAL REQUIREMENT")


class UnitAndMatchTests(unittest.TestCase):
    def test_unit_mismatch_is_not_converted(self):
        rfq = rfq_with(unit="FT")
        provider = PriceTableQuoteProvider([], [stock(unit="IN")])
        row = provider.evaluate(rfq)[0]
        self.assertEqual(row["eligibility"], "needs_review")
        self.assertIn("uom_mismatch", [r["rule"] for r in row["decision"]["review_evidence"]])
        confirmed = provider.evaluate(rfq, {"L001": "confirm_uom"})[0]
        self.assertEqual(confirmed["eligibility"], "priced")
        self.assertEqual(confirmed["priced_quantity"], "2")       # quantity untouched

    def test_supplier_unit_is_checked_against_material_master(self):
        rfq = rfq_with(unit="EA")
        provider = PriceTableQuoteProvider([supplier("PR02", "EASA Form 1", 14)],
                                           [stock(unit="IN", qty=0)])
        row = provider.evaluate(rfq)[0]
        rules = {r["rule"] for c in row["supplier_candidates"] for r in c["rejection_reasons"]}
        self.assertIn("uom_mismatch", rules)

    def test_normalized_and_alias_matches_need_confirmation(self):
        self.assertEqual(normalize_pn(" pn-0002 "), normalize_pn("PN 0002"))
        line = Line(line_id="L001", part_number="PN-0002", quantity="1", unit="EA",
                    mpn="MPN-77")
        self.assertEqual(match_type("PN-0002", line), "exact")
        self.assertEqual(match_type("pn 0002", line), "normalized")
        self.assertEqual(match_type("mpn77", line), "alias")
        self.assertIsNone(match_type("PN-0003", line))

        rfq = rfq_with(part_number="PN-0002")
        provider = PriceTableQuoteProvider([supplier("PR02", "EASA Form 1", 14, pn="pn 0002")], [])
        row = provider.evaluate(rfq)[0]
        self.assertEqual(row["eligibility"], "needs_review")
        self.assertEqual(row["supplier_candidates"][0]["match_type"], "normalized")
        confirmed = provider.evaluate(rfq, {"L001": "confirm_match"})[0]
        self.assertEqual(confirmed["decision"]["match_type"], "normalized")
        findings, issues = check_business(rfq, [confirmed])
        self.assertEqual(next(f for f in findings if f["rule"] == "pn_match")["status"], "warn")
        self.assertEqual(issues, [])

    def test_pdf_units_are_kept_as_printed(self):
        rfq, _ = load_case(ROOT, "C")
        self.assertEqual({line.unit for line in rfq.lines}, {"Each"})
        self.assertTrue(all(line.evidence.get("pdf_line") for line in rfq.lines))
        self.assertTrue(all(line.evidence.get("attachment") == "Quote.pdf" for line in rfq.lines))


class AmountAlignmentTests(unittest.TestCase):
    def test_fractional_quantity_uses_one_rounding_rule(self):
        rfq = rfq_with(quantity="2.5", unit="IN")
        provider = PriceTableQuoteProvider([], [{**stock(unit="IN"), "标准成本": "11.11"}])
        row = provider.evaluate(rfq)[0]
        self.assertEqual(row["line_total"], str(line_amount(row["unit_price"], "2.5")))
        self.assertNotEqual(Decimal(row["line_total"]), Decimal(row["unit_price"]) * Decimal("2.5"))
        self.assertEqual(validate(rfq, [row]), [])


class BusinessChecksTests(unittest.TestCase):
    def test_every_line_gets_every_rule_including_passes(self):
        from rfq_agent.domain.business_rules import BUSINESS_RULES

        rfq, provider = load_case(ROOT, "B")
        checks, issues = check_business(rfq, provider.evaluate(rfq))
        self.assertEqual(issues, [])
        self.assertEqual({(c["line_id"], c["rule"]) for c in checks},
                         {(line.line_id, rule) for line in rfq.lines for rule in BUSINESS_RULES})
        self.assertIn("pass", {c["status"] for c in checks})
        self.assertTrue(all(c["locator"]["text"] for c in checks))

    def test_task_stores_business_checks_and_old_tasks_default_empty(self):
        from rfq_agent.domain.models import Task

        restored = Task.from_dict({"task_id": "0" * 32, "rfq": rfq_with().to_dict()})
        self.assertEqual(restored.business_checks, [])
        self.assertEqual(restored.approval, {})


class LocatorTests(unittest.TestCase):
    def test_xlsx_and_html_findings_point_to_the_cell(self):
        rfq, provider = load_case(ROOT, "B")
        findings, _ = check_business(rfq, provider.evaluate(rfq))
        loc = next(f for f in findings if f["line_id"] == "L001" and f["rule"] == "uom")["locator"]
        self.assertEqual(loc["attachment"], "询价报价清单.xlsx")
        self.assertTrue(loc["sheet"])
        self.assertEqual(loc["row"], 2)
        self.assertEqual(loc["column"], "Unit Of Measure")
        self.assertTrue(loc["column_letter"])
        self.assertEqual(loc["raw_value"], "EA")

        rfq, provider = load_case(ROOT, "A")
        findings, _ = check_business(rfq, provider.evaluate(rfq))
        loc = next(f for f in findings if f["line_id"] == "L030"
                   and f["rule"] == "certificate")["locator"]
        self.assertEqual(loc["container"], "body-html")
        self.assertEqual(loc["raw_value"], "SPECIAL REQUIREMENT")
        self.assertIn("询价产品要求", loc["text"])

    def test_quote_deadline_before_evaluation_date_is_flagged(self):
        rfq = rfq_with(due_at="2026-09-01T10:00:00")
        row = PriceTableQuoteProvider([supplier("PR02", "EASA Form 1", 14)], []).evaluate(rfq)[0]
        findings, _ = check_business(rfq, [row])
        self.assertEqual(next(f for f in findings if f["rule"] == "quote_due")["status"], "warn")


if __name__ == "__main__":
    unittest.main()
