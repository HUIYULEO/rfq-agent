"""Golden test: the deterministic core must reproduce tests/fixtures/expected/buyer_b.json.

The fixture was produced independently (tools/build_expected_buyer_b.py) from the same
written policy, so this is a differential check, not the engine certifying itself:
any change in rounding order, tie-breaking or rule precedence shows up as a diff.
"""
import json
import os
import shutil
import unittest
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from rfq_agent import deps
from rfq_agent.cases import load_case, template_for
from rfq_agent.pricing import summarize
from rfq_agent.service import RFQService
from rfq_agent.storage import TaskStore

ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "expected" / "buyer_b.json"


def _test_root() -> Path:
    root = Path(os.environ.get("RFQ_TEST_ROOT") or ROOT / "runtime" / "tests")
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_temp_dir(prefix: str) -> Path:
    """mkdtemp() drops ACL inheritance and the sandbox then denies child mkdir."""
    path = _test_root() / f"{prefix}{uuid4().hex[:12]}"
    path.mkdir()
    return path


@unittest.skipUnless(GOLDEN.exists(), "golden fixture missing")
class GoldenBuyerBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = make_temp_dir("g")
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        cls.rfq, cls.provider = load_case(ROOT, "B")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def _service(self) -> RFQService:
        service = RFQService(TaskStore(Path(self.root)), provider=self.provider,
                             template=template_for(ROOT))
        task = service.create_task(self.rfq.to_dict())
        service.analyze_rfq(task["task_id"])
        return service, task["task_id"]

    # ---------- pricing ----------

    def test_summary_matches_golden(self):
        got = summarize(self.provider.evaluate(self.rfq))
        want = self.golden["expected_summary"]
        self.assertEqual(got["lines"], want["lines"])
        self.assertEqual(got["priced_lines"], want["priced_lines"])
        self.assertEqual(got["review_lines"], want["review_lines"])
        self.assertEqual(got["internal_lines"], want["source_breakdown"]["internal"])
        self.assertEqual(got["supplier_lines"], want["source_breakdown"]["supplier"])
        self.assertEqual(got["priced_subtotal_cny"], want["priced_subtotal_cny"])

    def test_every_line_matches_golden(self):
        results = self.provider.evaluate(self.rfq)
        self.assertEqual(len(results), len(self.golden["rfq"]["items"]))
        for got, want in zip(results, self.golden["rfq"]["items"]):
            with self.subTest(line=want["line_number"], part=want["part_number"]):
                self.assertEqual(got["part_number"], want["part_number"])
                self.assertEqual(Decimal(got["quantity"]), Decimal(want["quantity"]))
                a, b = got["decision"], want["decision"]
                for key in ("recommended_source", "selected_source_id", "unit_cost_cny",
                            "recommended_unit_price", "line_total", "requires_human_review"):
                    self.assertEqual(a.get(key), b.get(key), f"field {key}")

    def test_subtotal_equals_sum_of_lines(self):
        results = self.provider.evaluate(self.rfq)
        total = sum((Decimal(row["line_total"]) for row in results
                     if row["line_total"] is not None), Decimal("0"))
        self.assertEqual(f"{total:.2f}", self.golden["expected_summary"]["priced_subtotal_cny"])

    # ---------- gate ----------

    def test_gate_blocks_then_resumes(self):
        service, task_id = self._service()
        state = service.build_quote_options(task_id, 1)
        self.assertEqual(state["status"], "awaiting_review")
        ids = [item["id"] for item in state["review_items"]]
        self.assertEqual(ids, ["RI-0016", "RI-0019"])

        state = service.resolve_review(task_id, state["revision"], "RI-0016", "quote_with_moq")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["status"], "awaiting_review")
        self.assertEqual([item["id"] for item in state["review_items"]], ["RI-0019"])
        self.assertEqual(summarize(state["results"])["priced_lines"], 20)
        # the answered line is priced at the supplier MOQ, not at the requested quantity
        line16 = next(row for row in state["results"] if row["line_number"] == 16)
        self.assertEqual(line16["quantity_basis"], "moq")
        self.assertEqual(Decimal(line16["priced_quantity"]), Decimal("50"))
        self.assertEqual([entry["actor"] for entry in state["decision_log"]], ["human"])

        state = service.resolve_review(task_id, state["revision"], "RI-0019", "request_quotation")
        self.assertEqual(state["status"], "draft_ready")
        self.assertEqual(summarize(state["results"])["priced_lines"], 20)
        self.assertEqual([entry["option"] for entry in state["decision_log"]],
                         ["quote_with_moq", "request_quotation"])

    def test_auto_draft_completes_without_a_human(self):
        service, task_id = self._service()
        state = service.build_quote_options(task_id, 1, autonomy="auto_draft")
        self.assertEqual(state["status"], "draft_ready")
        self.assertTrue(all(item["status"] == "answered" for item in state["review_items"]))
        self.assertEqual({entry["actor"] for entry in state["decision_log"]}, {"system:auto_draft"})
        # defaults never fabricate a price for the blocked lines
        self.assertEqual(summarize(state["results"])["priced_lines"], 19)

    def test_cannot_answer_twice_or_invent_an_option(self):
        service, task_id = self._service()
        state = service.build_quote_options(task_id, 1)
        with self.assertRaises(ValueError):
            service.resolve_review(task_id, 1, "RI-0016", "give_it_away")
        with self.assertRaises(ValueError):
            service.resolve_review(task_id, 1, "RI-9999", "request_quotation")
        state = service.resolve_review(task_id, 1, "RI-0016", "request_quotation")
        with self.assertRaises(ValueError):
            service.resolve_review(task_id, state["revision"], "RI-0016", "request_quotation")

    def test_stale_revision_is_rejected(self):
        service, task_id = self._service()
        state = service.build_quote_options(task_id, 1)
        service.resolve_review(task_id, 1, "RI-0016", "request_quotation")
        with self.assertRaises(ValueError):
            service.resolve_review(task_id, 1, "RI-0019", "request_quotation")

    # ---------- export ----------

    @unittest.skipUnless(deps.DEPS.is_dir(), "vendored .deps not present")
    def test_docx_renders_all_lines_and_totals(self):
        import docx

        service, task_id = self._service()
        state = service.build_quote_options(task_id, 1)
        preview = service.export_quote_draft(task_id, state["revision"])
        # Open reviews: analysis record + analysis report only, no quotation document.
        self.assertEqual([Path(a).name for a in preview["artifacts"]],
                         [f"draft-r{state['revision']}.json", f"analysis-r{state['revision']}.docx"])
        state = service.resolve_review(task_id, state["revision"], "RI-0016", "request_quotation")
        state = service.resolve_review(task_id, state["revision"], "RI-0019", "request_quotation")
        exported = service.export_quote_draft(task_id, state["revision"])
        path = next(Path(a) for a in exported["artifacts"] if Path(a).name.startswith("quote-draft"))
        self.assertTrue(path.exists())
        document = docx.Document(str(path))
        table = document.tables[0]
        self.assertEqual(len(table.rows), 1 + len(state["results"]) + 3)
        self.assertEqual(table.rows[1].cells[3].text.strip(), "PN-0001")
        totals = table.rows[len(state["results"]) + 1]
        self.assertIn("386,595.80", totals.cells[7].text)
        self.assertIn("叁拾捌万陆仟伍佰玖拾伍元捌角整", totals.cells[2].text)
        self.assertEqual(len(document.tables[1].rows), 1 + len(state["results"]))


class RmbUpperTests(unittest.TestCase):
    def test_capital_amounts(self):
        from rfq_agent.export_docx import rmb_upper

        cases = {
            "386595.80": "叁拾捌万陆仟伍佰玖拾伍元捌角整",
            "1000": "壹仟元整",
            "10001": "壹万零壹元整",
            "20.05": "贰拾元零伍分",
            "0.50": "伍角整",
            "0": "零元整",
        }
        for amount, expected in cases.items():
            with self.subTest(amount=amount):
                self.assertEqual(rmb_upper(amount), expected)


if __name__ == "__main__":
    unittest.main()
