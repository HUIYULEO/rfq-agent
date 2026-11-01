"""Task-scoped JSON persistence; single-process scaffold."""
import json
import os
import re
from pathlib import Path
from .models import Task


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
        temporary = directory / "task.json.tmp"
        temporary.write_text(json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, directory / "task.json")

    def get(self, task_id: str) -> Task:
        path = self.directory(task_id) / "task.json"
        if not path.exists():
            raise ValueError("Task not found")
        return Task.from_dict(json.loads(path.read_text(encoding="utf-8")))
