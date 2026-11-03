"""Inquiry analysis report: produced for every task, quotable or not.

A supplier inquiry or a no-bid outcome is still work product: the buyer needs to see
which lines have no price, why, what to do next, and exactly where in the mail each
problem sits. This module turns a task into that structure (for the web panel) and
into ``analysis-r{rev}.docx``. It never prices anything and is never titled as a
quotation.
"""
from __future__ import annotations

from pathlib import Path

from .deps import require
from .pricing import ANSWER_LABELS, line_answers, summarize

TITLE = "询价分析报告"

STATUS_LABELS = {"priced": "已报价", "pending": "待确认", "inquiry": "转询价", "no_bid": "不报价"}

ACTIONS = {
    "no_price_record": "向供应商询价（参考表无此件号记录）",
    "stock_insufficient": "向供应商询价补足数量",
    "moq_exceeds": "与供应商协商 MOQ，或经确认后按 MOQ 报价",
    "condition_mismatch": "询问客户能否接受替代件状，或向供应商询价所需件状",
    "condition_unknown": "向客户确认可接受件状",
    "cert_mismatch": "要求供应商提供 FAA 8130-3 / EASA Form 1",
    "cert_unverified": "向仓库确认库存件随附证书",
    "uom_mismatch": "与客户确认计量单位（系统不做换算）",
    "pn_match": "确认件号对应关系（非精确匹配）",
    "date_expired": "向供应商更新过期报价",
    "status_invalid": "向供应商确认价格状态",
    "price_missing": "向供应商索取有效单价",
    "fx_missing": "补充币种汇率",
    "cost_missing": "补录库存标准成本",
    "special_requirement": "向客户澄清 SPECIAL REQUIREMENT 的具体内容",
    "human_excluded": "按人工决定处理",
    "pma_excluded": "按人工决定改用适航证书货源",
}

CHECK_STATUSES = ("pass", "warn", "fail", "not_applicable")
CHECK_STATUS_LABELS = {"pass": "通过", "warn": "需关注", "fail": "违规", "not_applicable": "不适用"}

RULE_LABELS = {"pn_match": "件号匹配", "certificate": "证书/适航", "uom": "计量单位",
               "condition": "件状", "quote_due": "报价截止"}


def _line_status(row: dict, open_items: list[dict], resolutions: dict) -> str:
    line_id = row["line_id"]
    if any(item["target"].get("line_id") == line_id or item["scope"] == "task"
           for item in open_items):
        return "pending"
    if row.get("eligibility") == "priced":
        return "priced"
    choices = [resolutions.get(line_id), resolutions.get("__task__"),
               *line_answers(resolutions, line_id).values()]
    if any(choice in ("skip_line", "reject_task") for choice in choices):
        return "no_bid"
    return "inquiry"


def build(task) -> dict:
    """Structured analysis for one task revision (dict or Task)."""
    data = task.to_dict() if hasattr(task, "to_dict") else task
    rfq = data["rfq"]
    results = data.get("results") or []
    review_items = data.get("review_items") or []
    resolutions = data.get("resolutions") or {}
    findings = data.get("business_checks") or []
    open_items = [item for item in review_items
                  if item.get("severity") == "blocking" and item.get("status") == "open"]
    lines_by_id = {line["line_id"]: line for line in rfq.get("lines", [])}

    from .business_rules import locate

    rows = []
    for row in results:
        line = lines_by_id.get(row["line_id"], {})
        decision = row.get("decision") or {}
        status = _line_status(row, open_items, resolutions)
        reasons = list(decision.get("review_evidence") or [])
        reasons += [{"rule": flag["kind"], "detail": flag["detail"]}
                    for flag in decision.get("flags") or []]
        line_findings = [item for item in findings if item["line_id"] == row["line_id"]
                         and item["status"] in ("warn", "fail")]
        own_items = [item for item in open_items if item["target"].get("line_id") == row["line_id"]]
        if own_items:
            action = "人工决定：" + "；".join(item["question"] for item in own_items)
        elif status == "priced":
            extra = [item["suggested_action"] for item in line_findings if item.get("suggested_action")]
            action = "可进入报价" + (f"；报价前：{'；'.join(dict.fromkeys(extra))}" if extra else "")
        elif status == "no_bid":
            action = "不报此行，回复客户说明"
        else:
            rules = [reason["rule"] for reason in reasons]
            action = "；".join(dict.fromkeys(ACTIONS.get(rule, "转人工处理") for rule in rules)) \
                or "向供应商询价"
        rows.append({
            "line_id": row["line_id"],
            "line_number": row.get("line_number"),
            "part_number": row["part_number"],
            "description": row.get("description") or "",
            "quantity": row.get("quantity"),
            "unit": row.get("unit"),
            "status": status,
            "status_label": STATUS_LABELS[status],
            "unit_price": row.get("unit_price"),
            "line_total": row.get("line_total"),
            "source": decision.get("recommended_source"),
            "selected_source_id": decision.get("selected_source_id"),
            "certificate": (decision.get("basis") or {}).get("certificate"),
            "requirement": line.get("product_requirement"),
            "reasons": reasons,
            "findings": [{"rule": item["rule"], "status": item["status"],
                          "detail": item["detail"]} for item in line_findings],
            # Every business check on this line, passes included: what was verified.
            "checks": [{"rule": item["rule"], "status": item["status"], "detail": item["detail"]}
                       for item in findings if item["line_id"] == row["line_id"]],
            "suggested_action": action,
            "locator": locate(line, "Part Number") if line else None,
        })

    counts = {key: sum(1 for row in rows if row["status"] == key) for key in STATUS_LABELS}
    by_rule = {rule: sum(1 for item in findings if item["rule"] == rule
                         and item["status"] in ("warn", "fail")) for rule in RULE_LABELS}
    money = summarize(results) if results else {"priced_subtotal_cny": "0.00", "priced_lines": 0}
    return {
        "title": TITLE,
        "task_id": data["task_id"],
        "revision": data["revision"],
        "rfq_number": rfq.get("rfq_number", ""),
        "customer": rfq.get("customer", ""),
        "subject": rfq.get("subject", ""),
        "evaluation_date": rfq.get("evaluation_date", ""),
        "source_files": rfq.get("source_files", []),
        "commercial_disposition": data.get("commercial_disposition", "PENDING"),
        "evaluation_status": data.get("evaluation_status", "pending"),
        "policy_version": data.get("policy_version", ""),
        "data_snapshot_id": data.get("data_snapshot_id", ""),
        "summary": {
            "lines": len(rows),
            **counts,
            "open_reviews": len(open_items),
            "answered_reviews": sum(1 for item in review_items if item.get("status") == "answered"),
            "priced_subtotal_cny": money["priced_subtotal_cny"],
            "findings_warn": sum(1 for item in findings if item["status"] == "warn"),
            "findings_fail": sum(1 for item in findings if item["status"] == "fail"),
            "by_rule": by_rule,
            "checks": {rule: {status: sum(1 for item in findings if item["rule"] == rule
                                          and item["status"] == status)
                              for status in CHECK_STATUSES}
                       for rule in RULE_LABELS},
        },
        "open_questions": [{
            "id": item["id"], "question": item["question"], "kinds": item.get("kinds", []),
            "options": [option["label"] for option in item.get("options", [])],
            "locator": item.get("locator"),
        } for item in open_items],
        "decisions": [{
            "id": item["id"], "answer": item.get("answer"),
            "label": ANSWER_LABELS.get(item.get("answer"), item.get("answer")),
        } for item in review_items if item.get("status") == "answered"],
        "lines": rows,
        "findings": [item for item in findings if item["status"] in ("warn", "fail")],
        "evaluation_issues": data.get("evaluation_issues", []),
    }


def _cell(cell, value) -> None:
    cell.text = "" if value is None else str(value)


def render_docx(analysis: dict, output: Path) -> Path:
    """analysis-r{rev}.docx: readable by a buyer, never titled or formatted as a quote."""
    docx = require("docx", "rendering the analysis report")
    document = docx.Document()
    document.add_heading(TITLE, level=0)
    summary = analysis["summary"]
    document.add_paragraph(
        f"询价号：{analysis['rfq_number']}　客户：{analysis['customer'] or '（未提供）'}　"
        f"评估日：{analysis['evaluation_date']}　修订：r{analysis['revision']}")
    document.add_paragraph(
        f"来源：{'、'.join(analysis['source_files'])}　策略：{analysis['policy_version']}　"
        f"数据快照：{analysis['data_snapshot_id'] or '—'}")
    document.add_paragraph(
        "本文件是询价分析记录，用于定位问题和安排下一步动作，不构成对外报价，不得对外发送。")

    document.add_heading("一、结论摘要", level=1)
    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in (
            ("商业处置", analysis["commercial_disposition"]),
            ("评估状态", analysis["evaluation_status"]),
            ("总行数", summary["lines"]),
            ("已报价", summary["priced"]),
            ("待人工确认", summary["pending"]),
            ("转供应商询价", summary["inquiry"]),
            ("不报价", summary["no_bid"]),
            ("已报价小计（CNY，未税）", summary["priced_subtotal_cny"]),
            ("规则核查：需关注 / 违规", f"{summary['findings_warn']} / {summary['findings_fail']}"),
            *((f"规则核查 · {RULE_LABELS[rule]}", count) for rule, count in summary["by_rule"].items())):
        cells = table.add_row().cells
        _cell(cells[0], label)
        _cell(cells[1], value)

    if analysis["open_questions"]:
        document.add_heading("二、待人工决定", level=1)
        for item in analysis["open_questions"]:
            document.add_paragraph(f"{item['id']}：{item['question']}", style="List Bullet")
            document.add_paragraph(f"可选：{' / '.join(item['options'])}")
            if item.get("locator"):
                document.add_paragraph(f"定位：{item['locator']['text']}")
    if analysis["decisions"]:
        document.add_heading("已记录的人工决定", level=2)
        for item in analysis["decisions"]:
            document.add_paragraph(f"{item['id']} → {item['label']}", style="List Bullet")

    document.add_heading("三、逐行状态与建议动作", level=1)
    headers = ("行", "料号", "数量", "状态", "原因", "建议动作", "定位")
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for position, title in enumerate(headers):
        _cell(table.rows[0].cells[position], title)
    for row in analysis["lines"]:
        cells = table.add_row().cells
        reasons = "；".join(f"[{r['rule']}] {r['detail']}" for r in row["reasons"]) or "—"
        values = (row["line_number"], row["part_number"], f"{row['quantity']} {row['unit']}",
                  row["status_label"] + (f" ¥{row['line_total']}" if row["line_total"] else ""),
                  reasons, row["suggested_action"],
                  (row["locator"] or {}).get("text", ""))
        for position, value in enumerate(values):
            _cell(cells[position], value)

    document.add_heading("四、规则核查覆盖（每行每条规则都核查，含通过项）", level=1)
    table = document.add_table(rows=1, cols=1 + len(CHECK_STATUSES))
    table.style = "Table Grid"
    for position, title in enumerate(("规则", *(CHECK_STATUS_LABELS[s] for s in CHECK_STATUSES))):
        _cell(table.rows[0].cells[position], title)
    for rule, counts in summary["checks"].items():
        cells = table.add_row().cells
        _cell(cells[0], RULE_LABELS[rule])
        for position, status in enumerate(CHECK_STATUSES, start=1):
            _cell(cells[position], counts[status])

    if analysis["findings"]:
        document.add_heading("五、规则核查发现（需关注 / 违规）", level=1)
        headers = ("行", "规则", "结果", "说明", "建议", "定位")
        table = document.add_table(rows=1, cols=len(headers))
        table.style = "Table Grid"
        for position, title in enumerate(headers):
            _cell(table.rows[0].cells[position], title)
        for item in analysis["findings"]:
            cells = table.add_row().cells
            values = (item["line_id"], RULE_LABELS.get(item["rule"], item["rule"]),
                      "违规" if item["status"] == "fail" else "需关注", item["detail"],
                      item.get("suggested_action") or "—", item["locator"]["text"])
            for position, value in enumerate(values):
                _cell(cells[position], value)
    if analysis["evaluation_issues"]:
        document.add_heading("评估完整性问题", level=2)
        for issue in analysis["evaluation_issues"]:
            document.add_paragraph(issue, style="List Bullet")

    output.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output))
    return output
