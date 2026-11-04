"""Replaceable document and pricing adapters, independent of LLM libraries."""
from pathlib import Path
from typing import Protocol

from rfq_agent.domain.models import RFQ


class DocumentParser(Protocol):
    def parse(self, files: list[Path]) -> RFQ: ...


class QuoteProvider(Protocol):
    def evaluate(self, rfq: RFQ, resolutions: dict | None = None) -> list[dict]: ...


class PendingQuoteProvider:
    """Truthful default until the official-data rule engine is wired in."""
    snapshot_id = ""

    def evaluate(self, rfq: RFQ, resolutions: dict | None = None) -> list[dict]:
        return [{"line_id": line.line_id, "part_number": line.part_number,
                 "quantity": line.quantity, "unit": line.unit,
                 "eligibility": "needs_review", "unit_price": None, "line_total": None,
                 "decision": {"requires_human_review": True,
                              "recommended_source": "manual",
                              "decision_reasons": ["Official pricing-data adapter is not implemented"],
                              "review_evidence": [{"rule": "no_price_evidence",
                                                   "detail": "未接价格数据适配器"}]},
                 "internal_candidates": [], "supplier_candidates": []}
                for line in rfq.lines]
