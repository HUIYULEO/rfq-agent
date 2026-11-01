"""Five business actions; preconditions are enforced outside the model."""
import copy
import json
from uuid import uuid4
from .models import RFQ, positive_decimal
from .storage import TaskStore
from .ports import QuoteProvider, PendingQuoteProvider


class RFQService:
    def __init__(self, store: TaskStore, provider: QuoteProvider | None = None):
        self.store = store
        self.provider = provider or PendingQuoteProvider()

    def create_task(self, data: dict):
        from .models import Task
        task = Task(uuid4().hex, RFQ.from_dict(data))
        task.events.append({"action": "created", "revision": 1})
        self.store.save(task)
        return task.to_dict()

    def _current(self, task_id: str, revision: int):
        task = self.store.get(task_id)
        if task.revision != revision:
            raise ValueError("Stale revision; retrieve the current task before acting")
        return task

    def analyze_rfq(self, task_id: str):
        task = self.store.get(task_id)
        RFQ.from_dict(task.to_dict()["rfq"])
        if task.status == "received":
            task.status = "analyzed"
            task.events.append({"action": "analyzed", "revision": task.revision})
            self.store.save(task)
        return task.to_dict()

    def build_quote_options(self, task_id: str, revision: int):
        task = self._current(task_id, revision)
        if task.status == "received":
            raise ValueError("Analyze the RFQ before pricing")
        task.results = self.provider.evaluate(task.rfq)
        task.status = "needs_review"
        task.artifacts = []
        task.events.append({"action": "options_built", "revision": revision})
        self.store.save(task)
        return task.to_dict()

    def apply_review(self, task_id: str, revision: int, line_id: str, quantity: str):
        task = self._current(task_id, revision)
        positive_decimal(quantity)
        target = next((line for line in task.rfq.lines if line.line_id == line_id), None)
        if target is None:
            raise ValueError("Unknown line ID")
        before = target.quantity
        target.evidence = copy.deepcopy(target.evidence)
        target.evidence.setdefault("revisions", []).append({"field": "quantity", "before": before, "after": quantity})
        target.quantity = quantity
        task.revision += 1
        task.status = "received"
        task.results = []
        task.artifacts = []
        task.events.append({"action": "quantity_updated", "revision": task.revision, "line_id": line_id})
        self.store.save(task)
        return task.to_dict()

    def export_quote_draft(self, task_id: str, revision: int):
        task = self._current(task_id, revision)
        if not task.results:
            raise ValueError("Build quote options before exporting")
        # JSON is the scaffold artifact. Official DOCX rendering is a later adapter.
        path = self.store.directory(task_id) / f"draft-r{revision}.json"
        path.write_text(json.dumps({"label": "SCAFFOLD — NOT A COMMERCIAL QUOTE",
                                   "task_id": task_id, "revision": revision,
                                   "rfq": task.to_dict()["rfq"], "results": task.results},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        task.artifacts = [str(path)]
        self.store.save(task)
        return {"task_id": task_id, "revision": revision, "artifact": str(path), "status": task.status}

    def get_task_result(self, task_id: str):
        return self.store.get(task_id).to_dict()
