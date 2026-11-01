"""Validated domain contracts. Money is serialized as decimal strings."""
from dataclasses import dataclass, field, asdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


def positive_decimal(value: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("Value must be finite and positive")
    return number


@dataclass
class Line:
    line_id: str
    part_number: str
    quantity: str
    unit: str
    evidence: dict[str, Any]

    def __post_init__(self):
        if not self.line_id or not self.part_number or not self.unit:
            raise ValueError("line_id, part_number and unit are required")
        positive_decimal(self.quantity)


@dataclass
class RFQ:
    rfq_number: str
    evaluation_date: str
    lines: list[Line]
    source_files: list[str]

    @classmethod
    def from_dict(cls, data: dict):
        date.fromisoformat(data["evaluation_date"])
        lines = [Line(**line) for line in data["lines"]]
        if not lines or len({x.line_id for x in lines}) != len(lines):
            raise ValueError("RFQ must have nonempty, unique line IDs")
        return cls(data["rfq_number"], data["evaluation_date"], lines, data["source_files"])


@dataclass
class Task:
    task_id: str
    rfq: RFQ
    revision: int = 1
    status: str = "received"
    mode: str = "offline_demo"
    results: list[dict] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "rfq": RFQ.from_dict(data["rfq"])})
