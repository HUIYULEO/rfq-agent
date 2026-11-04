"""Quote-desk load stays small when the stored task is large.

Case A (and any wider upload) keeps every candidate on the line. The old
``/api/task`` handler parsed that whole file under the process lock and then
embedded every analysis row in one JSON body. Browsers reported
ERR_EMPTY_RESPONSE or sat on the request. The desk now reads ``desk.json``
(no candidate arrays) and pages lines, reviews and the report.
"""
import json
import os
import shutil
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from rfq_agent import analysis, web
from rfq_agent.models import Line, RFQ, Task
from rfq_agent.service import RFQService
from rfq_agent.storage import TaskStore

ROOT = Path(__file__).resolve().parents[1]
PAGE = Path(__file__).resolve().parents[1] / "rfq_agent" / "static" / "index.html"


def _test_root() -> Path:
    root = Path(os.environ.get("RFQ_TEST_ROOT") or ROOT / "runtime" / "tests")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _candidate(index: int) -> dict:
    return {
        "source_type": "supplier",
        "source_id": f"PR{index:06d}",
        "part_number": f"PN-{index:04d}",
        "match_type": "exact",
        "supplier_name": "广州白鹭航空技术服务有限公司",
        "certificate": "EASA Form 1",
        "condition": "FN",
        "unit": "EA",
        "unit_cost_cny": "13.86",
        "eligibility": "rejected",
        "remark": "含包装费" * 30,
        "rejection_reasons": [
            {"rule": "moq_exceeds", "detail": "MOQ 50 > 需求 2"},
            {"rule": "cert_mismatch", "detail": "候选证书不满足客户要求"},
        ],
        "source_row": index,
    }


def _evidence(number: int, part: str) -> dict:
    return {
        "file": "1/航空配件询价（RFQ-20260901）.eml",
        "attachment": "询价报价清单.xlsx",
        "container": "attachment-xlsx",
        "sheet": "Sheet1",
        "row": number + 1,
        "columns": {"Part Number": {"letter": "C", "header": "Part Number"}},
        "raw": {"Part Number": part, "Quantity": "2", "Unit Of Measure": "EA"},
    }


def _build(store: TaskStore, *, lines: int, candidates: int, open_at: int, blocked_at: int,
           disposition: str = "PENDING", evaluation: str = "incomplete") -> str:
    rfq_lines = []
    results = []
    reviews = []
    for number in range(1, lines + 1):
        part = f"PN-{number:04d}"
        evidence = _evidence(number, part)
        rfq_lines.append(Line(
            line_id=f"L{number:03d}", part_number=part, quantity="2", unit="EA",
            description="WASHER 垫片", condition_accept=["FN"], evidence=evidence))
        priced = number < open_at
        blocked = number >= blocked_at
        if priced:
            decision = {"recommended_source": "supplier", "basis": {"condition": "FN",
                        "certificate": "EASA Form 1"}, "review_evidence": [], "warnings": []}
            eligibility, price, total = "priced", "10.00", "20.00"
        else:
            detail = f"{part} 在两张参考表中均无记录" if blocked else f"{part} 证书不满足，待人工决定"
            decision = {"recommended_source": "manual", "basis": None,
                        "review_evidence": [{"rule": "no_price_record" if blocked else "cert_mismatch",
                                             "detail": detail}],
                        "warnings": []}
            eligibility, price, total = "needs_review", None, None
        results.append({
            "line_id": f"L{number:03d}", "line_number": number, "part_number": part,
            "description": "WASHER 垫片", "quantity": "2", "unit": "EA",
            "eligibility": eligibility, "unit_price": price, "line_total": total,
            "priced_quantity": "2" if priced else None,
            "decision": decision, "evidence": evidence,
            "internal_candidates": [_candidate(number * 1000 + i) for i in range(candidates)],
            "supplier_candidates": [_candidate(number * 2000 + i) for i in range(candidates)],
        })
        if open_at <= number < blocked_at:
            reviews.append({
                "id": f"RI-{number:04d}", "scope": "line", "status": "open", "severity": "blocking",
                "target": {"type": "line", "line_id": f"L{number:03d}", "line_number": number,
                           "part_number": part},
                "kinds": ["cert_mismatch"],
                "question": f"{part} 是否接受当前证书？",
                "options": [{"id": "skip_line", "label": "不报此行"},
                            {"id": "accept_noncert", "label": "接受非要求证书件并报价"}],
                "evidence": decision["review_evidence"],
                "answer": None,
            })
    if reviews:
        reviews.append({
            "id": "RI-0001-DONE", "scope": "line", "status": "answered", "severity": "blocking",
            "target": {"type": "line", "line_id": "L001", "part_number": "PN-0001"},
            "kinds": ["uom_mismatch"], "question": "单位已确认？",
            "options": [{"id": "confirm_uom", "label": "已与客户确认单位一致"}],
            "evidence": [], "answer": "confirm_uom",
        })
    task_id = uuid4().hex
    task = Task(task_id, RFQ(
        rfq_number="RFQ-A", evaluation_date="2026-09-01", lines=rfq_lines,
        source_files=["1/航空配件询价（RFQ-20260901）.eml"], customer="客户A", subject="大单"),
        status="awaiting_review", results=results, review_items=reviews,
        commercial_disposition=disposition, evaluation_status=evaluation)
    store.save(task)
    return task_id


class AnalysisPageTests(unittest.TestCase):
    def test_pages_match_full_report_and_stay_linear(self):
        lines = []
        results = []
        checks = []
        for number in range(1, 6):
            lines.append({"line_id": f"L{number:03d}", "part_number": f"PN-{number}",
                          "quantity": "1", "unit": "EA", "evidence": _evidence(number, f"PN-{number}")})
            results.append({
                "line_id": f"L{number:03d}", "line_number": number, "part_number": f"PN-{number}",
                "description": "", "quantity": "1", "unit": "EA", "eligibility": "priced",
                "unit_price": "1.00", "line_total": "1.00",
                "decision": {"recommended_source": "supplier", "basis": {}, "review_evidence": [],
                             "flags": []},
            })
            for rule in ("pn_match", "certificate", "uom", "condition", "quote_due"):
                checks.append({"line_id": f"L{number:03d}", "part_number": f"PN-{number}",
                               "rule": rule, "status": "pass", "detail": "ok"})
        task = {"task_id": "a" * 32, "revision": 1, "rfq": {"rfq_number": "R", "customer": "A",
                "subject": "", "evaluation_date": "2026-09-01", "source_files": ["a.eml"],
                "lines": lines}, "results": results, "review_items": [], "resolutions": {},
                "business_checks": checks}
        full = analysis.build(task)
        paged = []
        offset = 0
        while offset < len(full["lines"]):
            chunk = analysis.page(task, offset, 2)
            self.assertEqual(chunk["summary"], full["summary"])
            paged.extend(chunk["lines"])
            offset += 2
        self.assertEqual(paged, full["lines"])

        wide = []
        wide_results = []
        wide_checks = []
        for number in range(1, 1501):
            wide.append({"line_id": f"L{number:04d}", "part_number": f"PN-{number}",
                         "quantity": "1", "unit": "EA", "evidence": {}})
            wide_results.append({
                "line_id": f"L{number:04d}", "line_number": number, "part_number": f"PN-{number}",
                "description": "", "quantity": "1", "unit": "EA", "eligibility": "needs_review",
                "unit_price": None, "line_total": None,
                "decision": {"recommended_source": "manual", "basis": None,
                             "review_evidence": [{"rule": "no_price_record", "detail": "无记录"}],
                             "flags": []},
            })
            for rule in ("pn_match", "certificate", "uom", "condition", "quote_due"):
                wide_checks.append({"line_id": f"L{number:04d}", "rule": rule, "status": "warn",
                                    "detail": "需要看一眼"})
        wide_task = {"task_id": "b" * 32, "revision": 1,
                     "rfq": {"rfq_number": "R", "customer": "", "subject": "",
                             "evaluation_date": "2026-09-01", "source_files": [], "lines": wide},
                     "results": wide_results, "review_items": [], "resolutions": {},
                     "business_checks": wide_checks}
        started = time.perf_counter()
        window = analysis.page(wide_task, 0, 20)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(window["lines"]), 20)
        self.assertEqual(window["summary"]["lines"], 1500)
        self.assertLess(elapsed, 0.4, f"analysis page scanned too slowly ({elapsed:.3f}s)")


class DeskLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _test_root() / f"desk{uuid4().hex[:8]}"
        self.tmp.mkdir()
        self.store = TaskStore(self.tmp)
        self.service = RFQService(self.store)
        self._previous = web.SERVICE
        web.SERVICE = self.service
        self.task_id = _build(self.store, lines=80, candidates=40, open_at=70, blocked_at=76)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        web.SERVICE = self._previous
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_task_load_ignores_fat_task_json(self):
        stored = self.tmp / self.task_id / "task.json"
        self.assertGreater(stored.stat().st_size, 1_000_000)
        desk = json.loads((self.tmp / self.task_id / "desk.json").read_text(encoding="utf-8"))
        self.assertNotIn("internal_candidates", json.dumps(desk["results"][0]))
        stored.write_text("this is not the payload the desk should parse", encoding="utf-8")

        started = time.perf_counter()
        status, body = self._get(f"/api/task?id={self.task_id}")
        elapsed = time.perf_counter() - started
        self.assertEqual(status, 200, body)
        self.assertLess(len(body), 150_000, len(body))
        self.assertLess(elapsed, 2.0, elapsed)
        payload = json.loads(body)
        self.assertNotIn("results", payload)
        self.assertNotIn("internal_candidates", body.decode())
        self.assertEqual(payload["dock_step"], 4)
        self.assertEqual(payload["open_reviews"], 6)
        self.assertEqual(payload["inquiry"]["grounded"], 69)
        self.assertEqual(payload["inquiry"]["pending"], 6)
        self.assertEqual(payload["inquiry"]["blocked"], 5)
        self.assertEqual(payload["summary"]["priced_subtotal_cny"], "1380.00")

        status, body = self._get(f"/api/lines?id={self.task_id}&offset=0&limit=500")
        self.assertEqual(status, 200)
        page = json.loads(body)
        self.assertEqual(page["limit"], 80)
        self.assertEqual(len(page["lines"]), 80)
        first = page["lines"][0]
        self.assertEqual(first["outcome"], "有依据")
        self.assertEqual(first["citation"]["email"], "1/航空配件询价（RFQ-20260901）.eml")
        self.assertEqual(first["citation"]["attachment"], "询价报价清单.xlsx")
        self.assertEqual(first["citation"]["sheet"], "Sheet1")
        self.assertEqual(first["citation"]["column"], "C")
        self.assertEqual(first["citation"]["raw"], "PN-0001")
        pending = page["lines"][69]
        self.assertEqual(pending["outcome"], "待预审")
        blocked = page["lines"][75]
        self.assertIn("无记录", blocked["outcome"])
        self.assertNotEqual(blocked["outcome"], "待预审")

        status, body = self._get(f"/api/reviews?id={self.task_id}&bucket=open&offset=0&limit=2")
        self.assertEqual(status, 200)
        opened = json.loads(body)
        self.assertEqual(opened["total"], 6)
        self.assertEqual(len(opened["items"]), 2)
        status, body = self._get(f"/api/reviews?id={self.task_id}&bucket=passed&offset=0&limit=20")
        self.assertEqual(json.loads(body)["total"], 1)

        status, body = self._get(f"/api/report?id={self.task_id}&offset=40&limit=10")
        report = json.loads(body)
        self.assertEqual(report["total"], 80)
        self.assertEqual(len(report["lines"]), 10)
        self.assertEqual(report["lines"][0]["line_number"], 41)

        status, body = self._get("/api/task?id=not-a-task")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def test_uncaught_handler_errors_are_json(self):
        original = web._view
        def explode(_task):
            raise RuntimeError("boom")
        web._view = explode
        try:
            status, body = self._get(f"/api/task?id={self.task_id}")
        finally:
            web._view = original
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "boom")

    def test_quote_and_no_bid_dock_on_report_or_draft(self):
        quote_id = _build(self.store, lines=2, candidates=0, open_at=9, blocked_at=9,
                          disposition="QUOTE", evaluation="complete")
        quote = web._view(self.store.get_desk(quote_id))
        self.assertEqual(quote["dock_step"], 6)
        self.assertTrue(quote["quote_allowed"])
        inquiry_id = _build(self.store, lines=2, candidates=0, open_at=9, blocked_at=1,
                            disposition="SUPPLIER_INQUIRY", evaluation="complete")
        inquiry = web._view(self.store.get_desk(inquiry_id))
        self.assertEqual(inquiry["dock_step"], 5)
        self.assertFalse(inquiry["quote_allowed"])


class DeskCopyTests(unittest.TestCase):
    def test_extraction_page_keeps_raw_values_and_marks_all_duplicate_rows(self):
        lines = [{"line_id": f"L{number:03d}", "inquiry_no": "9001", "part_number": "PN-0001",
                  "quantity": "2", "unit": "EA", "priority": "" if number == 70 else "HIGH",
                  "condition_accept": ["NE"], "due_at": "2026-09-10",
                  "evidence": {"raw": {"Part Number": " PN-0001 ", "Quantity": "02"}}}
                 for number in range(1, 71)]
        task = {"rfq": {"lines": lines, "evaluation_date": "2026-09-01"}}
        page = web._extraction_page(task, 69, 1)
        self.assertEqual(page["total"], 70)
        self.assertEqual(page["lines"][0]["raw"]["Quantity"], "02")
        self.assertEqual({item["title"] for item in page["lines"][0]["issues"]},
                         {"同询价号重复料号"})

    def test_page_carries_the_six_steps_and_the_confirm(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertIn(
            'const STEPS = ["输入采集", "结构提取", "询价分析", "人工预审", "分析报告", "报价草案"]',
            html)
        self.assertIn("打开已有任务", html)
        self.assertIn("作为新任务运行", html)
        self.assertIn('rerun.className = "btn quiet"', html)
        self.assertIn("请先完成第 4 步", html)
        self.assertNotIn("SHOW_ALL_REVIEWS", html)
        self.assertNotIn("slice(0, 3)", html)
        self.assertNotIn('id="start"', html)


if __name__ == "__main__":
    unittest.main()
