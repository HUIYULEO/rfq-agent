import unittest

from rfq_agent.domain.models import Line, RFQ
from rfq_agent.domain.pricing import PriceTableQuoteProvider


def rfq_for(part_number: str) -> RFQ:
    return RFQ(
        rfq_number="FAIL-CLOSED",
        evaluation_date="2026-11-02",
        source_files=["test.eml"],
        lines=[Line(line_id="1", part_number=part_number, quantity="1", unit="EA",
                    condition_accept=["NE"])],
    )


def supplier_row(part_number: str, **overrides) -> dict:
    row = {
        "_source_row": 2,
        "Part Number": part_number,
        "价格资源编号": "PRICE-1",
        "供应商编码": "SUP-1",
        "供应商名称": "Supplier",
        "价格状态": "有效",
        "最小起订量": 1,
        "件状条件": "NE",
        "币种": "CNY",
        "单价": 100,
    }
    row.update(overrides)
    return row


class PricingFailClosedTests(unittest.TestCase):
    def assert_blocked_by(self, result: dict, rule: str):
        self.assertEqual(result["eligibility"], "needs_review")
        rules = {item["rule"] for item in result["decision"]["review_evidence"]}
        self.assertIn(rule, rules)

    def test_missing_supplier_price_never_becomes_zero(self):
        provider = PriceTableQuoteProvider([supplier_row("PN-1", 单价=None)], [])
        self.assert_blocked_by(provider.evaluate(rfq_for("PN-1"))[0], "price_missing")

    def test_unknown_currency_never_uses_one_to_one_fx(self):
        provider = PriceTableQuoteProvider([supplier_row("PN-2", 币种="JPY")], [])
        self.assert_blocked_by(provider.evaluate(rfq_for("PN-2"))[0], "fx_missing")

    def test_missing_internal_standard_cost_is_not_quotable(self):
        stock = {
            "_source_row": 3,
            "Part Number": "PN-3",
            "库存数量": 5,
            "库存件状": "NE",
            "标准成本": None,
            "币种": "CNY",
        }
        provider = PriceTableQuoteProvider([], [stock])
        self.assert_blocked_by(provider.evaluate(rfq_for("PN-3"))[0], "cost_missing")


if __name__ == "__main__":
    unittest.main()
