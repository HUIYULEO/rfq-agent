"""Deterministic pricing engine.

Pure functions over (RFQ, price table, cost table, policy). No model, no network,
no clock: the same inputs always produce the same decimals. The Agent layer may
explain this output but never computes it.
"""
from __future__ import annotations

import datetime as dt

from .policy import DEFAULT_POLICY, dec, money, q2

MONEY_RULES = ("status_invalid", "date_expired", "moq_exceeds", "condition_mismatch",
               "condition_unknown", "stock_insufficient", "cost_missing", "price_missing",
               "fx_missing", "no_price_record")


def _s(value) -> str:
    return "" if value is None else str(value).strip()


def _date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def _detail_condition(candidate_condition: str, accept: list[str]) -> str:
    if len(accept) == 1:
        return f"候选件状 {candidate_condition or '(空)'} ≠ 需求 {accept[0]}"
    return f"候选件状 {candidate_condition or '(空)'} 不属于可接受集合 {accept}"


class PriceTableQuoteProvider:
    """Supplier-side lookups against the two official reference tables."""

    def __init__(self, price_rows: list[dict], cost_rows: list[dict],
                 policy: dict | None = None, snapshot_id: str = ""):
        self.price_rows = price_rows
        self.cost_rows = cost_rows
        self.policy = policy or DEFAULT_POLICY
        self.snapshot_id = snapshot_id

    # ---------- candidate construction ----------

    def _stock_candidates(self, line, ledger: dict, ignore: set[str]):
        usable, all_rows = [], []
        qty = dec(line.quantity)
        for row in self.cost_rows:
            if _s(row.get("Part Number")) != line.part_number:
                continue
            reasons = []
            on_hand = dec(row.get("库存数量") or 0)
            remaining = ledger.get(row["_source_row"], on_hand)
            if "stock_insufficient" not in ignore and remaining < qty:
                suffix = "" if remaining == on_hand else f"（原始库存 {on_hand}，已被同任务其他行占用）"
                reasons.append({"rule": "stock_insufficient",
                                "detail": f"剩余可分配库存 {remaining} < 需求 {qty}{suffix}"})
            condition = _s(row.get("库存件状"))
            if "condition_mismatch" not in ignore and "condition_unknown" not in ignore:
                if line.condition_accept and condition not in line.condition_accept:
                    reasons.append({"rule": "condition_mismatch",
                                    "detail": f"库存件状 {condition or '(空)'} ≠ 需求 {line.condition_accept[0]}"
                                    if len(line.condition_accept) == 1 else
                                    f"库存件状 {condition or '(空)'} 不属于可接受集合 {line.condition_accept}"})
                elif not line.condition_accept and not condition:
                    reasons.append({"rule": "condition_unknown",
                                    "detail": "客户未指定件状，库存也未标注件状，报价单无法声明"})
            standard_cost = _s(row.get("标准成本"))
            try:
                if not standard_cost or dec(standard_cost) <= 0:
                    raise ValueError
            except Exception:
                reasons.append({"rule": "cost_missing",
                                "detail": "内部库存记录缺少有效的正数标准成本"})
            candidate = {
                "source_type": "internal",
                "source_id": f"STOCK-{row['_source_row']:03d}",
                "warehouse": row.get("仓库"),
                "condition": condition or None,
                "stock_on_hand": str(on_hand),
                "available_quantity": str(remaining),
                "standard_cost": standard_cost,
                "average_cost": _s(row.get("平均成本")),
                "last_purchase_price": _s(row.get("最近采购价")),
                "currency": _s(row.get("币种")) or "CNY",
                "product_scope": row.get("产品范围"),
                "preferred_supplier": row.get("首选供应商编码"),
                "cost_updated_at": row.get("成本更新日期").isoformat()
                if isinstance(row.get("成本更新日期"), (dt.date, dt.datetime)) else None,
                "eligibility": "rejected" if reasons else "eligible",
                "rejection_reasons": reasons,
                "source_row": row["_source_row"],
            }
            all_rows.append(candidate)
            if not reasons:
                usable.append(candidate)
        return usable, all_rows

    def _supplier_candidates(self, line, ignore: set[str]):
        usable, all_rows = [], []
        qty = dec(line.quantity)
        evaluation = dt.date.fromisoformat(self._evaluation_date)
        fx_table = self.policy["fx_to_cny"]
        for row in self.price_rows:
            if _s(row.get("Part Number")) != line.part_number:
                continue
            reasons = []
            if "status_invalid" not in ignore and _s(row.get("价格状态")) != "有效":
                reasons.append({"rule": "status_invalid", "detail": f"价格状态={_s(row.get('价格状态'))}"})
            valid_until = _date(row.get("有效期至"))
            if "date_expired" not in ignore and valid_until and valid_until < evaluation:
                reasons.append({"rule": "date_expired",
                                "detail": f"有效期至 {valid_until} 早于评估日 {evaluation}"})
            moq = dec(row.get("最小起订量") or 0)
            if "moq_exceeds" not in ignore and moq > qty:
                reasons.append({"rule": "moq_exceeds", "detail": f"MOQ {moq} > 需求 {qty}"})
            condition = _s(row.get("件状条件"))
            if "condition_mismatch" not in ignore and "condition_unknown" not in ignore:
                if line.condition_accept and condition not in line.condition_accept:
                    reasons.append({"rule": "condition_mismatch",
                                    "detail": f"候选件状 {condition or '(空)'} ≠ 需求 {line.condition_accept[0]}"
                                    if len(line.condition_accept) == 1 else
                                    f"候选件状 {condition or '(空)'} 不属于可接受集合 {line.condition_accept}"})
                elif not line.condition_accept and not condition:
                    reasons.append({"rule": "condition_unknown",
                                    "detail": "客户未指定件状，候选也未标注件状，报价单无法声明"})
            currency = _s(row.get("币种")) or "CNY"
            raw_price = _s(row.get("单价"))
            raw_rate = _s(row.get("汇率到CNY")) or _s(fx_table.get(currency))
            price = rate = unit_cny = None
            try:
                price = dec(raw_price)
                if price <= 0:
                    raise ValueError
            except Exception:
                reasons.append({"rule": "price_missing",
                                "detail": "供应商价格记录缺少有效的正数单价"})
            try:
                rate = dec(raw_rate)
                if rate <= 0:
                    raise ValueError
            except Exception:
                reasons.append({"rule": "fx_missing",
                                "detail": f"币种 {currency} 缺少有效的到 CNY 汇率"})
            if price is not None and rate is not None:
                unit_cny = q2(price * rate)
            candidate = {
                "source_type": "supplier",
                "source_id": _s(row.get("价格资源编号")),
                "supplier_code": _s(row.get("供应商编码")),
                "supplier_name": _s(row.get("供应商名称")),
                "supplier_type": _s(row.get("供应商类型")),
                "country": _s(row.get("国家地区")),
                "condition": condition,
                "certificate": _s(row.get("证书类型")),
                "unit_cost_original": _s(row.get("单价")),
                "currency": currency,
                "exchange_rate": str(rate) if rate is not None else None,
                "unit_cost_cny": str(unit_cny) if unit_cny is not None else None,
                "moq": str(moq),
                "lead_time_days": _s(row.get("交期天数")),
                "in_stock": _s(row.get("是否现货")) == "是",
                "available_quantity": None,
                "quote_date": _date(row.get("报价日期")).isoformat() if _date(row.get("报价日期")) else None,
                "valid_until": valid_until.isoformat() if valid_until else None,
                "price_status": _s(row.get("价格状态")),
                "remark": row.get("备注"),
                "eligibility": "rejected" if reasons else "eligible",
                "rejection_reasons": reasons,
                "source_row": row["_source_row"],
            }
            all_rows.append(candidate)
            if not reasons:
                usable.append(candidate)
        usable.sort(key=lambda c: (dec(c["unit_cost_cny"]), c["source_id"]))
        return usable, all_rows

    # ---------- per-line decision ----------

    def _price_line(self, line, ledger: dict, resolution: str | None) -> dict:
        ignore: set[str] = set()
        if resolution == "quote_with_moq":
            ignore.add("moq_exceeds")
        elif resolution == "accept_alternate_condition":
            ignore |= {"condition_mismatch", "condition_unknown"}

        stock_ok, stock_all = self._stock_candidates(line, ledger, ignore)
        price_ok, price_all = self._supplier_candidates(line, ignore)
        qty = dec(line.quantity)
        markup = dec(self.policy["default_markup_rate"])

        if resolution in (None, "quote_with_moq", "accept_alternate_condition") and (stock_ok or price_ok):
            if stock_ok:
                chosen = stock_ok[0]
                ledger[chosen["source_row"]] = dec(chosen["available_quantity"]) - qty
                unit_cost = dec(chosen["standard_cost"])
                basis = {
                    "source_type": "internal", "warehouse": chosen["warehouse"],
                    "condition": chosen["condition"], "certificate": None,
                    "available_quantity": chosen["available_quantity"],
                    "lead_time_days": None, "moq": "0", "ships_from": "内部库存",
                    "valid_until": None, "supplier_name": None,
                }
                reasons = [f"内部库存 {chosen['available_quantity']} ≥ 需求 {qty}，件状 {line.condition_accept[0] if line.condition_accept else '(未声明)'} 相符",
                           f"成本基线=标准成本 {chosen['standard_cost']}，加价 {markup * 100:.0f}%"]
                warnings = []
                source = "internal"
                cost_type = "standard_cost"
            else:
                chosen = price_ok[0]
                unit_cost = dec(chosen["unit_cost_cny"])
                basis = {
                    "source_type": "supplier", "warehouse": None,
                    "condition": chosen["condition"], "certificate": chosen["certificate"],
                    "available_quantity": None, "lead_time_days": chosen["lead_time_days"],
                    "moq": chosen["moq"], "ships_from": chosen["country"],
                    "valid_until": chosen["valid_until"], "supplier_name": chosen["supplier_name"],
                }
                reasons = [f"合格供应商候选 {len(price_ok)} 个，取统一成本最低者 {chosen['source_id']}",
                           f"{chosen['currency']} {chosen['unit_cost_original']} × 汇率 {chosen['exchange_rate']} = CNY {chosen['unit_cost_cny']}",
                           f"MOQ {chosen['moq']} ≤ 需求 {qty}，件状 {chosen['condition']} 相符，有效期至 {chosen['valid_until']}"]
                warnings = ["供应商未提供可售数量（仅现货标志），数量待确认；不据此拒绝报价建议"]
                source = "supplier"
                cost_type = "supplier_price"

            unit_price = q2(unit_cost * (1 + markup))
            priced_qty = qty
            quantity_basis = "requested"
            if resolution == "quote_with_moq" and dec(basis["moq"]) > qty:
                priced_qty = dec(basis["moq"])
                quantity_basis = "moq"
                warnings = list(warnings) + [f"按供应商最小起订量 {basis['moq']} 计价，客户需按 {basis['moq']} 起订"]
            if resolution == "accept_alternate_condition":
                warnings = list(warnings) + [f"已接受替代件状 {basis['condition']}（客户原要求 {line.condition_accept}）"]
            if not line.condition_accept and basis.get("condition"):
                warnings = list(warnings) + [
                    f"客户未提出件状要求，按 {basis['condition']} 报价并在报价单上注明"]

            return {
                "line_id": line.line_id,
                "eligibility": "priced",
                "unit_price": money(unit_price),
                "line_total": money(unit_price * priced_qty),
                "priced_quantity": str(priced_qty),
                "quantity_basis": quantity_basis,
                "unit_cost_cny": money(unit_cost),
                "decision": {
                    "recommended_source": source,
                    "selected_source_id": chosen["source_id"],
                    "cost_basis_type": cost_type,
                    "unit_cost_original": chosen.get("standard_cost") or chosen.get("unit_cost_original"),
                    "currency": chosen.get("currency", "CNY"),
                    "exchange_rate": chosen.get("exchange_rate", "1"),
                    "unit_cost_cny": money(unit_cost),
                    "markup_rate": self.policy["default_markup_rate"],
                    "recommended_unit_price": money(unit_price),
                    "line_total": money(unit_price * priced_qty),
                    "confidence": "high" if source == "internal" else "medium",
                    "requires_human_review": False,
                    "resolution": resolution,
                    "decision_reasons": reasons,
                    "warnings": warnings,
                    "basis": basis,
                },
                "internal_candidates": stock_all,
                "supplier_candidates": price_all,
            }

        reasons: list[dict] = []
        if stock_all:
            reasons += stock_all[0]["rejection_reasons"]
        for candidate in price_all:
            reasons += candidate["rejection_reasons"]
        deduped, seen = [], set()
        for reason in reasons:
            key = (reason["rule"], reason["detail"])
            if key not in seen:
                seen.add(key)
                deduped.append(reason)
        if not stock_all and not price_all:
            deduped.append({"rule": "no_price_record", "detail": f"{line.part_number} 在两张参考表中均无记录"})
        return {
            "line_id": line.line_id,
            "eligibility": "needs_review",
            "unit_price": None,
            "line_total": None,
            "unit_cost_cny": None,
            "decision": {
                "recommended_source": "manual",
                "selected_source_id": None,
                "confidence": "low",
                "requires_human_review": True,
                "resolution": resolution,
                "unit_cost_cny": None,
                "recommended_unit_price": None,
                "line_total": None,
                "decision_reasons": ["无满足全部硬约束的候选，转人工"],
                "warnings": ["不得为凑数而放宽件状或超额采购"],
                "basis": None,
                "review_evidence": deduped,
            },
            "internal_candidates": stock_all,
            "supplier_candidates": price_all,
        }

    # ---------- task level ----------

    def evaluate(self, rfq, resolutions: dict | None = None) -> list[dict]:
        self._evaluation_date = rfq.evaluation_date
        resolutions = resolutions or {}
        ledger: dict[int, float] = {}
        results = []
        for line in rfq.lines:
            outcome = self._price_line(line, ledger, resolutions.get(line.line_id))
            outcome.update({
                "line_number": int(line.line_id.lstrip("L")) if line.line_id.lstrip("L").isdigit() else None,
                "part_number": line.part_number,
                "description": line.description,
                "quantity": line.quantity,
                "unit": line.unit,
                "condition_accept": list(line.condition_accept),
                "due_at": line.due_at,
                "evidence": dict(line.evidence or {}),
            })
            results.append(outcome)
        return results


class SQLiteQuoteProvider(PriceTableQuoteProvider):
    """Same engine, but the reference rows come from SQLite instead of the workbooks.

    Rows are fetched per RFQ (one indexed query on the part numbers in play), so a
    request never scans 505 rows and never re-parses an .xlsx.
    """

    def __init__(self, connection, snapshot_id: str, policy: dict | None = None):
        super().__init__([], [], policy=policy, snapshot_id=snapshot_id)
        self.connection = connection
        self.last_query = {"price_rows": 0, "cost_rows": 0, "parts": 0}

    def evaluate(self, rfq, resolutions: dict | None = None) -> list[dict]:
        from .db import rows_for

        parts = {line.part_number for line in rfq.lines if line.part_number}
        self.price_rows, self.cost_rows = rows_for(self.connection, self.snapshot_id, parts)
        self.last_query = {"price_rows": len(self.price_rows), "cost_rows": len(self.cost_rows),
                           "parts": len(parts)}
        return super().evaluate(rfq, resolutions)


# ---------- review items ----------

def build_review_items(rfq, results: list[dict], resolutions: dict | None = None) -> list[dict]:
    """Turn blocked lines into answerable questions; group them when they repeat."""
    resolutions = resolutions or {}
    items: list[dict] = []
    blocked = [row for row in results if row["eligibility"] == "needs_review"]
    priced = [row for row in results if row["eligibility"] == "priced"]

    # Zero coverage is one decision, not one question per line: asking a buyer 122
    # times "what now?" is how HITL products die.
    if results and not priced:
        answered = resolutions.get("__task__")
        return [{
            "id": "RI-TASK",
            "scope": "task",
            "target": {"type": "task", "line_count": len(results)},
            "kinds": ["no_price_evidence"],
            "severity": "blocking",
            "question": f"本案 {len(results)} 行在参考表中均无可用价格依据，是否整体转人工询价？",
            "options": [
                {"id": "request_quotation", "label": "整体转人工询价，本期不出报价"},
                {"id": "reject_task", "label": "回复客户无法报价"},
            ],
            "default_option": "request_quotation",
            "evidence": [{"rule": "no_price_evidence",
                          "detail": "全部行在价格资源表与内部物料成本表中均无满足条件的候选"}],
            "status": "answered" if answered else "open",
            "answer": answered,
        }]

    for row in blocked:
        rules = [e["rule"] for e in row["decision"].get("review_evidence", [])]
        line_id = row["line_id"]
        answered = resolutions.get(line_id)
        options = [{"id": "request_quotation", "label": "转人工询价，本期不报价"}]
        if "moq_exceeds" in rules:
            options.append({"id": "quote_with_moq", "label": "按供应商 MOQ 超额采购并报价"})
        if "condition_mismatch" in rules:
            options.append({"id": "accept_alternate_condition", "label": "接受替代件状并报价"})
        line_number = row.get("line_number")
        item_id = f"RI-{line_number:04d}" if isinstance(line_number, int) else f"RI-{line_id}"
        items.append({
            # Stable per target: renumbering after an answer would break the audit trail.
            "id": item_id,
            "scope": "line",
            "target": {"type": "line", "line_id": line_id,
                       "line_number": row.get("line_number"), "part_number": row["part_number"]},
            "kinds": sorted(set(rules)) or ["unpriced"],
            "severity": "blocking",
            "question": f"{row['part_number']}（需求 {row['quantity']}，件状 "
                        f"{'/'.join(row.get('condition_accept') or ['未声明'])}）无可用候选，请选择处理方式",
            "options": options,
            "default_option": "request_quotation",
            "evidence": row["decision"].get("review_evidence", []),
            "status": "answered" if answered else "open",
            "answer": answered,
        })

    return items


def summarize(results: list[dict]) -> dict:
    priced = [row for row in results if row["eligibility"] == "priced"]
    subtotal = sum((dec(row["line_total"]) for row in priced), dec(0))
    return {
        "lines": len(results),
        "priced_lines": len(priced),
        "review_lines": len(results) - len(priced),
        "internal_lines": sum(1 for row in priced if row["decision"]["recommended_source"] == "internal"),
        "supplier_lines": sum(1 for row in priced if row["decision"]["recommended_source"] == "supplier"),
        "priced_subtotal_cny": money(subtotal),
    }
