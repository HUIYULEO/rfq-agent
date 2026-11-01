"""Render a quote draft onto the official template.

Rules:
* never invent a field the data does not have — write "未提供" instead;
* keep the official 9-column layout, and put everything it cannot hold
  (condition / certificate / lead time / MOQ / validity) on a summary page;
* every artifact is a DRAFT and says so on its face.
"""
from __future__ import annotations

import datetime as dt
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from .deps import require
from .policy import money, q2

DIGITS = "零壹贰叁肆伍陆柒捌玖"
UNITS = ("", "拾", "佰", "仟")
GROUPS = ("", "万", "亿", "万亿")


def _section(value: int) -> str:
    text = ""
    zero = False
    position = 0
    while value > 0:
        digit = value % 10
        if digit == 0:
            if text and not zero:
                text = "零" + text
                zero = True
        else:
            zero = False
            text = DIGITS[digit] + UNITS[position] + text
        value //= 10
        position += 1
    return text


def rmb_upper(amount) -> str:
    """Chinese capital amount, e.g. 386595.80 -> 叁拾捌万陆仟伍佰玖拾伍元捌角整."""
    amount = q2(amount)
    if amount == 0:
        return "零元整"
    integer = int(amount)
    jiao = int(amount * 10) % 10
    fen = int(amount * 100) % 10

    parts: list[str] = []
    groups: list[int] = []
    remaining = integer
    while remaining > 0:
        groups.append(remaining % 10000)
        remaining //= 10000

    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if group == 0:
            if parts and parts[-1] != "零":
                parts.append("零")
            continue
        chunk = _section(group)
        if parts and group < 1000 and parts[-1] != "零":
            parts.append("零")
        parts.append(chunk + GROUPS[index])
    text = "".join(parts).rstrip("零")

    result = (text + "元") if integer else ""
    if jiao == 0 and fen == 0:
        return result + "整"
    if jiao:
        result += DIGITS[jiao] + "角"
    elif fen:
        result += "零"
    if fen:
        result += DIGITS[fen] + "分"
    else:
        result += "整"
    return result


def _set_paragraph(paragraph, text: str):
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def _replace(paragraph, pattern: str, replacement: str) -> bool:
    import re
    original = paragraph.text
    updated = re.sub(pattern, replacement, original, count=1)
    if updated == original:
        return False
    _set_paragraph(paragraph, updated)
    return True


def _set_cell(cell, value):
    text = "" if value is None else str(value)
    paragraph = cell.paragraphs[0]
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def _heading(document, text: str):
    try:
        return document.add_heading(text, level=1)
    except KeyError:  # template lacks the style
        paragraph = document.add_paragraph()
        run = paragraph.add_run(text)
        run.bold = True
        return paragraph


def _basis_text(row: dict) -> str:
    decision = row["decision"]
    basis = decision.get("basis") or {}
    if row["eligibility"] != "priced" or not basis:
        return "转人工"
    return "内部库存/标准成本" if basis.get("source_type") == "internal" else "供应商报价/统一成本"


def _remark(row: dict) -> str:
    decision = row["decision"]
    if row["eligibility"] != "priced":
        rules = sorted({item["rule"] for item in decision.get("review_evidence", [])})
        return "待确认，暂不报价（" + "/".join(rules) + "）"
    bits = []
    basis = decision.get("basis") or {}
    if basis.get("condition"):
        bits.append(f"件状{basis['condition']}")
    if basis.get("certificate"):
        bits.append(f"证书{basis['certificate']}")
    if basis.get("lead_time_days"):
        bits.append(f"交期{basis['lead_time_days']}天")
    if row.get("quantity_basis") == "moq":
        bits.append(f"按MOQ {row['priced_quantity']}计")
    for warning in decision.get("warnings", []):
        if "数量待确认" in warning:
            bits.append("数量待确认")
        elif "替代件状" in warning:
            bits.append("已接受替代件状")
    return "；".join(bits)


def render_draft(*, template: Path, output: Path, rfq, results: list[dict], summary: dict,
                 policy_version: str, evaluation_date: str) -> Path:
    docx = require("docx", "rendering the .docx quote draft")
    document = docx.Document(str(template))

    expiry = (dt.date.fromisoformat(evaluation_date) + dt.timedelta(days=30)).isoformat()

    for paragraph in document.paragraphs:
        text = paragraph.text
        if text.startswith("需方："):
            _replace(paragraph, r"需方： +", f"需方：{rfq.customer or '（客户名称）'}")
        elif text.startswith("供方："):
            _replace(paragraph, r"供方： +", "供方：（我方主体信息，盖章处）")
        elif text.startswith("项目名称："):
            _replace(paragraph, r"项目名称： +", f"项目名称：{rfq.rfq_number}")
        elif text.startswith("报价日期："):
            year, month, day = evaluation_date.split("-")
            _replace(paragraph, r"报价日期：.*", f"报价日期：{year} 年 {month} 月 {day} 日")
        elif text.startswith("报价有效期："):
            _replace(paragraph, r"报价有效期：.*", f"报价有效期：自报价日起 30 天（至 {expiry}）")

    table = document.tables[0]
    detail_rows = len(table.rows) - 4  # 1 header + 3 totals rows
    rows_needed = len(results)
    if rows_needed > detail_rows:
        last = table.rows[detail_rows]._tr
        for _ in range(rows_needed - detail_rows):
            clone = deepcopy(last)
            last.addnext(clone)
            last = clone
    elif rows_needed < detail_rows:
        for _ in range(detail_rows - rows_needed):
            element = table.rows[rows_needed + 1]._tr
            element.getparent().remove(element)

    for index, row in enumerate(results):
        cells = table.rows[index + 1].cells
        priced = row["eligibility"] == "priced"
        quantity = row.get("priced_quantity") or row["quantity"]
        _set_cell(cells[0], str(index + 1))
        _set_cell(cells[1], row.get("description") or row["part_number"])
        _set_cell(cells[2], "")
        _set_cell(cells[3], row["part_number"])
        _set_cell(cells[4], quantity)
        _set_cell(cells[5], row["unit"])
        _set_cell(cells[6], money(row["unit_price"]) if priced else "")
        _set_cell(cells[7], money(row["line_total"]) if priced else "")
        _set_cell(cells[8], _remark(row))

    total = Decimal(summary["priced_subtotal_cny"])
    totals_row = len(results) + 1
    _set_cell(table.rows[totals_row].cells[2], rmb_upper(total))
    _set_cell(table.rows[totals_row].cells[7], f"￥{total:,.2f} 元")
    _set_cell(table.rows[totals_row + 1].cells[1], "随行交期，详见第 2 页条件摘要")
    _set_cell(table.rows[totals_row + 1].cells[6], "按供应商标准质保，详见条件摘要")
    _set_cell(table.rows[totals_row + 2].cells[1],
              "1) 本报价为待复核草案，须经人工确认后方可对外发送；"
              "2) 税费、运费、包装费未计入；"
              "3) 本报价不预留库存，以正式订单确认为准；"
              f"4) 价格基准日 {evaluation_date}，策略版本 {policy_version}。")

    # ---- summary page ----
    document.add_paragraph().add_run().add_break(docx.enum.text.WD_BREAK.PAGE)
    _heading(document, "报价依据与条件摘要（草案）")
    note = document.add_paragraph(
        f"报价单号：{rfq.rfq_number}　客户：{rfq.customer or '（未提供）'}　"
        f"行数：{summary['lines']}（已报价 {summary['priced_lines']}，"
        f"转人工 {summary['review_lines']}）　币种：{rfq.currency}")
    note.runs[0].italic = True

    headers = ["序号", "料号", "件状", "证书", "交期(天)", "MOQ", "发货国", "价格有效期", "报价依据"]
    detail = document.add_table(rows=1, cols=len(headers))
    detail.style = "Table Grid"
    for position, title in enumerate(headers):
        _set_cell(detail.rows[0].cells[position], title)
    for index, row in enumerate(results):
        basis = row["decision"].get("basis") or {}
        cells = detail.add_row().cells
        values = [
            str(index + 1),
            row["part_number"],
            basis.get("condition") or "未提供",
            basis.get("certificate") or "未提供",
            (str(basis.get("lead_time_days")) if basis.get("lead_time_days") else "—"),
            (str(basis.get("moq")) if basis.get("moq") is not None else "—"),
            basis.get("ships_from") or "未提供",
            basis.get("valid_until") or "—",
            _basis_text(row),
        ]
        for position, value in enumerate(values):
            _set_cell(cells[position], value)

    document.add_paragraph(
        "说明：1) 追溯信息与可售数量在供应商报价中未提供，需下单前书面确认；"
        "2) “未提供”不等于“不满足”，但未确认前不得对外承诺；"
        "3) 件状按客户可接受集合判定，替代件状需客户书面同意。")

    output.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output))
    return output