"""Local demo server: stdlib only, so it runs on a judge machine with no network.

    python -m rfq_agent.web --port 8765

The agent's input is the material itself: any of the six official .eml files, or one
the judge uploads. The page only shows what the pipeline produced — every number comes
from rfq_agent.pricing, every field from rfq_agent.ingest.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from . import quality
from .cases import (admit_case, available, case_by_key, data_status, discover, inbox,
                    load_case, mark_stage, provider_for, save_upload, template_for)
from .genesis import GenesisError, Session, load_config, missing_keys
from .pricing import summarize
from .service import RFQService
from .storage import TaskStore

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
FILE_PATTERN = re.compile(r"^(draft|quote-draft)-r\d+\.(json|docx)$")

LOCK = threading.Lock()
SERVICE: RFQService | None = None
SESSIONS: dict[str, Session] = {}


def _service() -> RFQService:
    global SERVICE
    if SERVICE is None:
        SERVICE = RFQService(TaskStore(ROOT / "runtime"), provider=provider_for(ROOT),
                             template=template_for(ROOT))
    return SERVICE


def _view(task: dict) -> dict:
    """Trim the task for the browser: full candidate arrays stay server-side."""
    results = []
    for row in task["results"]:
        decision = row.get("decision") or {}
        basis = decision.get("basis") or {}
        evidence = row.get("evidence") or {}
        results.append({
            "line_number": row.get("line_number"),
            "part_number": row["part_number"],
            "description": row.get("description") or "",
            "quantity": row.get("priced_quantity") or row["quantity"],
            "requested_quantity": row["quantity"],
            "unit": row["unit"],
            "eligibility": row["eligibility"],
            "unit_price": row.get("unit_price"),
            "line_total": row.get("line_total"),
            "source": decision.get("recommended_source"),
            "condition": basis.get("condition"),
            "certificate": basis.get("certificate"),
            "lead_time_days": basis.get("lead_time_days"),
            "moq": basis.get("moq"),
            "ships_from": basis.get("ships_from"),
            "valid_until": basis.get("valid_until"),
            "supplier_name": basis.get("supplier_name"),
            "quantity_basis": row.get("quantity_basis"),
            "provenance": f"{evidence.get('container', '')} · 第 {evidence.get('row', '?')} 行",
            "reasons": [item["detail"] for item in decision.get("review_evidence", [])],
            "warnings": decision.get("warnings", []),
        })
    rfq = task["rfq"]
    container = (task["results"][0].get("evidence") or {}).get("container", "") if task["results"] else ""
    container = container or (rfq.get("lines") or [{}])[0].get("evidence", {}).get("container", "")
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
    stage = ("draft" if task["artifacts"] else "priced" if task["results"]
             else "parsed" if task["status"] == "analyzed" else "received")
    quality_findings = quality.inspect(rfq)
    return {
        "task_id": task["task_id"],
        "revision": task["revision"],
        "status": task["status"],
        "autonomy": task["autonomy"],
        "policy_version": task["policy_version"],
        "data_snapshot_id": task["data_snapshot_id"],
        "customer": rfq.get("customer", ""),
        "rfq_number": rfq.get("rfq_number", ""),
        "subject": rfq.get("subject", ""),
        "evaluation_date": rfq.get("evaluation_date", ""),
        "source_files": rfq.get("source_files", []),
        "container": container,
        "summary": summarize(task["results"]),
        "stage": stage,
        "lines": lines,
        "results": results,
        "review_items": task["review_items"],
        "decision_log": task["decision_log"],
        "artifacts": [Path(name).name for name in task["artifacts"]],
        "fields": _field_health(lines),
        "quality": quality_findings,
        "quality_summary": quality.summary(quality_findings),
        "requirements": rfq.get("requirements", []),
        "data": _data_trace(),
    }


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


class Handler(BaseHTTPRequestHandler):
    server_version = "RFQAgent/0.2"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    # ---------- helpers ----------

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._json({"error": str(message)}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _run(self, case: str, autonomy: str):
        rfq, _ = load_case(ROOT, case)
        state = _service().start_task(rfq.to_dict(), autonomy=autonomy)
        admit_case(ROOT, case)
        mark_stage(ROOT, case, "priced", state["task_id"])
        return state

    # ---------- routes ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            page = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if parsed.path == "/api/cases":
            self._json([{"key": item["case"].key, "label": item["case"].label,
                         "container": item["case"].container, "file": item["case"].path.name,
                         "customer": item["case"].customer, "subject": item["case"].subject,
                         "received": item["case"].received, "lines": item["case"].lines,
                         "stage": item["stage"], "task_id": item["task_id"],
                         "imported_at": item["imported_at"]}
                        for item in inbox(ROOT)])
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
            self._json(data_status(ROOT))
            return
        if parsed.path == "/api/task":
            task_id = (parse_qs(parsed.query).get("id") or [""])[0]
            try:
                with LOCK:
                    self._json(_view(_service().get_task_result(task_id)))
            except ValueError as exc:
                self._error(exc, 404)
            return
        if parsed.path == "/api/download":
            query = parse_qs(parsed.query)
            task_id = (query.get("id") or [""])[0]
            name = (query.get("file") or [""])[0]
            if not FILE_PATTERN.match(name):
                self._error("unsupported file", 400)
                return
            try:
                with LOCK:
                    path = _service().store.directory(task_id) / name
                if not path.exists():
                    self._error("file not found", 404)
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
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/upload":
            name = (query.get("name") or ["upload.eml"])[0]
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length)
            with LOCK:
                try:
                    saved = save_upload(ROOT, Path(name).name, payload)
                    uploaded = next((case for case in discover(ROOT) if case.path == saved), None)
                    if uploaded is None:
                        raise ValueError("上传的邮件里没有可识别的询价表")
                    admit_case(ROOT, uploaded.key)
                    case = case_by_key(ROOT, uploaded.key)
                    self._json({"key": case.key, "customer": case.customer,
                                "subject": case.subject, "container": case.container,
                                "lines": case.lines, "received": case.received, "stage": "received"})
                except (ValueError, KeyError, OSError) as exc:
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
                            "error": str(exc), "trace": []}, 502)
                return
            payload = {"session_id": session_id, "enabled": True}
            payload.update(result)
            self._json(payload)
            return

        with LOCK:
            try:
                if parsed.path == "/api/import":
                    case = admit_case(ROOT, body["key"])
                    self._json({"key": case.key, "customer": case.customer,
                                "subject": case.subject, "container": case.container,
                                "lines": case.lines, "received": case.received,
                                "stage": "received"})
                    return
                if parsed.path == "/api/run":
                    state = self._run(body.get("case", "B"), body.get("autonomy") or "interactive")
                    self._json(_view(state))
                    return
                if parsed.path == "/api/resolve":
                    state = _service().resolve_review(
                        body["task_id"], int(body["revision"]),
                        body["item_id"], body["option_id"])
                    self._json(_view(state))
                    return
                if parsed.path == "/api/export":
                    result = _service().export_quote_draft(body["task_id"], int(body["revision"]))
                    payload = _view(_service().get_task_result(body["task_id"]))
                    if isinstance(result, dict) and "artifacts" in result:
                        result["artifacts"] = [Path(name).name for name in result["artifacts"]]
                    payload["export"] = result
                    for item in inbox(ROOT):
                        if item["task_id"] == body["task_id"]:
                            mark_stage(ROOT, item["case"].key, "draft", body["task_id"])
                            break
                    self._json(payload)
                    return
            except (ValueError, KeyError) as exc:
                self._error(exc)
                return
        self._error("not found", 404)


def _warm() -> None:
    """Pay one-off costs (openpyxl import, first ETL, inbox scan) at boot, not on a click."""
    try:
        import openpyxl  # noqa: F401
        import docx  # noqa: F401
        _service()
        discover(ROOT, refresh=True)
    except Exception:  # a cold cache must never stop the server from starting
        pass


def main():
    parser = argparse.ArgumentParser(description="RFQ agent demo server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"RFQ agent on http://{args.host}:{args.port}/  (Ctrl+C to stop)", flush=True)
    threading.Thread(target=_warm, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()