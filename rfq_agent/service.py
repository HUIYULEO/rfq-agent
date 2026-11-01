"""Five business actions plus the review gate.

The gate is the product: automated work runs to completion, blocking questions are
turned into answerable items, and the task resumes from where it stopped instead of
being recomputed from zero.
"""
import copy
import datetime as dt
import json
from uuid import uuid4

from .models import RFQ, Task, positive_decimal
from .policy import POLICY_VERSION, AUTONOMY_PRESETS, DEFAULT_POLICY
from .pricing import build_review_items, summarize
from .ports import PendingQuoteProvider, QuoteProvider
from .storage import TaskStore


class RFQService:
    def __init__(self, store: TaskStore, provider: QuoteProvider | None = None,
                 policy: dict | None = None, template=None):
        self.store = store
        self.provider = provider or PendingQuoteProvider()
        self.policy = policy or DEFAULT_POLICY
        self.template = template

    # ---------- task lifecycle ----------

    def create_task(self, data: dict, autonomy: str = "interactive") -> dict:
        if autonomy not in AUTONOMY_PRESETS:
            raise ValueError(f"Unknown autonomy preset; expected one of {AUTONOMY_PRESETS}")
        task = Task(uuid4().hex, RFQ.from_dict(data), autonomy=autonomy,
                    policy_version=self.policy.get("policy_version", POLICY_VERSION),
                    data_snapshot_id=getattr(self.provider, "snapshot_id", ""))
        task.events.append({"action": "created", "revision": 1, "autonomy": autonomy})
        self.store.save(task)
        return task.to_dict()

    def _current(self, task_id: str, revision: int) -> Task:
        task = self.store.get(task_id)
        if task.revision != revision:
            raise ValueError("Stale revision; retrieve the current task before acting")
        return task

    def start_task(self, data: dict, autonomy: str = "interactive") -> dict:
        """Create, validate and price in one call — the agent's entry point."""
        task = self.create_task(data, autonomy=autonomy)
        self.analyze_rfq(task["task_id"])
        return self.build_quote_options(task["task_id"], task["revision"], autonomy=autonomy)

    def analyze_rfq(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        RFQ.from_dict(task.to_dict()["rfq"])
        if task.status == "received":
            task.status = "analyzed"
            task.events.append({"action": "analyzed", "revision": task.revision})
            self.store.save(task)
        return task.to_dict()

    # ---------- pricing gate ----------

    def _settle(self, task: Task) -> None:
        task.review_items = build_review_items(task.rfq, task.results, task.resolutions)
        task.status = "awaiting_review" if task.open_reviews() else "draft_ready"

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
                key = item["target"].get("line_id") or "__task__"
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
        self.store.save(task)
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

        key = item["target"].get("line_id") or "__task__"
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
        self.store.save(task)
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
        task.status = "received"
        task.results = []
        task.review_items = []
        task.artifacts = []
        task.events.append({"action": "quantity_updated", "revision": task.revision, "line_id": line_id})
        self.store.save(task)
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
            "autonomy": task.autonomy,
            "policy_version": task.policy_version,
            "data_snapshot_id": task.data_snapshot_id,
            "rfq": task.to_dict()["rfq"],
            "summary": summary,
            "review_items": task.review_items,
            "resolutions": task.resolutions,
            "decision_log": task.decision_log,
            "results": task.results,
        }
        artifacts = []
        json_path = directory / f"draft-r{revision}.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts.append(str(json_path))

        if render_docx and self.template is not None:
            from .export_docx import render_draft
            docx_path = directory / f"quote-draft-r{revision}.docx"
            render_draft(template=self.template, output=docx_path, rfq=task.rfq,
                         results=task.results, summary=summary,
                         policy_version=task.policy_version,
                         evaluation_date=task.rfq.evaluation_date)
            artifacts.append(str(docx_path))

        task.artifacts = artifacts
        if not task.open_reviews():
            task.status = "exported"
        task.events.append({"action": "exported", "revision": revision, "artifacts": len(artifacts)})
        self.store.save(task)
        return {"task_id": task_id, "revision": revision, "status": task.status,
                "artifact": artifacts[0], "artifacts": artifacts, "summary": summary}

    def get_task_result(self, task_id: str) -> dict:
        return self.store.get(task_id).to_dict()