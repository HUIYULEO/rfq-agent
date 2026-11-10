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
    evidence: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    # A customer may accept several conditions ("NE, NS, OH"); matching is set
    # membership, never equality against a single code.
    condition_accept: list[str] = field(default_factory=list)
    priority: str | None = None
    due_at: str | None = None  # Legacy field: customer's quote-response deadline.
    inquiry_no: str | None = None
    allow_alternates: bool = False
    trace_required: str | None = None
    ships_from: str | None = None
    # Per-line customer fields carried from the inquiry table (see ingest.HEADER_ALIASES).
    product_requirement: str | None = None   # 询价产品要求, e.g. SPECIAL REQUIREMENT / CERT REQUIRED
    mpn: str | None = None                   # 询价MPN
    manual_pn: str | None = None             # 手册件号
    product_scope: str | None = None         # 产品范围
    required_certificates: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.line_id or not self.part_number or not self.unit:
            raise ValueError("line_id, part_number and unit are required")
        positive_decimal(self.quantity)
        if isinstance(self.condition_accept, str):
            self.condition_accept = [self.condition_accept]
        if isinstance(self.required_certificates, str):
            self.required_certificates = [self.required_certificates]


@dataclass
class Requirement:
    """A quoting condition the buyer stated in prose — not a price input, but binding."""
    text: str
    source: str = ""          # "邮件正文" / "Quote.pdf" / ...

    def to_dict(self):
        return asdict(self)


@dataclass
class RFQ:
    rfq_number: str
    evaluation_date: str
    lines: list[Line]
    source_files: list[str]
    customer: str = ""
    subject: str = ""
    currency: str = "CNY"
    urgency: str = "NORMAL"
    requirements: list[Requirement] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict):
        date.fromisoformat(data["evaluation_date"])
        lines = [Line(**line) for line in data["lines"]]
        if not lines or len({x.line_id for x in lines}) != len(lines):
            raise ValueError("RFQ must have nonempty, unique line IDs")
        known = {f for f in cls.__dataclass_fields__ if f != "lines"}
        extra = {k: v for k, v in data.items() if k in known}
        extra["requirements"] = [Requirement(**item) for item in data.get("requirements", [])]
        return cls(lines=lines, **extra)

    def to_dict(self):
        return asdict(self)


@dataclass
class Task:
    task_id: str
    rfq: RFQ
    revision: int = 1
    status: str = "received"
    results: list[dict] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    # Gate bookkeeping: what was asked, what the human (or the preset) answered.
    autonomy: str = "interactive"
    policy_version: str = ""
    data_snapshot_id: str = ""
    review_items: list[dict] = field(default_factory=list)
    resolutions: dict[str, str] = field(default_factory=dict)
    decision_log: list[dict] = field(default_factory=list)
    # v2 separates execution progress from the commercial decision. ``status``
    # remains as a compatibility mirror for the existing API and UI.
    process_status: str = "received"
    commercial_disposition: str = "PENDING"
    evaluation_status: str = "pending"
    evaluation_issues: list[str] = field(default_factory=list)
    # Business-rule checks (match type, certificate, unit, condition, quote deadline):
    # one entry per (line, rule), passes included, each with a locator back to the
    # source cell. Separate from evaluation_issues (contract defects); never a price input.
    business_checks: list[dict] = field(default_factory=list)
    approval: dict = field(default_factory=dict)
    # A blocking question is an interruption of one durable run, not a new chat.
    interruptions: list[dict] = field(default_factory=list)
    agent_trace: list[dict] = field(default_factory=list)
    # How the input was read (perception.py): blocks, confidence, refusals.
    extraction: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        payload = {**data, "rfq": RFQ.from_dict(data["rfq"])}
        # Old task.json files predate process_status. Preserve their real
        # lifecycle position instead of silently resetting them to received.
        payload.setdefault("process_status", payload.get("status", "received"))
        payload.pop("mode", None)  # Legacy value was never read by any workflow gate.
        payload.setdefault("commercial_disposition", "PENDING")
        payload.setdefault("evaluation_status", "pending")
        payload.setdefault("interruptions", [])
        payload.setdefault("agent_trace", [])
        payload.pop("rule_results", None)  # Drop the redundant field from older task snapshots.
        payload.setdefault("evaluation_issues", [])
        payload.setdefault("business_checks", [])
        payload.setdefault("approval", {})
        payload.setdefault("extraction", {})
        # A runtime/ directory can outlive a schema change (an older snapshot may
        # carry fields this build no longer has, e.g. the removed ``triage``).
        # Keep what this Task knows and drop the rest, the way RFQ.from_dict does,
        # so a stale file is still readable instead of 500-ing every request.
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in payload.items() if key in known})

    def open_reviews(self) -> list[dict]:
        return [item for item in self.review_items
                if item.get("severity") == "blocking" and item.get("status") == "open"]
