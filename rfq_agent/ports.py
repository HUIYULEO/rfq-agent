"""Replaceable document and pricing adapters, independent of LLM libraries."""
from pathlib import Path
from typing import Protocol
from .models import RFQ


class DocumentParser(Protocol):
    def parse(self, files: list[Path]) -> RFQ: ...


class QuoteProvider(Protocol):
    def evaluate(self, rfq: RFQ) -> list[dict]: ...


class PendingQuoteProvider:
    """Truthful default until the official-data rule engine is implemented."""
    def evaluate(self, rfq: RFQ) -> list[dict]:
        return [{"line_id": line.line_id, "part_number": line.part_number,
                 "eligibility": "needs_review", "unit_price": None, "line_total": None,
                 "reason": "Official pricing-data adapter is not implemented"}
                for line in rfq.lines]
