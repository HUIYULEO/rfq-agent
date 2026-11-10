"""Demo server: stdlib only, so it runs on a judge machine or a public host.

    python -m rfq_agent.web --port 8765                  # local, loopback only
    RFQ_HOST=0.0.0.0 PORT=8000 python -m rfq_agent.web   # container / PaaS

The agent's input is the material itself: any of the six official .eml files, or one
the judge uploads. The page only shows what the pipeline produced — every number comes

from rfq_agent.domain.pricing, every field from rfq_agent.ingestion.ingest.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import mimetypes
import os
import re
import signal
import threading
from collections import Counter
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4
from rfq_agent.reporting import analysis as analysis_report
from rfq_agent.llm import perception
from rfq_agent.domain import quality
from rfq_agent.domain import terms
from rfq_agent.ingestion.ingest import read_eml
from rfq_agent.application.cases import (admit_case, available, case_by_key, confirm_pending, data_status,
                    discover, inbox, load_case, mark_stage, provider_for, reset_inbox,
                    stage_pending, sweep_pending, template_for)
from rfq_agent.llm.genesis import GenesisError, Session, load_config, missing_keys
from rfq_agent.llm.perception import PerceptionError
from rfq_agent.infrastructure.ledger import OperationalLedger
from rfq_agent.domain.policy import active_policy
from rfq_agent.domain.pricing import summarize
from rfq_agent.application.service import RFQService
from rfq_agent.infrastructure.storage import TaskStore

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
FILE_PATTERN = re.compile(r"^(draft|quote-draft|analysis|quote)-r\d+\.(json|docx)$")

LOCK = threading.Lock()
SERVICE: RFQService | None = None
SESSIONS: dict[str, Session] = {}


def _service() -> RFQService:
    global SERVICE
    if SERVICE is None:
        policy = active_policy()
        try:
            provider = provider_for(ROOT)
        except (OSError, ValueError):
            # Missing workbooks must not drop the socket (ERR_EMPTY_RESPONSE).
            # Pricing endpoints still fail closed inside the service.
            provider = None
        SERVICE = RFQService(TaskStore(ROOT / "runtime"), provider=provider,
                             policy=policy, template=template_for(ROOT),
                             ledger=OperationalLedger(ROOT / "runtime" / "operations.db"))
    return SERVICE


def _understand(case: str):
    """Main path: the model reads the mail, code audits it, pricing still does the sums.

    Falls back to the deterministic parser when the model is unconfigured, unreachable
    or refuses the extraction — a dead model must never dead-end the demo. Which path
    actually ran is written to the task trace, so "the agent read this" is checkable
    rather than asserted.
    """
    path = case_by_key(ROOT, case).path
    config = load_config(ROOT)
    if missing_keys(config):
        rfq, _ = load_case(ROOT, case)
        return rfq, {"source": "deterministic", "model": "",
                     "reason": "模型未配置，使用确定性解析"}
    try:
        return perception.run(config, path)
    except (PerceptionError, GenesisError) as exc:
        rfq, _ = load_case(ROOT, case)
        return rfq, {"source": "deterministic", "model": config.get("GENESIS_MODEL", ""),
                     "reason": f"模型理解失败，回退确定性解析：{exc}"}


def _lines_view(rfq: dict) -> list[dict]:
    lines = []
    for line in rfq.get("lines", []):
        evidence = line.get("evidence") or {}
        number = str(line.get("line_id", "")).lstrip("L")
        lines.append({
            "line_number": int(number) if number.isdigit() else None,
            "part_number": line.get("part_number", ""),
            "description": line.get("description", ""),
            "quantity": line.get("quantity"),
            "unit": line.get("unit"),
            "condition_accept": line.get("condition_accept") or [],
            "priority": line.get("priority") or "",
            "due_at": line.get("due_at") or "",
            "provenance": f"{evidence.get('container', '')} · 第 {evidence.get('row', '?')} 行",
            "located": bool(evidence.get("row")),
        })
    return lines


def _preview_view(rfq) -> dict:
    """What an upload will become, shown before the user commits to it."""
    data = rfq.to_dict() if hasattr(rfq, "to_dict") else rfq
    lines = _lines_view(data)
    container = ((data.get("lines") or [{}])[0].get("evidence") or {}).get("container", "")
    findings = quality.inspect(data)
    return {
        "customer": data.get("customer", ""),
        "subject": data.get("subject", ""),
        "rfq_number": data.get("rfq_number", ""),
        "evaluation_date": data.get("evaluation_date", ""),
        "urgency": data.get("urgency", ""),
        "container": container,
        "source_files": data.get("source_files", []),
        "lines": len(lines),
        "fields": _field_health(lines),
        "quality": findings,
        "quality_summary": quality.summary(findings),
        "requirements": data.get("requirements", []),
        "sample": [{"line_number": line["line_number"], "part_number": line["part_number"],
                    "description": line["description"], "quantity": line["quantity"],
                    "unit": line["unit"]}
                   for line in lines[:5]],
    }


PAGE_DEFAULT = 40
PAGE_MAX = 80
OUTCOME_LABEL = {"grounded": "有依据", "pending": "待预审"}


def _page_window(query: dict) -> tuple[int, int]:
    def one(name: str, default: int) -> int:
        raw = (query.get(name) or [str(default)])[0]
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    offset = max(0, one("offset", 0))
    limit = one("limit", PAGE_DEFAULT)
    if limit < 1:
        limit = PAGE_DEFAULT
    return offset, min(limit, PAGE_MAX)


def _open_review_index(task: dict) -> tuple[set, bool, int, int]:
    open_lines: set[str] = set()
    task_open = False
    open_count = answered = 0
    for item in task.get("review_items") or []:
        if item.get("status") == "open":
            open_count += 1
            if item.get("scope") == "task":
                task_open = True
            line_id = (item.get("target") or {}).get("line_id")
            if line_id:
                open_lines.add(line_id)
        elif item.get("status") == "answered":
            answered += 1
    answered = len({i.get("item_id") for i in task.get("decision_log", []) if i.get("item_id")} |
                   {i["id"] for i in task.get("review_items", []) if i.get("status") == "answered"})
    return open_lines, task_open, open_count, answered


def _reason_text(row: dict) -> str:
    bits = []
    for item in (row.get("decision") or {}).get("review_evidence") or []:
        if isinstance(item, dict) and item.get("detail"):
            bits.append(str(item["detail"]))
    return "；".join(bits)


def _outcome(row: dict, open_lines: set, task_open: bool) -> tuple[str, str]:
    """Step 3 is locate-only: 有依据 / 待预审 / the block reason. It does not decide."""
    reason = _reason_text(row)
    if row.get("line_id") in open_lines or (task_open and row.get("eligibility") != "priced"):
        return "pending", reason
    if row.get("eligibility") == "priced":
        return "grounded", reason
    return "blocked", reason or "无可用依据"


def _inquiry_summary(task: dict) -> dict:
    results = task.get("results") or []
    open_lines, task_open, open_count, answered = _open_review_index(task)
    counts = {"grounded": 0, "pending": 0, "blocked": 0}
    for row in results:
        kind, _reason = _outcome(row, open_lines, task_open)
        counts[kind] += 1
    money = summarize(results) if results else {
        "lines": 0, "priced_lines": 0, "review_lines": 0, "internal_lines": 0,
        "supplier_lines": 0, "priced_subtotal_cny": "0.00"}
    return {**money, **counts, "open_reviews": open_count, "answered_reviews": answered}


def _citation(source) -> dict:
    from rfq_agent.domain.business_rules import locate

    located = locate(source, "Part Number") if source else {}
    raw = located.get("raw_value")
    return {
        "email": located.get("file") or "",
        "attachment": located.get("attachment") or "",
        "sheet": located.get("sheet") or "",
        "row": located.get("row"),
        "column": located.get("column_letter") or "",
        "column_header": located.get("column_header") or "",
        "raw": "" if raw is None else str(raw),
        "text": located.get("text") or "",
    }


def _line_page(task: dict, offset: int, limit: int) -> dict:
    results = task.get("results") or []
    lines_by_id = {line["line_id"]: line for line in (task.get("rfq") or {}).get("lines") or []}
    open_lines, task_open, _open_count, _answered = _open_review_index(task)
    rows = []
    for row in results[offset:offset + limit]:
        kind, reason = _outcome(row, open_lines, task_open)
        decision = row.get("decision") or {}
        basis = decision.get("basis") or {}
        rows.append({
            "line_number": row.get("line_number"),
            "line_id": row.get("line_id"),
            "part_number": row.get("part_number") or "",
            "description": row.get("description") or "",
            "quantity": row.get("priced_quantity") or row.get("quantity"),
            "unit": row.get("unit"),
            "kind": kind,
            "outcome": reason if kind == "blocked" else OUTCOME_LABEL[kind],
            "reason": reason,
            "unit_price": row.get("unit_price"),
            "line_total": row.get("line_total"),
            "source": decision.get("recommended_source"),
            "price_basis": {
                "source_id": decision.get("selected_source_id"),
                "cost_basis_type": decision.get("cost_basis_type"),
                "unit_cost_original": decision.get("unit_cost_original"),
                "currency": decision.get("currency"),
                "exchange_rate": decision.get("exchange_rate"),
                "unit_cost_cny": decision.get("unit_cost_cny"),
                "markup_rate": decision.get("markup_rate"),
                "details": {key: basis.get(key) for key in (
                    "supplier_name", "warehouse", "condition", "certificate", "available_quantity",
                    "lead_time_days", "moq", "ships_from", "valid_until", "unit", "match_type")},
                "reasons": decision.get("decision_reasons") or [],
                "warnings": decision.get("warnings") or [],
            },
            "condition": basis.get("condition"),
            "certificate": basis.get("certificate"),
            "citation": _citation(lines_by_id.get(row.get("line_id")) or row),
        })
    return {"total": len(results), "offset": offset, "limit": limit,
            "summary": _inquiry_summary(task), "lines": rows}


def _extraction_page(task: dict, offset: int, limit: int) -> dict:
    rfq = task.get("rfq") or {}
    lines = rfq.get("lines") or []
    findings = quality.inspect(rfq)
    by_line: dict[str, list[dict]] = {}
    unlocated = []
    part_inquiries: dict[str, set[str]] = {}
    groups: dict[tuple[str, str], list[dict]] = {}
    for line in lines:
        inquiry = str(line.get("inquiry_no") or "").strip()
        part = line.get("part_number") or ""
        part_inquiries.setdefault(part, set()).add(inquiry)
        groups.setdefault((inquiry, part), []).append(line)
    units = Counter(line.get("unit") for line in lines)
    common_unit = units.most_common(1)[0][0] if units else None
    evaluated = rfq.get("evaluation_date") or ""
    for finding in findings:
        ids = finding.get("lines") or []
        kind = finding.get("kind")
        if kind == "duplicate_part":
            ids = [line["line_id"] for line in lines if len(groups[(str(line.get("inquiry_no") or "").strip(), line.get("part_number") or "")]) > 1]
        elif kind == "repeated_part_across_inquiries":
            ids = [line["line_id"] for line in lines if len(part_inquiries[line.get("part_number") or ""] - {""}) > 1]
        elif kind == "unit_mix":
            ids = [line["line_id"] for line in lines if line.get("unit") != common_unit]
        elif kind == "unit_conflict":
            ids = [line["line_id"] for line in lines if len({item.get("unit") for item in groups[(str(line.get("inquiry_no") or "").strip(), line.get("part_number") or "")]}) > 1]
        elif kind in ("missing_due_at", "missing_condition"):
            field = {"missing_due_at": "due_at", "missing_condition": "condition_accept"}[kind]
            ids = [line["line_id"] for line in lines if not line.get(field)]
        elif kind == "date_anomaly":
            ids = []
            for line in lines:
                due = (line.get("due_at") or "")[:10]
                if not due or not evaluated:
                    continue
                try:
                    anomalous = date.fromisoformat(due) < date.fromisoformat(evaluated)
                except ValueError:
                    anomalous = True
                if anomalous:
                    ids.append(line["line_id"])
        if not ids:
            unlocated.append({"title": finding["title"], "detail": finding["detail"],
                              "severity": finding["severity"]})
        for line_id in ids:
            by_line.setdefault(line_id, []).append({
                "title": finding["title"], "detail": finding["detail"],
                "severity": finding["severity"]})
    for issue in (task.get("extraction") or {}).get("issues") or []:
        if issue.get("kind") == "reconciled":
            continue
        item = {"title": "模型识别疑点", "detail": str(issue.get("detail") or ""),
                "severity": "warn"}
        index = issue.get("line")
        if isinstance(index, int) and 1 <= index <= len(lines):
            by_line.setdefault(lines[index - 1]["line_id"], []).append(item)
        else:
            unlocated.append(item)
    rows = []
    for line in lines[offset:offset + limit]:
        evidence = line.get("evidence") or {}
        rows.append({
            "line_id": line["line_id"],
            "line_number": int(str(line["line_id"]).lstrip("L")) if str(line["line_id"]).lstrip("L").isdigit() else None,
            "part_number": line.get("part_number") or "",
            "inquiry_no": line.get("inquiry_no") or "",
            "description": line.get("description") or "",
            "quantity": line.get("quantity"), "unit": line.get("unit") or "",
            "condition": line.get("condition_accept") or [],
            "priority": line.get("priority") or "",
            "due_at": line.get("due_at") or "",
            "raw": evidence.get("raw") or {},
            "citation": _citation(line),
            "issues": by_line.get(line["line_id"], []),
        })
    return {"total": len(lines), "offset": offset, "limit": limit,
            "lines": rows, "unlocated_issues": unlocated}


def _review_page(task: dict, bucket: str, offset: int, limit: int) -> dict:
    items = {i["id"]: dict(i) for i in task.get("review_items") or []}
    for log in task.get("decision_log", []):
        item_id = log.get("item_id")
        snapshot = log.get("item_snapshot") or items.get(item_id)
        if not snapshot:
            gate = next((g for g in task.get("interruptions", []) if g.get("review_item_id") == item_id), None)
            snapshot = {"id": item_id, "target": {}, "kinds": [], **(gate or {} )}
            snapshot["id"] = item_id
        item = items.setdefault(item_id, dict(snapshot))
        item.update(status="answered", answer=log.get("option"), options=snapshot.get("options", []))
        item.setdefault("history", []).append(log)
        item["can_amend"] = bool(snapshot.get("target")) and not task.get("approval") and task.get("status") != "active"
    items = list(items.values())
    passed = bucket == "passed"
    chosen = items if bucket == "all" else [item for item in items if (item.get("status") != "open") == passed]
    chosen.sort(key=lambda i: ((i.get("target") or {}).get("line_number") or 0, i.get("id") or ""))
    return {"bucket": "passed" if passed else "open", "total": len(chosen),
            "offset": offset, "limit": limit, "items": chosen[offset:offset + limit]}


def _report_page(task: dict, offset: int, limit: int) -> dict:
    data = analysis_report.page(task, offset, limit)
    lines = []
    for row in data["lines"]:
        locator = row.get("locator") or {}
        reason = "；".join(item.get("detail") for item in row.get("reasons") or []
                           if isinstance(item, dict) and item.get("detail"))
        lines.append({
            "line_number": row.get("line_number"),
            "part_number": row.get("part_number") or "",
            "quantity": row.get("quantity"),
            "unit": row.get("unit"),
            "status": row.get("status"),
            "status_label": row.get("status_label"),
            "reason": reason or "—",
            "suggested_action": row.get("suggested_action") or "",
            "locator": locator.get("text") or "",
        })
    return {"total": data["total"], "offset": data["offset"], "limit": limit,
            "summary": data["summary"], "lines": lines,
            "evaluation_status": data.get("evaluation_status"),
            "commercial_disposition": data.get("commercial_disposition"),
            "title": data.get("title")}


def _view(task: dict) -> dict:
    """Desk summary only. Line grids, reviews and the report are paged separately.

    Candidate arrays are not read here: callers pass ``TaskStore.get_desk``.
    """
    rfq = task.get("rfq") or {}
    results = task.get("results") or []
    lines = _lines_view(rfq)
    inquiry = _inquiry_summary(task)
    container = ""
    if results:
        container = (results[0].get("evidence") or {}).get("container") or ""
    if not container:
        container = ((rfq.get("lines") or [{}])[0].get("evidence") or {}).get("container", "")
    artifacts = [Path(name).name for name in task.get("artifacts") or []]
    stage = ("draft" if artifacts else "priced" if results
             else "parsed" if task.get("status") == "analyzed" else "received")
    quality_findings = quality.inspect(rfq)
    term_items = terms.check(rfq, results)
    quote_allowed = (inquiry["open_reviews"] == 0
                     and task.get("evaluation_status") == "complete"
                     and task.get("commercial_disposition") in {"QUOTE", "PARTIAL_QUOTE"})
    policy = _service().policy
    return {
        "task_id": task["task_id"],
        "revision": task["revision"],
        "status": task["status"],
        "process_status": task.get("process_status", task["status"]),
        "commercial_disposition": task.get("commercial_disposition", "PENDING"),
        "evaluation_status": task.get("evaluation_status", "pending"),
        "evaluation_issues": task.get("evaluation_issues", []),
        "autonomy": task.get("autonomy"),
        "policy_version": task.get("policy_version"),
        "data_snapshot_id": task.get("data_snapshot_id"),
        "customer": rfq.get("customer", ""),
        "rfq_number": rfq.get("rfq_number", ""),
        "subject": rfq.get("subject", ""),
        "evaluation_date": rfq.get("evaluation_date", ""),
        "source_files": rfq.get("source_files", []),
        "container": container,
        "summary": {key: inquiry[key] for key in (
            "lines", "priced_lines", "review_lines", "internal_lines", "supplier_lines",
            "priced_subtotal_cny")},
        "inquiry": inquiry,
        "stage": stage,
        "open_reviews": inquiry["open_reviews"],
        "answered_reviews": inquiry["answered_reviews"],
        "dock_step": 4 if inquiry["open_reviews"] else (6 if quote_allowed else 5),
        "quote_allowed": quote_allowed,
        "agent_trace": (task.get("agent_trace") or [])[-30:],
        "extraction": task.get("extraction") or {},
        "artifacts": artifacts,
        "fields": _field_health(lines),
        "quality": quality_findings,
        "quality_summary": quality.summary(quality_findings),
        "requirements": rfq.get("requirements", []),
        "terms": term_items,
        "terms_summary": terms.summary(term_items),
        "data": _data_trace(),
        "approval": task.get("approval") or {},
        "approval_enabled": bool(policy.get("commercial_approval_enabled")),
        "approval_mode": policy.get("approval_mode") or "none",
    }


def _load_desk(task_id: str) -> dict:
    if not task_id:
        raise ValueError("Task not found")
    return _service().store.get_desk(task_id)


def _field_health(lines: list[dict]) -> dict:
    """What the parse actually captured — completeness is a product fact, not a guess."""
    total = len(lines)
    filled = lambda key: sum(1 for row in lines if row.get(key) not in (None, "", []))
    return {"lines": total, "part_number": filled("part_number"), "quantity": filled("quantity"),
            "unit": filled("unit"), "condition": filled("condition_accept"),
            "with_evidence": sum(1 for row in lines if row.get("located"))}


def _data_trace() -> dict:
    """Which snapshot answered this request, and how much of it was touched."""
    provider = getattr(_service(), "provider", None)
    snapshot = getattr(provider, "snapshot_id", "") or ""
    query = getattr(provider, "last_query", {}) or {}
    try:
        status = data_status(ROOT)
    except (ValueError, OSError):
        status = {}
    return {"snapshot_id": snapshot or status.get("snapshot_id", ""),
            "price_rows": status.get("price_rows"), "cost_rows": status.get("cost_rows"),
            "hit_price": query.get("price_rows"), "hit_cost": query.get("cost_rows"),
            "parts": query.get("parts"), "sources": status.get("sources", []),
            "db": status.get("db", ""), "ingested_at": status.get("created_at", "")}


def _reference_task_id() -> str | None:
    """Return the one persisted example run; all other fixtures stay input-only."""
    for item in inbox(ROOT):
        if item["case"].key != "B" or not item.get("task_id"):
            continue
        try:
            _service().get_task_result(item["task_id"])
            return item["task_id"]
        except ValueError:
            break
    try:
        rfq, _ = load_case(ROOT, "B")
    except (OSError, ValueError):
        return None
    state = _service().start_task(rfq.to_dict(), autonomy="interactive")
    admit_case(ROOT, "B")
    mark_stage(ROOT, "B", "priced", state["task_id"])
    return state["task_id"]


class Handler(BaseHTTPRequestHandler):
    server_version = "RFQAgent/0.3"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    # ---------- helpers ----------

    def _json(self, payload, status=200):
        # Compact JSON: the old pretty payload, plus candidate arrays, was large
        # enough that a single write under the process lock stalled the browser.
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        view = memoryview(body)
        sent = 0
        while sent < len(view):
            written = self.wfile.write(view[sent:sent + 65536])
            if not written:
                break
            sent += written

    def _error(self, message, status=400):
        self._json({"error": str(message)}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _run(self, case: str, autonomy: str):
        rfq, extraction = _understand(case)
        state = _service().start_task(rfq.to_dict(), autonomy=autonomy)
        if extraction:
            state = _service().record_extraction(state["task_id"], extraction)
        admit_case(ROOT, case)
        mark_stage(ROOT, case, "priced", state["task_id"])
        return state

    # ---------- routes ----------

    def do_GET(self):
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # a dropped socket is what the browser calls ERR_EMPTY_RESPONSE
            self.log_error("GET failed: %s", exc)
            try:
                self._error(exc, 500)
            except Exception:
                return

    def _do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            page = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        if parsed.path in ("/healthz", "/health"):
            # Platform probes hit this every few seconds: it must never touch the
            # store lock, the model or the reference tables. It only reports the two
            # things that actually break a deploy: missing materials, unwritable runtime.
            runtime = ROOT / "runtime"
            self._json({
                "ok": True,
                "service": "rfq-agent",
                "materials": (ROOT / "data" / "询价信息素材").is_dir(),
                "runtime_writable": os.access(runtime if runtime.exists() else ROOT, os.W_OK),
            })
            return
        if parsed.path == "/api/cases":
            self._json([{"key": item["case"].key, "label": item["case"].label,
                         "container": item["case"].container, "file": item["case"].path.name,
                         "customer": item["case"].customer, "subject": item["case"].subject,
                         "received": item["case"].received, "lines": item["case"].lines,
                         "stage": item["stage"], "task_id": item["task_id"],
                         "imported_at": item["imported_at"], "history": item.get("history", [])}
                        for item in inbox(ROOT)])
            return
        if parsed.path == "/api/fixtures":
            # Official cases are immutable test inputs, not remembered task results.
            with LOCK:
                reference_task_id = _reference_task_id()
            fixtures = [case for case in discover(ROOT)
                        if len(case.key) == 1 and case.key in {"A", "B", "C", "D", "E", "F"}]
            self._json([{"key": case.key, "label": case.label,
                         "container": case.container, "file": case.path.name,
                         "customer": case.customer, "subject": case.subject,
                         "received": case.received, "lines": case.lines,
                         "reference": case.key == "B",
                         "task_id": reference_task_id if case.key == "B" else None}
                        for case in fixtures])
            return
        if parsed.path == "/api/material":
            self._json([{"key": case.key, "customer": case.customer, "subject": case.subject,
                         "container": case.container, "lines": case.lines,
                         "received": case.received}
                        for case in available(ROOT)])
            return
        if parsed.path == "/api/agent":
            config = load_config(ROOT)
            missing = missing_keys(config)
            self._json({"enabled": not missing, "missing": missing,
                        "model": config.get("GENESIS_MODEL") or "",
                        "base_url": config.get("GENESIS_BASE_URL") or "",
                        "hint": "" if not missing else
                                "在 rfq-agent/.env 里填 GENESIS_MODEL 和 GENESIS_ACCESS_TOKEN 后重启"})
            return
        if parsed.path == "/api/data":
            try:
                self._json(data_status(ROOT))
            except (OSError, ValueError) as exc:
                self._json({"snapshot_id": "", "price_rows": 0, "cost_rows": 0,
                            "sources": [], "error": str(exc)})
            return
        if parsed.path == "/api/task":
            task_id = (parse_qs(parsed.query).get("id") or [""])[0]
            try:
                with LOCK:
                    desk = _load_desk(task_id)
                self._json(_view(desk))
            except ValueError as exc:
                self._error(exc, 404)
            return
        if parsed.path in ("/api/extraction", "/api/lines", "/api/reviews", "/api/report"):
            query = parse_qs(parsed.query)
            task_id = (query.get("id") or [""])[0]
            offset, limit = _page_window(query)
            try:
                with LOCK:
                    desk = _load_desk(task_id)
                if parsed.path == "/api/extraction":
                    payload = _extraction_page(desk, offset, limit)
                elif parsed.path == "/api/lines":
                    payload = _line_page(desk, offset, limit)
                elif parsed.path == "/api/reviews":
                    bucket = (query.get("bucket") or ["open"])[0]
                    payload = _review_page(desk, bucket, offset, limit)
                else:
                    payload = _report_page(desk, offset, limit)
                self._json(payload)
            except ValueError as exc:
                self._error(exc, 404)
            return
        if parsed.path == "/api/source":
            query = parse_qs(parsed.query)
            task_id = (query.get("id") or [""])[0]
            requested = (query.get("file") or [""])[0]
            try:
                with LOCK:
                    _load_desk(task_id)
                    item = next((entry for entry in inbox(ROOT)
                                 if entry.get("task_id") == task_id), None)
                if item is None:
                    raise ValueError("source file not found for task")
                path = item["case"].path
                if requested:
                    attachment = next(((name, data) for name, data in read_eml(path).attachments
                                       if name == requested), None)
                    if attachment is None:
                        raise ValueError("attachment not found in original email")
                    filename, payload = attachment
                else:
                    filename, payload = path.name, path.read_bytes()
            except (ValueError, OSError) as exc:
                self._error(exc, 404)
                return
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            disposition = "inline" if content_type in ("application/pdf", "text/plain") else "attachment"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", disposition + "; filename*=UTF-8''" + quote(filename))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/analysis":
            query = parse_qs(parsed.query)
            task_id = (query.get("id") or [""])[0]
            offset, limit = _page_window(query)
            try:
                with LOCK:
                    desk = _load_desk(task_id)
                self._json(_report_page(desk, offset, limit))
            except ValueError as exc:
                self._error(exc, 404)
            return
        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            task_id = (query.get("id") or [""])[0]
            event_type = (query.get("type") or [None])[0]
            try:
                with LOCK:
                    self._json({"task_id": task_id,
                                "events": _service().task_history(task_id, event_type)})
            except ValueError as exc:
                self._error(exc, 404)
            return
        if parsed.path in {"/api/download", "/api/draft-preview"}:
            query = parse_qs(parsed.query)
            task_id = (query.get("id") or [""])[0]
            name = (query.get("file") or [""])[0]
            if not FILE_PATTERN.match(name):
                self._error("unsupported file", 400)
                return
            try:
                with LOCK:
                    service = _service()
                    task = service.store.get_desk(task_id)
                    current = {Path(item).name for item in task.get("artifacts", [])}
                    if name not in current:
                        self._error("artifact is stale or is not part of the current revision", 409)
                        return
                    path = service.store.directory(task_id) / name
                if not path.exists():
                    self._error("file not found", 404)
                    return
                if parsed.path == "/api/draft-preview":
                    if not re.fullmatch(r"quote-draft-r\d+\.docx", name):
                        self._error("请选择已生成的 quote-draft Word 草案", 400)
                        return
                    from rfq_agent.reporting.docx_preview import render
                    self._json({"file": name, "html": render(path), "revision": task["revision"]})
                    return
                payload = path.read_bytes()
            except ValueError as exc:
                self._error(exc, 400)
                return
            ctype = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                     if name.endswith(".docx") else "application/json")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._error("not found", 404)

    def do_POST(self):
        try:
            self._do_POST()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self.log_error("POST failed: %s", exc)
            try:
                self._error(exc, 500)
            except Exception:
                return

    def _do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/preview":
            # Parse-only: nothing is admitted, saved or run until the user confirms.
            name = (query.get("name") or ["upload.eml"])[0]
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._error("空文件")
                return
            payload = self.rfile.read(length)
            with LOCK:
                try:
                    token, _, rfq = stage_pending(ROOT, Path(name).name, payload)
                    view = _preview_view(rfq)
                    view["token"] = token
                    self._json(view)
                except (ValueError, OSError) as exc:
                    self._error(exc)
            return

        try:
            body = self._body()
        except ValueError:
            self._error("invalid JSON body")
            return

        # Chat runs outside the store lock: a model turn can take seconds and must not
        # block the deterministic endpoints. Each tool call does its own short I/O.
        if parsed.path == "/api/chat":
            session_id = body.get("session_id") or uuid4().hex
            session = SESSIONS.get(session_id)
            if session is None:
                session = Session(_service(), ROOT, load_config(ROOT))
                SESSIONS[session_id] = session
            if not session.enabled:
                self._json({"session_id": session_id, "enabled": False,
                            "reply": "模型未配置：" + "、".join(missing_keys(load_config(ROOT))),
                            "trace": [], "state": None}, 200)
                return
            try:
                result = session.send(body.get("message") or "", task_id=body.get("task_id"))
            except GenesisError as exc:
                self._json({"session_id": session_id, "enabled": True,
                            "error": str(exc), "trace": exc.trace}, 502)
                return
            payload = {"session_id": session_id, "enabled": True}
            payload.update(result)
            self._json(payload)
            return

        status = 200
        try:
            with LOCK:
                if parsed.path == "/api/upload":
                    saved = confirm_pending(ROOT, str(body.get("token") or ""))
                    digest = hashlib.sha256(saved.read_bytes()).hexdigest()
                    existing = next((i for i in inbox(ROOT) if i.get("task_id") and
                        hashlib.sha256(i["case"].path.read_bytes()).hexdigest() == digest), None)
                    if existing:
                        self._json({"task_id": existing["task_id"], "duplicate": True})
                        return
                    uploaded = next((case for case in discover(ROOT) if case.path == saved), None)
                    if uploaded is None:
                        raise ValueError("上传的邮件里没有可识别的询价表")
                    admit_case(ROOT, uploaded.key)
                    case = case_by_key(ROOT, uploaded.key)
                    state = self._run(uploaded.key, "interactive")
                    payload = {"key": case.key, "customer": case.customer,
                               "subject": case.subject, "container": case.container,
                               "lines": case.lines, "received": case.received,
                               "stage": "priced", "task_id": state["task_id"],
                               "process_status": state.get("process_status", state["status"]),
                               "evaluation_status": state.get("evaluation_status"),
                               "commercial_disposition": state.get("commercial_disposition"),
                               "pending_interruptions": len([
                                   item for item in state.get("interruptions", [])
                                   if item.get("status") == "pending"])}
                elif parsed.path == "/api/import":
                    case = admit_case(ROOT, body["key"])
                    payload = {"key": case.key, "customer": case.customer,
                               "subject": case.subject, "container": case.container,
                               "lines": case.lines, "received": case.received,
                               "stage": "received"}
                elif parsed.path == "/api/run":
                    state = self._run(body.get("case", "B"), body.get("autonomy") or "interactive")
                    payload = _view(_load_desk(state["task_id"]))
                elif parsed.path == "/api/reset":
                    reset_inbox(ROOT)
                    payload = {"ok": True}
                elif parsed.path == "/api/review-preview":
                    payload = _service().preview_review(body["task_id"], int(body["revision"]),
                        body["item_id"], body["option_id"], bool(body.get("amend")))
                elif parsed.path == "/api/resolve":
                    state = _service().resolve_review(
                        body["task_id"], int(body["revision"]),
                        body["item_id"], body["option_id"], body.get("note") or "", bool(body.get("amend")))
                    payload = _view(_load_desk(state["task_id"]))
                elif parsed.path == "/api/export":
                    result = _service().export_quote_draft(body["task_id"], int(body["revision"]))
                    payload = _view(_load_desk(body["task_id"]))
                    if isinstance(result, dict) and "artifacts" in result:
                        result["artifacts"] = [Path(name).name for name in result["artifacts"]]
                    payload["export"] = result
                    for item in inbox(ROOT):
                        if item["task_id"] == body["task_id"]:
                            mark_stage(ROOT, item["case"].key, "draft", body["task_id"])
                            break
                elif parsed.path == "/api/activate":
                    state = _service().activate_quote(
                        body["task_id"], int(body["revision"]), body["approved_by"])
                    payload = _view(_load_desk(state["task_id"]))
                elif parsed.path == "/api/feedback":
                    payload = _service().record_feedback(
                        body["task_id"], int(body["revision"]), body["actor"],
                        body.get("feedback") or {}, body.get("outcome"))
                    status = 201
                else:
                    payload = None
        except (ValueError, KeyError) as exc:
            self._error(exc)
            return
        if payload is None:
            self._error("not found", 404)
            return
        self._json(payload, status)


def _warm() -> None:
    """Pay one-off costs (openpyxl import, first ETL, inbox scan) at boot, not on a click."""
    try:
        import openpyxl  # noqa: F401
        import docx  # noqa: F401
        sweep_pending(ROOT)
        _service()
        discover(ROOT, refresh=True)
    except Exception:  # a cold cache must never stop the server from starting
        pass


def _default_port() -> int:
    """A PaaS injects $PORT; local runs keep 8765."""
    try:
        return int(os.environ.get("PORT") or 8765)
    except ValueError:
        return 8765


def main():
    parser = argparse.ArgumentParser(description="RFQ agent demo server")
    parser.add_argument("--port", type=int, default=_default_port(),
                        help="listen port (default: $PORT, else 8765)")
    parser.add_argument("--host", default=os.environ.get("RFQ_HOST") or "127.0.0.1",
                        help="bind address (default: $RFQ_HOST, else 127.0.0.1)")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    if not (ROOT / "data" / "询价信息素材").is_dir():
        # The one misconfiguration that produces a blank demo: code shipped without
        # the six official emails. Say it at boot instead of failing on a click.
        print("WARNING: data/询价信息素材 is missing — the inbox will be empty. "
              "Ship the data/ directory with the service.", flush=True)
    print(f"RFQ agent on http://{args.host}:{args.port}/  (Ctrl+C to stop)", flush=True)

    def _stop(_signum, _frame):
        # Containers are stopped with SIGTERM; serve_forever() only returns when
        # shutdown() is called from another thread, never from the signal handler.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _stop)
            except (ValueError, OSError):
                pass

    threading.Thread(target=_warm, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
