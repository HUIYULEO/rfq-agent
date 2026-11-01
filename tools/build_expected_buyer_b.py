"""生成 buyer_b.json 黄金数据草案（机器起草，待人工复核）。

规则来源：refer_doc/RFQ_SOLUTION_DESIGN.md §14（演示默认策略）
  成本基线      : 内部标准成本优先，不足或过期时用供应商价
  加价与取整    : 20%，Decimal HALF_UP 保留 2 位
  拆单 / 超额MOQ: 均不允许
  价格可用性    : 状态=有效 且 有效期>=evaluation_date

同时统计案例 A（用 HTML 表格解析，验证"正文型优先解析 HTML"可行）的自动报价率。

用法：python tools/build_expected_buyer_b.py
"""
from __future__ import annotations

import datetime as dt
import email
import email.policy
import html
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "询价信息素材"
OUT = ROOT / "tests" / "fixtures" / "expected" / "buyer_b.json"

EVAL_DATE = dt.date(2026, 9, 7)
MARKUP = Decimal("0.20")

POLICY = {
    "policy_version": "demo-2026-11-01",
    "cost_basis_priority": ["standard_cost", "supplier_price"],
    "internal_inventory_priority": True,
    "default_markup_rate": "0.20",
    "rounding_mode": "HALF_UP",
    "price_decimals": 2,
    "cost_rounding": {"round_cost_to_decimals": 2, "apply_markup_after_rounding": True},
    "quote_validity_days": 30,
    "allow_split_fulfillment": False,
    "allow_moq_overbuy": False,
    "allow_expired_price_reference": False,
    "fx_to_cny": {"CNY": "1", "EUR": "7.75", "USD": "7.1"},
}


def q2(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def as_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def as_iso(value):
    if isinstance(value, dt.datetime):
        return value.isoformat(sep="T")
    if isinstance(value, dt.date):
        return value.isoformat()
    return None


def dec(value) -> Decimal:
    return Decimal(str(value))


def load_sheet(path: Path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h) for h in rows[0]]
    records = []
    for index, row in enumerate(rows[1:], start=2):
        if all(cell in (None, "") for cell in row):
            continue
        record = {header[i]: (row[i] if i < len(row) else None) for i in range(len(header))}
        record["_source_row"] = index
        records.append(record)
    wb.close()
    return header, records


def price_candidates(pn: str, qty: Decimal, condition: str):
    """返回（可用的供应商候选, 全部候选含拒绝理由）"""
    usable, all_rows = [], []
    for row in SUPPLIER:
        if str(row["Part Number"]).strip() != pn:
            continue
        reasons = []
        if str(row["价格状态"]).strip() != "有效":
            reasons.append({"rule": "status_invalid", "detail": f"价格状态={row['价格状态']}"})
        valid_until = as_date(row["有效期至"])
        if valid_until and valid_until < EVAL_DATE:
            reasons.append({"rule": "date_expired", "detail": f"有效期至 {valid_until} 早于评估日 {EVAL_DATE}"})
        moq = dec(row["最小起订量"])
        if moq > qty:
            reasons.append({"rule": "moq_exceeds", "detail": f"MOQ {moq} > 需求 {qty}"})
        if str(row["件状条件"]).strip() != condition:
            reasons.append({"rule": "condition_mismatch",
                            "detail": f"候选件状 {row['件状条件']} ≠ 需求 {condition}"})
        fx = dec(row["汇率到CNY"])
        unit_cny = q2(dec(row["单价"]) * fx)
        candidate = {
            "source_type": "supplier",
            "source_id": str(row["价格资源编号"]),
            "supplier_code": str(row["供应商编码"]),
            "supplier_name": str(row["供应商名称"]),
            "supplier_type": str(row["供应商类型"]),
            "country": str(row["国家地区"]),
            "condition": str(row["件状条件"]),
            "certificate": str(row["证书类型"]),
            "unit_cost_original": str(row["单价"]),
            "currency": str(row["币种"]),
            "exchange_rate": str(row["汇率到CNY"]),
            "unit_cost_cny": str(unit_cny),
            "moq": str(moq),
            "lead_time_days": str(row["交期天数"]),
            "in_stock": str(row["是否现货"]) == "是",
            "available_quantity": None,
            "quote_date": as_iso(row["报价日期"]),
            "valid_until": as_iso(row["有效期至"]),
            "price_status": str(row["价格状态"]),
            "remark": row["备注"],
            "eligibility": "rejected" if reasons else "eligible",
            "rejection_reasons": reasons,
            "source_row": row["_source_row"],
        }
        all_rows.append(candidate)
        if not reasons:
            usable.append(candidate)
    usable.sort(key=lambda c: (dec(c["unit_cost_cny"]), c["source_id"]))
    return usable, all_rows


def stock_candidates(pn: str, qty: Decimal, condition: str, ledger: dict | None = None):
    """ledger 为同任务累计分配台账：source_row -> 剩余可用库存（RULE-008）。"""
    ledger = ledger if ledger is not None else {}
    usable, all_rows = [], []
    for row in INTERNAL:
        if str(row["Part Number"]).strip() != pn:
            continue
        reasons = []
        stock = dec(row["库存数量"])
        remaining = ledger.get(row["_source_row"], stock)
        if remaining < qty:
            reasons.append({
                "rule": "stock_insufficient",
                "detail": f"剩余可分配库存 {remaining} < 需求 {qty}"
                          + ("" if remaining == stock else f"（原始库存 {stock}，已被同任务其他行占用）"),
            })
        stock_condition = str(row["库存件状"]).strip() if row["库存件状"] else ""
        if stock_condition != condition:
            reasons.append({"rule": "condition_mismatch",
                            "detail": f"库存件状 {stock_condition or '(空)'} ≠ 需求 {condition}"})
        candidate = {
            "source_type": "internal",
            "source_id": f"STOCK-{row['_source_row']:03d}",
            "warehouse": row["仓库"],
            "condition": stock_condition or None,
            "stock_on_hand": str(stock),
            "available_quantity": str(remaining),
            "standard_cost": str(row["标准成本"]),
            "average_cost": str(row["平均成本"]),
            "last_purchase_price": str(row["最近采购价"]),
            "currency": str(row["币种"]),
            "product_scope": row["产品范围"],
            "preferred_supplier": row["首选供应商编码"],
            "cost_updated_at": as_iso(row["成本更新日期"]),
            "eligibility": "rejected" if reasons else "eligible",
            "rejection_reasons": reasons,
            "source_row": row["_source_row"],
        }
        all_rows.append(candidate)
        if not reasons:
            usable.append(candidate)
    return usable, all_rows


def build_case_b():
    _, items = load_sheet(DATA / "1" / "附件" / "询价报价清单.xlsx")
    lines, review_items = [], []
    totals = {"internal": 0, "supplier": 0, "review": 0}
    priced_subtotal = Decimal("0")
    ledger: dict[int, Decimal] = {}

    for number, item in enumerate(items, start=1):
        pn = str(item["Part Number"]).strip()
        qty = dec(item["Quantity"])
        condition = str(item["件状条件"]).strip()

        stock_ok, stock_all = stock_candidates(pn, qty, condition, ledger)
        price_ok, price_all = price_candidates(pn, qty, condition)

        decision = None
        warnings = []
        if stock_ok:
            chosen = stock_ok[0]
            ledger[chosen["source_row"]] = dec(chosen["available_quantity"]) - qty
            unit_cost = dec(chosen["standard_cost"])
            unit_price = q2(unit_cost * (1 + MARKUP))
            totals["internal"] += 1
            decision = {
                "recommended_source": "internal",
                "selected_source_id": chosen["source_id"],
                "cost_basis_type": "standard_cost",
                "unit_cost_original": chosen["standard_cost"],
                "currency": chosen["currency"],
                "exchange_rate": "1",
                "unit_cost_cny": str(q2(unit_cost)),
                "markup_rate": POLICY["default_markup_rate"],
                "recommended_unit_price": str(unit_price),
                "line_total": str(q2(unit_price * qty)),
                "confidence": "high",
                "requires_human_review": False,
                "decision_reasons": [
                    f"内部库存 {chosen['available_quantity']} ≥ 需求 {qty}，件状 {condition} 相符",
                    f"成本基线=标准成本 {chosen['standard_cost']}，加价 20%",
                ],
                "warnings": warnings,
            }
            priced_subtotal += q2(unit_price * qty)
        elif price_ok:
            chosen = price_ok[0]
            unit_cost = dec(chosen["unit_cost_cny"])
            unit_price = q2(unit_cost * (1 + MARKUP))
            totals["supplier"] += 1
            warnings.append("供应商未提供可售数量（仅现货标志），数量待确认；不据此拒绝报价建议")
            decision = {
                "recommended_source": "supplier",
                "selected_source_id": chosen["source_id"],
                "cost_basis_type": "supplier_price",
                "unit_cost_original": chosen["unit_cost_original"],
                "currency": chosen["currency"],
                "exchange_rate": chosen["exchange_rate"],
                "unit_cost_cny": str(unit_cost),
                "markup_rate": POLICY["default_markup_rate"],
                "recommended_unit_price": str(unit_price),
                "line_total": str(q2(unit_price * qty)),
                "confidence": "medium",
                "requires_human_review": False,
                "decision_reasons": [
                    f"合格供应商候选 {len(price_ok)} 个，取统一成本最低者 {chosen['source_id']}",
                    f"{chosen['currency']} {chosen['unit_cost_original']} × 汇率 {chosen['exchange_rate']} = CNY {unit_cost}",
                    f"MOQ {chosen['moq']} ≤ 需求 {qty}，件状 {chosen['condition']} 相符，有效期至 {chosen['valid_until']}",
                ],
                "warnings": warnings,
            }
            priced_subtotal += q2(unit_price * qty)
        else:
            totals["review"] += 1
            reasons = []
            if stock_all:
                reasons += [r for r in stock_all[0]["rejection_reasons"]]
            for candidate in price_all:
                reasons += candidate["rejection_reasons"]
            deduped, seen = [], set()
            for reason in reasons:
                key = (reason["rule"], reason["detail"])
                if key not in seen:
                    seen.add(key)
                    deduped.append(reason)
            reasons = deduped
            kinds = sorted({r["rule"] for r in reasons})
            decision = {
                "recommended_source": "manual",
                "selected_source_id": None,
                "confidence": "low",
                "requires_human_review": True,
                "unit_cost_cny": None,
                "recommended_unit_price": None,
                "line_total": None,
                "decision_reasons": ["无满足全部硬约束的候选，转人工"],
                "warnings": ["不得为凑数而放宽件状或超额采购"],
            }
            review_items.append({
                "id": f"RI-{len(review_items) + 1:04d}",
                "target": {"type": "line", "line_number": number, "part_number": pn},
                "kinds": kinds,
                "severity": "blocking",
                "question": f"{pn}（需求 {qty}，件状 {condition}）无可用候选，请选择处理方式",
                "options": [
                    {"id": "quote_with_moq", "label": "按供应商 MOQ 超额采购并报价"},
                    {"id": "accept_alternate_condition", "label": "接受替代件状（如 NE）"},
                    {"id": "request_quotation", "label": "转人工询价，本期不报价"},
                ],
                "default_option": "request_quotation",
                "evidence": reasons,
                "status": "open",
                "answer": None,
            })

        lines.append({
            "line_number": number,
            "inquiry_no": str(item["询价单号"]),
            "part_number": pn,
            "description": item["Description"],
            "quantity": str(qty),
            "unit_of_measure": item["Unit Of Measure"],
            "priority": item["优先级"],
            "product_requirement": item["询价产品要求"],
            "requested_mpn": item["询价MPN"],
            "condition_required": condition,
            "manual_part_number": item["手册件号"],
            "product_scope": item["产品范围"],
            "response_due_at": as_iso(item["截止报价日期"]),
            "source": {"file": "1/附件/询价报价清单.xlsx", "sheet": "Sheet1", "row": item["_source_row"]},
            "internal_candidates": stock_all,
            "supplier_candidates": price_all,
            "decision": decision,
        })

    return {
        "_meta": {
            "case": "客户B",
            "description": "邮件 + 询价报价清单.xlsx（Excel 附件型，第一组开发集）",
            "rfq_number": "RFQ-20260902",
            "evaluation_date": EVAL_DATE.isoformat(),
            "policy": POLICY,
            "data_snapshot": {
                "price_table": "价格资源表_演示数据.xlsx",
                "cost_table": "内部物料成本表_演示数据.xlsx",
                "price_rows": len(SUPPLIER),
                "cost_rows": len(INTERNAL),
            },
            "provenance": {
                "generated_by": "tools/build_expected_buyer_b.py",
                "generated_at": dt.date.today().isoformat(),
                "status": "machine_drafted_pending_human_verification",
                "note": "黄金数据必须人工逐字段复核后才能作为验收基准；生成脚本不得自证正确。",
            },
            "open_policy_questions": [
                "供应商“是否现货”只有是/否、没有可售数量：现按 warning 处理（不阻断报价建议）。若改为 blocking，则 14 行供应商报价全部转人工。",
            ],
        },
        "rfq": {
            "rfq_number": "RFQ-20260902",
            "source_email": "1/航空询价（RFQ-20260902）.eml",
            "sender": "客户B <rfq-b@customer.test>",
            "subject": "航空询价（RFQ-20260902）",
            "inquiry_date": "2026-09-07",
            "evaluation_date": EVAL_DATE.isoformat(),
            "currency": "CNY",
            "attachments": ["询价报价清单.xlsx"],
            "global_requirements": [],
            "items": lines,
        },
        "expected_summary": {
            "lines": len(lines),
            "distinct_part_numbers": len({line["part_number"] for line in lines}),
            "priced_lines": totals["internal"] + totals["supplier"],
            "review_lines": totals["review"],
            "source_breakdown": {"internal": totals["internal"], "supplier": totals["supplier"]},
            "priced_subtotal_cny": str(q2(priced_subtotal)),
            "duplicate_part_numbers": sorted(
                {pn for pn, n in _count_pn(lines).items() if n > 1}
            ),
        },
        "review_items": review_items,
    }


def _count_pn(lines):
    counts = {}
    for line in lines:
        counts[line["part_number"]] = counts.get(line["part_number"], 0) + 1
    return counts


def strip_tags(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).replace("\xa0", " ").strip()


def parse_html_rows(eml_path: Path):
    message = email.message_from_bytes(eml_path.read_bytes(), policy=email.policy.default)
    body = None
    for part in message.walk():
        if part.get_content_type() == "text/html":
            body = part.get_content()
    if not body:
        return []
    rows = []
    for match in re.finditer(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I):
        cells = [strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", match.group(1), re.S | re.I)]
        if cells:
            rows.append(cells)
    return rows


def case_a_stats():
    rows = parse_html_rows(DATA / "1" / "航空配件询价（RFQ-20260901）.eml")
    header, data = rows[0], rows[1:]
    index = {name: i for i, name in enumerate(header)}
    priced = internal = supplier = review = 0
    ledger: dict[int, Decimal] = {}
    for cells in data:
        pn = cells[index["Part Number"]]
        qty = dec(cells[index["Quantity"]])
        condition = cells[index["件状条件"]]
        stock_ok, _ = stock_candidates(pn, qty, condition, ledger)
        price_ok, _ = price_candidates(pn, qty, condition)
        if stock_ok:
            internal += 1
            priced += 1
            ledger[stock_ok[0]["source_row"]] = dec(stock_ok[0]["available_quantity"]) - qty
        elif price_ok:
            supplier += 1
            priced += 1
        else:
            review += 1
    return {
        "html_rows": len(data),
        "columns": len(header),
        "distinct_pn": len({c[index["Part Number"]] for c in data}),
        "priced": priced,
        "internal": internal,
        "supplier": supplier,
        "review": review,
    }


SUPPLIER_HEADER, SUPPLIER = load_sheet(DATA / "价格资源表_演示数据.xlsx")
INTERNAL_HEADER, INTERNAL = load_sheet(DATA / "内部物料成本表_演示数据.xlsx")


def main():
    payload = build_case_b()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = payload["expected_summary"]
    print("written:", OUT)
    print("summary:", json.dumps(summary, ensure_ascii=False))
    print("review items:", [(ri["target"]["part_number"], ri["kinds"]) for ri in payload["review_items"]])
    print("case A (HTML parsed):", json.dumps(case_a_stats(), ensure_ascii=False))


if __name__ == "__main__":
    main()
