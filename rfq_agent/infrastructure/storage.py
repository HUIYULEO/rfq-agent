"""Task-scoped JSON persistence; single-process scaffold."""
import json
import os
import re
from pathlib import Path

from rfq_agent.domain.models import Task


# Candidate arrays are audit evidence for the engine. The desk never renders them,
# and parsing them is what made a large task (case A and anything wider) stall the
# load request before a single byte was sent.
_CANDIDATE_KEYS = ("internal_candidates", "supplier_candidates")


def without_candidates(data: dict) -> dict:
    """Shallow copy of a task dict with per-line candidate arrays removed."""
    results = []
    for row in data.get("results") or []:
        if isinstance(row, dict):
            results.append({key: value for key, value in row.items() if key not in _CANDIDATE_KEYS})
        else:
            results.append(row)
    slim = dict(data)
    slim["results"] = results
    return slim


class TaskStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, task_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise ValueError("Invalid task ID")
        return self.root / task_id

    def save(self, task: Task):
        directory = self.directory(task.task_id)
        directory.mkdir(exist_ok=True)
        data = task.to_dict()
        temporary = directory / "task.json.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, directory / "task.json")
        self._write_desk(directory, data)

    def _write_desk(self, directory: Path, data: dict) -> None:
        temporary = directory / "desk.json.tmp"
        temporary.write_text(
            json.dumps(without_candidates(data), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
        os.replace(temporary, directory / "desk.json")

    def get(self, task_id: str) -> Task:
        path = self.directory(task_id) / "task.json"
        if not path.exists():
            raise ValueError("Task not found")
        return Task.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def get_desk(self, task_id: str) -> dict:
        """Slim snapshot for the quote desk. Never parses candidate arrays when present.

        Older tasks only have ``task.json``. The first read builds ``desk.json`` so
        the next load stays small.
        """
        directory = self.directory(task_id)
        desk = directory / "desk.json"
        if desk.exists():
            try:
                return json.loads(desk.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        path = directory / "task.json"
        if not path.exists():
            raise ValueError("Task not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        self._write_desk(directory, data)
        return without_candidates(data)
