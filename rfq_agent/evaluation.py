"""Independent completeness checks for the current deterministic price results.

This is a first, deliberately small rule catalog. It verifies that the provider
evaluated every requested line and that a priced decision can be reconstructed
from its candidate and money evidence. It does not certify commercial policy.
"""

from collections import Counter
from decimal import Decimal, InvalidOperation


RULES = ("result_present", "input_identity", "candidate_evidence",
         "decision_trace", "amount_consistency")


def expected_rule_instances(rfq) -> set[tuple[str, str]]:
    return {(line.line_id, rule) for line in rfq.lines for rule in RULES}


def _money(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def validate(rfq, results: list[dict]) -> tuple[list[dict], list[str]]:
    """Return one four-valued result per expected instance, plus contract errors."""
    by_id: dict[str, list[dict]] = {}
    issues: list[str] = []
    expected_lines = {line.line_id for line in rfq.lines}
    for row in results:
        line_id = row.get("line_id") if isinstance(row, dict) else None
        if line_id not in expected_lines:
            issues.append(f"unexpected_result:{line_id}")
            continue
        by_id.setdefault(line_id, []).append(row)

    actual: list[dict] = []

    def record(line_id: str, rule: str, status: str, detail: str):
        actual.append({"line_id": line_id, "rule": rule,
                       "status": status, "detail": detail})

    for line in rfq.lines:
        rows = by_id.get(line.line_id, [])
        if len(rows) != 1:
            detail = "missing" if not rows else f"duplicate:{len(rows)}"
            record(line.line_id, "result_present", "fail", detail)
            issues.append(f"result_present:{line.line_id}:{detail}")
            for rule in RULES[1:]:
                record(line.line_id, rule, "unknown", "no unique result")
            continue

        row = rows[0]
        record(line.line_id, "result_present", "pass", "exactly one result")
        same_input = (row.get("part_number") == line.part_number and
                      row.get("quantity") == line.quantity and
                      row.get("unit") == line.unit)
        record(line.line_id, "input_identity", "pass" if same_input else "fail",
               "result matches RFQ identity, quantity and unit" if same_input else
               "result differs from RFQ input")

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
        record(line.line_id, "candidate_evidence", "pass" if evidence_ok else "fail",
               "selected eligible candidate or explicit unpriced candidate set" if evidence_ok
               else "candidate set or selected source is missing/inconsistent")

        trace_ok = isinstance(decision, dict) and (
            bool(decision.get("selected_source_id")) if priced else
            bool(decision.get("review_evidence")))
        record(line.line_id, "decision_trace", "pass" if trace_ok else "fail",
               "decision has source or review evidence" if trace_ok else
               "decision lacks source or review evidence")

        if priced:
            unit_price = _money(row.get("unit_price"))
            quantity = _money(row.get("priced_quantity"))
            total = _money(row.get("line_total"))
            amount_ok = (unit_price is not None and quantity is not None and
                         total is not None and unit_price > 0 and quantity > 0 and
                         total == unit_price * quantity)
            status = "pass" if amount_ok else "fail"
            detail = "line total equals unit price times priced quantity" if amount_ok else "amount cannot be reconstructed"
        else:
            status = "not_applicable" if row.get("unit_price") is None and row.get("line_total") is None else "fail"
            detail = "no price committed" if status == "not_applicable" else "unpriced row contains money"
        record(line.line_id, "amount_consistency", status, detail)

    expected = expected_rule_instances(rfq)
    counted = Counter((item["line_id"], item["rule"]) for item in actual)
    if set(counted) != expected or any(count != 1 for count in counted.values()):
        issues.append("rule_instance_set_mismatch")
    issues.extend(f"{item['rule']}:{item['line_id']}:{item['status']}" for item in actual
                  if item["status"] in {"fail", "unknown"})
    return actual, sorted(set(issues))
