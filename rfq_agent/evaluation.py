"""Independent completeness checks for the current deterministic price results.

This checks the provider's output contract: every requested line must be
accounted for, and priced decisions must have candidate and money evidence.
It does not certify business eligibility or commercial policy.
"""

from decimal import Decimal, InvalidOperation

from .policy import line_amount


def _money(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def validate(rfq, results: list[dict]) -> list[str]:
    """Return concrete defects that must block the downstream quote gate."""
    by_id: dict[str, list[dict]] = {}
    issues: list[str] = []
    expected_lines = {line.line_id for line in rfq.lines}
    for row in results:
        line_id = row.get("line_id") if isinstance(row, dict) else None
        if line_id not in expected_lines:
            issues.append(f"unexpected_result:{line_id}")
            continue
        by_id.setdefault(line_id, []).append(row)

    for line in rfq.lines:
        rows = by_id.get(line.line_id, [])
        if len(rows) != 1:
            detail = "missing" if not rows else f"duplicate:{len(rows)}"
            issues.append(f"result_present:{line.line_id}:{detail}")
            continue

        row = rows[0]
        same_input = (row.get("part_number") == line.part_number and
                      row.get("quantity") == line.quantity and
                      row.get("unit") == line.unit)
        if not same_input:
            issues.append(f"input_identity:{line.line_id}")

        candidates = row.get("internal_candidates"), row.get("supplier_candidates")
        candidate_shape = all(isinstance(group, list) for group in candidates)
        candidate_rows = [candidate for group in candidates if isinstance(group, list)
                          for candidate in group if isinstance(candidate, dict)]
        decision = row.get("decision")
        priced = row.get("eligibility") == "priced"
        if priced:
            selected = decision.get("selected_source_id") if isinstance(decision, dict) else None
            source_ok = bool(selected) and any(
                c.get("source_id") == selected and c.get("eligibility") == "eligible"
                for c in candidate_rows)
            evidence_ok = candidate_shape and source_ok
        else:
            evidence_ok = candidate_shape and row.get("eligibility") == "needs_review"
        if not evidence_ok:
            issues.append(f"candidate_evidence:{line.line_id}")

        trace_ok = isinstance(decision, dict) and (
            bool(decision.get("selected_source_id")) if priced else
            bool(decision.get("review_evidence")))
        if not trace_ok:
            issues.append(f"decision_trace:{line.line_id}")

        if priced:
            unit_price = _money(row.get("unit_price"))
            quantity = _money(row.get("priced_quantity"))
            total = _money(row.get("line_total"))
            amount_ok = (unit_price is not None and quantity is not None and
                         total is not None and unit_price > 0 and quantity > 0 and
                         total == line_amount(unit_price, quantity))
            if not amount_ok:
                issues.append(f"amount_consistency:{line.line_id}")
        else:
            if row.get("unit_price") is not None or row.get("line_total") is not None:
                issues.append(f"unpriced_money:{line.line_id}")

    return sorted(set(issues))
