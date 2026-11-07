"""Five business actions plus the review gate.

The gate is the product: automated work runs to completion, blocking questions are
turned into answerable items, and the task resumes from where it stopped instead of
being recomputed from zero.
"""
import copy
import datetime as dt
import json
from uuid import uuid4

from rfq_agent.domain.models import RFQ, Task, positive_decimal
from rfq_agent.domain.input_status import input_status
from rfq_agent.infrastructure.ledger import AGENT_TRACE, ARTIFACT, HUMAN_DECISION, OperationalLedger
from rfq_agent.domain.policy import POLICY_VERSION, AUTONOMY_PRESETS, DEFAULT_POLICY
from rfq_agent.domain.pricing import build_review_items, summarize
from rfq_agent.domain.ports import PendingQuoteProvider, QuoteProvider
from rfq_agent.infrastructure.storage import TaskStore


class RFQService:
    def __init__(self, store: TaskStore, provider: QuoteProvider | None = None,
                 policy: dict | None = None, template=None,
                 ledger: OperationalLedger | None = None, history_db=None):
        self.store = store
        self.provider = provider or PendingQuoteProvider()
        self.policy = policy or DEFAULT_POLICY
        self.template = template
        self.ledger = ledger
        # Approved-quote history (quote_header / quote_line). Defaults next to the
        # task store, which is runtime/rfq.db for the web app.
        self.history_db = history_db or (store.root / "rfq.db")

    @staticmethod
    def _now() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _set_status(task: Task, status: str, process_status: str | None = None) -> None:
        """Update the legacy API status and the v2 execution status together."""
        task.status = status
        task.process_status = process_status or status

    def _trace(self, task: Task, action: str, **details) -> None:
        entry = {
            "action": action,
            "revision": task.revision,
            "at": self._now(),
            **details,
        }
        task.agent_trace.append(entry)
        if self.ledger is not None:
            self.ledger.append_event(
                task_id=task.task_id, revision=task.revision,
                event_type=AGENT_TRACE, payload=entry,
                policy_version=task.policy_version,
                data_snapshot_id=task.data_snapshot_id)

    def _save(self, task: Task) -> None:
        """Persist current state and append an immutable replay snapshot."""
        self.store.save(task)
        if self.ledger is not None:
            self.ledger.record_task_snapshot(task)

    # ---------- task lifecycle ----------

    def create_task(self, data: dict, autonomy: str = "interactive") -> dict:
        if autonomy not in AUTONOMY_PRESETS:
            raise ValueError(f"Unknown autonomy preset; expected one of {AUTONOMY_PRESETS}")
        task = Task(uuid4().hex, RFQ.from_dict(data), autonomy=autonomy,
                    policy_version=self.policy.get("policy_version", POLICY_VERSION),
                    data_snapshot_id=getattr(self.provider, "snapshot_id", ""))
        task.events.append({"action": "created", "revision": 1, "autonomy": autonomy})
        self._trace(task, "task_created", outcome="continue")
        self._save(task)
        return task.to_dict()

    def _current(self, task_id: str, revision: int) -> Task:
        task = self.store.get(task_id)
        if task.revision != revision:
            raise ValueError("Stale revision; retrieve the current task before acting")
        return task

    def start_task(self, data: dict, autonomy: str = "interactive") -> dict:
        """Run automatically until draft-ready or a durable human interruption."""
        task = self.create_task(data, autonomy=autonomy)
        self.analyze_rfq(task["task_id"])
        return self.build_quote_options(task["task_id"], task["revision"], autonomy=autonomy)

    def analyze_rfq(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        RFQ.from_dict(task.to_dict()["rfq"])
        if task.status == "received":
            self._set_status(task, "analyzed", "validated")
            task.evaluation_status = "in_progress"
            task.events.append({"action": "analyzed", "revision": task.revision})
            self._trace(task, "rfq_validated", outcome="continue")
            self._save(task)
        return task.to_dict()

    def record_extraction(self, task_id: str, report: dict) -> dict:
        """Persist how the input was understood, independently of pricing.

        The report is the perception layer's audit trail (which blocks were read, what
        the model refused, which rows look misread). It is evidence, never a price
        input, so it lives on the trace and not in the results.
        """
        task = self.store.get(task_id)
        status = input_status(report)
        self._trace(task, "input_understood",
                    model_ran=status["model_ran"], degraded=status["degraded"],
                    fallback_code=status["fallback_code"], label=status["label"],
                    source=report.get("source", ""),
                    model=report.get("model", ""),
                    document_type=report.get("document_type", ""),
                    lines_model=report.get("lines_model", 0),
                    lines_deterministic=report.get("lines_deterministic", 0),
                    refused=len(report.get("refused") or []),
                    best_guess=len(report.get("best_guess") or []),
                    reason=status["reason"],
                    outcome="continue")
        task.extraction = report
        self._save(task)
        return task.to_dict()

    # ---------- pricing gate ----------

    @staticmethod
    def _derive_disposition(task: Task) -> str:
        """Derive the commercial outcome conservatively from settled results."""
        if task.open_reviews() or not task.results:
            return "PENDING"
        task_choice = task.resolutions.get("__task__")
        if task_choice == "reject_task":
            return "NO_BID"
        if task_choice == "request_quotation":
            return "SUPPLIER_INQUIRY"
        priced = sum(1 for row in task.results if row.get("eligibility") == "priced")
        if priced == len(task.results):
            return "QUOTE"
        if priced:
            return "PARTIAL_QUOTE"
        if "request_quotation" in task.resolutions.values():
            return "SUPPLIER_INQUIRY"
        return "NO_BID"

    def _sync_interruptions(self, task: Task) -> None:
        """Persist blocking review items as resumable human interruptions."""
        open_items = {item["id"]: item for item in task.open_reviews()}
        for interruption in task.interruptions:
            if (interruption.get("status") == "pending" and
                    (interruption.get("review_item_id") not in open_items or
                     interruption.get("revision") != task.revision)):
                interruption["status"] = "superseded"
                interruption["resolved_at"] = self._now()

        pending_keys = {
            (item.get("review_item_id"), item.get("revision"))
            for item in task.interruptions if item.get("status") == "pending"
        }
        for item_id, item in open_items.items():
            if (item_id, task.revision) in pending_keys:
                continue
            task.interruptions.append({
                "id": f"INT-r{task.revision}-{item_id}",
                "type": "human_review",
                "gate": "pricing_exception",
                "status": "pending",
                "review_item_id": item_id,
                "revision": task.revision,
                "proposed_action": "resolve_review",
                "question": item.get("question"),
                "evidence": copy.deepcopy(item.get("evidence", [])),
                "options": copy.deepcopy(item.get("options", [])),
                "created_at": self._now(),
            })

    def _settle(self, task: Task) -> None:
        from rfq_agent.domain.business_rules import check_business
        from rfq_agent.domain.evaluation import validate
        task.evaluation_issues = validate(task.rfq, task.results)
        task.business_checks, business_issues = check_business(task.rfq, task.results)
        task.evaluation_issues = sorted(set(task.evaluation_issues) | set(business_issues))
        task.review_items = build_review_items(task.rfq, task.results, task.resolutions)
        if task.open_reviews():
            self._set_status(task, "awaiting_review", "awaiting_human")
            task.evaluation_status = "incomplete"
        elif task.evaluation_issues:
            self._set_status(task, "evaluation_incomplete", "evaluation_incomplete")
            task.evaluation_status = "incomplete"
        else:
            self._set_status(task, "draft_ready")
            task.evaluation_status = "complete"
        task.commercial_disposition = ("PENDING" if task.evaluation_issues else
                                       self._derive_disposition(task))
        self._sync_interruptions(task)

    def build_quote_options(self, task_id: str, revision: int, autonomy: str | None = None) -> dict:
        task = self._current(task_id, revision)
        if task.status == "received":
            raise ValueError("Analyze the RFQ before pricing")
        if autonomy:
            if autonomy not in AUTONOMY_PRESETS:
                raise ValueError(f"Unknown autonomy preset; expected one of {AUTONOMY_PRESETS}")
            task.autonomy = autonomy

        task.results = self.provider.evaluate(task.rfq, task.resolutions)
        self._settle(task)

        if task.autonomy == "auto_draft" and task.open_reviews():
            for item in task.open_reviews():
                key = item.get("resolution_key") or item["target"].get("line_id") or "__task__"
                task.resolutions[key] = item["default_option"]
                task.decision_log.append({
                    "actor": "system:auto_draft", "item_id": item["id"],
                    "option": item["default_option"], "revision": task.revision,
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "note": "按预设默认选项自动解算，未改变任何价格约束",
                })
            task.results = self.provider.evaluate(task.rfq, task.resolutions)
            self._settle(task)

        task.artifacts = []
        task.events.append({"action": "priced", "revision": task.revision,
                            "status": task.status, "open_reviews": len(task.open_reviews())})
        self._trace(task, "quote_evaluated",
                    outcome="pause" if task.open_reviews() else "draft_ready",
                    evaluation_status=task.evaluation_status,
                    commercial_disposition=task.commercial_disposition)
        self._save(task)
        return task.to_dict()

    def resolve_review(self, task_id: str, revision: int, item_id: str, option_id: str) -> dict:
        task = self._current(task_id, revision)
        item = next((entry for entry in task.review_items if entry["id"] == item_id), None)
        if item is None:
            raise ValueError("Unknown review item")
        if item["status"] != "open":
            raise ValueError("Review item already answered")
        if option_id not in {option["id"] for option in item["options"]}:
            raise ValueError("Option is not offered by this review item")

        interruption = next((entry for entry in reversed(task.interruptions)
                             if entry.get("review_item_id") == item_id and
                             entry.get("revision") == revision and
                             entry.get("status") == "pending"), None)
        if interruption is not None:
            interruption["status"] = "resolved"
            interruption["decision"] = option_id
            interruption["resolved_by"] = "human"
            interruption["resolved_at"] = self._now()

        key = item.get("resolution_key") or item["target"].get("line_id") or "__task__"
        task.resolutions[key] = option_id
        task.results = self.provider.evaluate(task.rfq, task.resolutions)
        task.revision += 1
        task.artifacts = []
        self._settle(task)
        task.decision_log.append({
            "actor": "human", "item_id": item_id, "option": option_id,
            "revision": task.revision,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        })
        task.events.append({"action": "review_resolved", "revision": task.revision,
                            "item_id": item_id, "option": option_id, "status": task.status})
        self._trace(task, "human_decision_applied", outcome="resume",
                    review_item_id=item_id, option=option_id,
                    commercial_disposition=task.commercial_disposition)
        if self.ledger is not None:
            self.ledger.append_event(
                task_id=task.task_id, revision=task.revision,
                event_type=HUMAN_DECISION,
                payload={"review_item_id": item_id, "option": option_id,
                         "actor": "human", "resumed": True},
                policy_version=task.policy_version,
                data_snapshot_id=task.data_snapshot_id)
        self._save(task)
        return task.to_dict()

    # ---------- legacy field edit ----------

    def apply_review(self, task_id: str, revision: int, line_id: str, quantity: str) -> dict:
        task = self._current(task_id, revision)
        positive_decimal(quantity)
        target = next((line for line in task.rfq.lines if line.line_id == line_id), None)
        if target is None:
            raise ValueError("Unknown line ID")
        before = target.quantity
        target.evidence = copy.deepcopy(target.evidence)
        target.evidence.setdefault("revisions", []).append(
            {"field": "quantity", "before": before, "after": quantity})
        target.quantity = quantity
        task.revision += 1
        self._set_status(task, "received")
        task.evaluation_status = "pending"
        task.commercial_disposition = "PENDING"
        for interruption in task.interruptions:
            if interruption.get("status") == "pending":
                interruption["status"] = "superseded"
                interruption["resolved_at"] = self._now()
        task.results = []
        task.review_items = []
        task.artifacts = []
        task.events.append({"action": "quantity_updated", "revision": task.revision, "line_id": line_id})
        self._trace(task, "rfq_field_updated", outcome="restart_evaluation", line_id=line_id)
        self._save(task)
        return task.to_dict()

    # ---------- export ----------

    def export_quote_draft(self, task_id: str, revision: int, render_docx: bool = True) -> dict:
        task = self._current(task_id, revision)
        if not task.results:
            raise ValueError("Build quote options before exporting")
        directory = self.store.directory(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        summary = summarize(task.results)

        payload = {
            "label": "DRAFT — NOT A COMMERCIAL QUOTE",
            "task_id": task_id,
            "revision": revision,
            "status": task.status,
            "process_status": task.process_status,
            "commercial_disposition": task.commercial_disposition,
            "evaluation_status": task.evaluation_status,
            "autonomy": task.autonomy,
            "policy_version": task.policy_version,
            "data_snapshot_id": task.data_snapshot_id,
            "rfq": task.to_dict()["rfq"],
            "summary": summary,
            "review_items": task.review_items,
            "resolutions": task.resolutions,
            "decision_log": task.decision_log,
            "results": task.results,
            "evaluation_issues": task.evaluation_issues,
        }
        from rfq_agent.reporting.analysis import build as build_analysis

        analysis = build_analysis(task)
        payload["business_checks"] = task.business_checks
        payload["analysis"] = analysis
        artifacts = []
        json_path = directory / f"draft-r{revision}.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts.append(str(json_path))

        # Every task gets a human-readable analysis report, quotable or not.
        if render_docx:
            from rfq_agent.reporting.analysis import render_docx as render_analysis
            try:
                analysis_path = render_analysis(analysis, directory / f"analysis-r{revision}.docx")
                artifacts.append(str(analysis_path))
            except ImportError:
                pass   # python-docx unavailable: the JSON record still carries the analysis

        # A supplier inquiry or no-bid result can have an analysis record, but
        # it must not be packaged as a quotation document.
        quote_draft_allowed = (task.evaluation_status == "complete" and
                               not task.open_reviews() and
                               task.commercial_disposition in {"QUOTE", "PARTIAL_QUOTE"})
        if render_docx and self.template is not None and quote_draft_allowed:
            from rfq_agent.reporting.export_docx import render_draft
            docx_path = directory / f"quote-draft-r{revision}.docx"
            render_draft(template=self.template, output=docx_path, rfq=task.rfq,
                         results=task.results, summary=summary,
                         policy_version=task.policy_version,
                         evaluation_date=task.rfq.evaluation_date)
            artifacts.append(str(docx_path))

        task.artifacts = artifacts
        if not task.open_reviews():
            self._set_status(task, "exported", "draft_exported")
        task.events.append({"action": "exported", "revision": revision, "artifacts": len(artifacts)})
        self._trace(task, "draft_exported", outcome="preview",
                    blocking_reviews=len(task.open_reviews()))
        if self.ledger is not None:
            self.ledger.append_event(
                task_id=task.task_id, revision=task.revision,
                event_type=ARTIFACT,
                payload={"kind": "quote_preview", "paths": list(artifacts),
                         "commercial": False},
                policy_version=task.policy_version,
                data_snapshot_id=task.data_snapshot_id)
        self._save(task)
        return {"task_id": task_id, "revision": revision, "status": task.status,
                "artifact": artifacts[0], "artifacts": artifacts, "summary": summary,
                "analysis_summary": analysis["summary"]}

    def analysis(self, task_id: str) -> dict:
        """Structured inquiry analysis for the current revision (web panel / API)."""
        from rfq_agent.reporting.analysis import build as build_analysis

        return build_analysis(self.store.get(task_id))

    def activate_quote(self, task_id: str, revision: int, approved_by: str) -> dict:
        """Freeze a commercially quotable revision after explicit human approval.

        Fail-closed: only a named human may approve, automatic review defaults never
        count, and the policy must allow activation (the default demo policy does not;
        ``policy.DEMO_APPROVED_POLICY`` / RFQ_DEMO_APPROVAL=1 does, and stamps 演示批准).
        Approval writes quote_header/quote_line and renders ``quote-r{rev}.docx`` in one
        SQLite transaction: if rendering fails nothing is recorded.
        """
        task = self._current(task_id, revision)
        approver = (approved_by or "").strip()
        if not approver:
            raise ValueError("approved_by is required")
        if approver.lower().startswith("system:"):
            raise ValueError("A system actor cannot approve a commercial quote")
        if task.status == "active":
            raise ValueError("This revision is already activated")
        if task.evaluation_status != "complete":
            raise ValueError("Evaluation is incomplete; quote cannot be activated")
        if task.open_reviews():
            raise ValueError("Blocking human review is unresolved")
        if task.commercial_disposition not in {"QUOTE", "PARTIAL_QUOTE"}:
            raise ValueError("Commercial disposition is not quotable")
        if not task.results:
            raise ValueError("Quote has no evaluated lines")
        if not self.policy.get("commercial_approval_enabled", False):
            raise ValueError("Demo pricing policy is not approved for commercial activation")
        if task.policy_version != self.policy.get("policy_version"):
            raise ValueError("Pricing policy changed; re-evaluate before activation")
        if any(item.get("actor") == "system:auto_draft" for item in task.decision_log):
            raise ValueError("Automatic review defaults cannot approve a commercial quote")

        from rfq_agent.infrastructure.db import connect, insert_quote

        mode = self.policy.get("approval_mode", "commercial")  # demo policy sets "demo"
        approval = {
            "approved_by": approver,
            "approved_at": self._now(),
            "mode": mode,
            "label": self.policy.get("approval_label") or ("演示批准" if mode == "demo" else "人工批准"),
            "revision": revision,
            "policy_version": task.policy_version,
        }
        summary = summarize(task.results)
        quote_id = f"Q-{task.task_id[:12]}-r{revision}"
        directory = self.store.directory(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        docx_path = directory / f"quote-r{revision}.docx"
        header = {
            "quote_id": quote_id, "task_id": task.task_id, "revision": revision,
            "rfq_number": task.rfq.rfq_number, "customer": task.rfq.customer or "",
            "currency": task.rfq.currency, "disposition": task.commercial_disposition,
            "total_lines": summary["lines"], "priced_lines": summary["priced_lines"],
            "subtotal_cny": summary["priced_subtotal_cny"], "approved_by": approver,
            "approved_at": approval["approved_at"], "approval_mode": mode,
            "policy_version": task.policy_version, "data_snapshot_id": task.data_snapshot_id,
            "document": docx_path.name if self.template is not None else None,
        }
        lines = []
        for row in task.results:
            if row.get("eligibility") != "priced":
                continue
            decision = row.get("decision") or {}
            basis = decision.get("basis") or {}
            lines.append({
                "quote_id": quote_id, "line_id": row["line_id"],
                "line_number": row.get("line_number"), "part_number": row["part_number"],
                "description": row.get("description") or "",
                "quantity": str(row.get("priced_quantity") or row["quantity"]),
                "unit": row["unit"], "unit_price": row["unit_price"],
                "line_total": row["line_total"],
                "source_type": decision.get("recommended_source") or "",
                "source_id": decision.get("selected_source_id") or "",
                "certificate": basis.get("certificate"), "condition": basis.get("condition"),
                "match_type": decision.get("match_type") or basis.get("match_type"),
            })

        connection = connect(self.history_db)
        try:
            with connection:   # one transaction: history rows + final document, or neither
                insert_quote(connection, header, lines)
                if self.template is not None:
                    from rfq_agent.reporting.export_docx import render_draft
                    try:
                        render_draft(template=self.template, output=docx_path, rfq=task.rfq,
                                     results=task.results, summary=summary,
                                     policy_version=task.policy_version,
                                     evaluation_date=task.rfq.evaluation_date,
                                     approval=approval)
                    except Exception:
                        docx_path.unlink(missing_ok=True)
                        raise
        finally:
            connection.close()

        approval["quote_id"] = quote_id
        approval["document"] = header["document"]
        task.approval = approval
        if self.template is not None:
            task.artifacts = [*task.artifacts, str(docx_path)]
        self._set_status(task, "active", "quote_activated")
        task.decision_log.append({
            "actor": approver,
            "action": "activate_quote",
            "revision": revision,
            "approval_mode": mode,
            "quote_id": quote_id,
            "at": approval["approved_at"],
        })
        task.events.append({"action": "quote_activated", "revision": revision,
                            "approved_by": approver, "quote_id": quote_id,
                            "approval_mode": mode,
                            "commercial_disposition": task.commercial_disposition})
        self._trace(task, "quote_activated", outcome="active",
                    approved_by=approver, approval_mode=mode, quote_id=quote_id)
        if self.ledger is not None:
            self.ledger.append_event(
                task_id=task.task_id, revision=task.revision,
                event_type=HUMAN_DECISION,
                payload={"action": "activate_quote", "actor": approver,
                         "approval_mode": mode, "quote_id": quote_id,
                         "commercial_disposition": task.commercial_disposition},
                policy_version=task.policy_version,
                data_snapshot_id=task.data_snapshot_id)
            if self.template is not None:
                self.ledger.append_event(
                    task_id=task.task_id, revision=task.revision, event_type=ARTIFACT,
                    payload={"kind": "approved_quote", "paths": [str(docx_path)],
                             "commercial": mode != "demo", "approval_mode": mode},
                    policy_version=task.policy_version,
                    data_snapshot_id=task.data_snapshot_id)
        self._save(task)
        return task.to_dict()

    def task_history(self, task_id: str, event_type: str | None = None) -> list[dict]:
        if self.ledger is None:
            return []
        # Validate the identifier against the task store before querying the ledger.
        self.store.directory(task_id)
        return self.ledger.events_for_task(task_id, event_type=event_type)

    def record_feedback(self, task_id: str, revision: int, actor: str,
                        feedback: dict, outcome: str | None = None) -> dict:
        task = self._current(task_id, revision)
        if self.ledger is None:
            raise ValueError("Operational ledger is not configured")
        event = self.ledger.record_feedback(
            task_id=task.task_id, revision=task.revision, actor=actor,
            feedback=feedback, outcome=outcome,
            policy_version=task.policy_version,
            data_snapshot_id=task.data_snapshot_id)
        self._trace(task, "feedback_recorded", outcome="evidence_only",
                    feedback_event_id=event["event_id"])
        self._save(task)
        return event

    def get_task_result(self, task_id: str) -> dict:
        return self.store.get(task_id).to_dict()
