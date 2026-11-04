"""Business-rule layer: per-line checks of *what* the engine decided.

``evaluation.validate`` is the program/contract self-check (did the provider answer
every line consistently?). This module is the other half: it re-checks each line
against the customer's own fields — part-number match type, certificate, unit,
condition and the quote deadline — and keeps every result, passes included, so
the analysis report shows what was checked and not only what failed. Each entry
carries a locator back to the source cell.
"""
from __future__ import annotations

import datetime as dt

from rfq_agent.domain.compliance import certificate_satisfies, is_pma, same_unit

# Status: pass / warn (a human must look) / fail (a priced line violates a hard rule
# without a recorded human decision — an engine defect) / not_applicable.

BUSINESS_RULES = ("pn_match", "certificate", "uom", "condition", "quote_due")

COLUMN_FOR_RULE = {"pn_match": "Part Number", "certificate": "询价产品要求",
                   "uom": "Unit Of Measure", "condition": "件状条件",
                   "quote_due": "截止报价日期"}


def locate(line, column: str | None = None) -> dict:
    """email -> attachment -> sheet/page -> row -> column -> raw value for one line."""
    evidence = (getattr(line, "evidence", None) if not isinstance(line, dict)
                else line.get("evidence")) or {}
    columns = evidence.get("columns") if isinstance(evidence.get("columns"), dict) else {}
    raw = evidence.get("raw") if isinstance(evidence.get("raw"), dict) else {}
    placed = columns.get(column) if column else None
    container = evidence.get("container") or ""
    row = evidence.get("row")
    if container == "attachment-pdf":
        position = f"PDF 文本第 {evidence.get('pdf_line') or '?'} 行"
        if column == "Quantity" or column == "Unit Of Measure":
            position = f"PDF 文本第 {evidence.get('qty_line') or evidence.get('pdf_line') or '?'} 行"
    else:
        position = f"第 {row} 行" if row else "行号未知"
    value = raw.get(column) if column else None
    if column == "Unit Of Measure" and evidence.get("unit_raw"):
        value = evidence.get("unit_raw")
    return {
        "file": evidence.get("file") or "",
        "attachment": evidence.get("attachment"),
        "container": container,
        "sheet": evidence.get("sheet"),
        "row": row,
        "pdf_line": evidence.get("pdf_line"),
        "column": column,
        "column_letter": placed.get("letter") if placed else None,
        "column_header": placed.get("header") if placed else None,
        "raw_value": value,
        "text": " / ".join(bit for bit in (
            evidence.get("file") or "",
            evidence.get("attachment") or "",
            evidence.get("sheet") or "",
            position,
            (f"{placed['letter']} 列「{placed['header']}」" if placed else (column or "")),
            (f"原值「{value}」" if value not in (None, "") else "原值为空"),
        ) if bit),
    }


def _chosen(row: dict) -> dict | None:
    decision = row.get("decision") or {}
    selected = decision.get("selected_source_id")
    for group in ("internal_candidates", "supplier_candidates"):
        for candidate in row.get(group) or []:
            if isinstance(candidate, dict) and candidate.get("source_id") == selected:
                return candidate
    return None


def _finding(line, rule: str, status: str, severity: str, detail: str,
             action: str = "") -> dict:
    return {"line_id": line.line_id, "part_number": line.part_number, "rule": rule,
            "status": status, "severity": severity, "detail": detail,
            "suggested_action": action, "locator": locate(line, COLUMN_FOR_RULE[rule])}


def check_business(rfq, results: list[dict]) -> tuple[list[dict], list[str]]:
    """One finding per (line, business rule), plus issues for hard-rule violations."""
    by_id = {row.get("line_id"): row for row in results if isinstance(row, dict)}
    findings: list[dict] = []
    issues: list[str] = []
    try:
        evaluated = dt.date.fromisoformat(rfq.evaluation_date)
    except (TypeError, ValueError):
        evaluated = None

    for line in rfq.lines:
        row = by_id.get(line.line_id)
        if row is None:
            continue          # completeness already reports the missing result
        decision = row.get("decision") or {}
        resolution = decision.get("resolution")
        priced = row.get("eligibility") == "priced"
        chosen = _chosen(row) if priced else None
        answers = decision.get("answers") or {}
        candidates = [c for g in ("internal_candidates", "supplier_candidates")
                      for c in (row.get(g) or []) if isinstance(c, dict)]

        # --- part number match type
        if priced and chosen:
            kind = chosen.get("match_type") or "exact"
            if kind == "exact":
                findings.append(_finding(line, "pn_match", "pass", "info", "件号精确匹配（exact）"))
            elif resolution == "confirm_match":
                findings.append(_finding(line, "pn_match", "warn", "advisory",
                                         f"件号匹配方式 {kind}，已人工确认"))
            else:
                findings.append(_finding(line, "pn_match", "fail", "blocking",
                                         f"件号匹配方式 {kind} 却未经人工确认就报价",
                                         "退回人工确认件号对应关系"))
        elif not candidates:
            findings.append(_finding(line, "pn_match", "warn", "advisory",
                                     "价格资源表和内部成本表均无此件号（含归一化、MPN、手册件号）",
                                     "向供应商询价"))
        else:
            loose = sorted({c.get("match_type") for c in candidates if c.get("match_type") != "exact"})
            findings.append(_finding(
                line, "pn_match", "warn" if loose else "pass", "advisory" if loose else "info",
                f"候选件号匹配方式含 {'/'.join(loose)}，需人工确认" if loose else "件号精确匹配（exact）",
                "确认件号对应关系" if loose else ""))

        # --- certificate
        required = list(line.required_certificates or [])
        cert = (chosen or {}).get("certificate")
        if priced and chosen:
            if required and not certificate_satisfies(cert, required):
                if resolution in ("accept_noncert", "confirm_stock_cert"):
                    findings.append(_finding(line, "certificate", "warn", "advisory",
                                             f"客户要求 {'/'.join(required)}，所选 {cert or '库存(未登记证书)'}，已人工接受"))
                else:
                    findings.append(_finding(line, "certificate", "fail", "blocking",
                                             f"客户要求 {'/'.join(required)}，所选 {cert or '(未登记)'} 不满足",
                                             "改选合规货源或要求供应商补证书"))
                    issues.append(f"certificate:{line.line_id}:fail")
            elif not cert:
                findings.append(_finding(line, "certificate", "warn", "advisory",
                                         "内部库存记录未登记证书类型，报价前需确认随附证书",
                                         "向仓库确认证书（EASA Form 1 / FAA 8130-3 / CoC）"))
            elif is_pma(cert):
                accepted = answers.get("pma_selected") == "accept_pma"
                findings.append(_finding(line, "certificate", "warn", "advisory" if accepted else "blocking",
                                         f"所选为 PMA 件（{cert}）" + ("，已人工接受" if accepted else "，待人工决定"),
                                         "" if accepted else "接受 PMA / 改用适航证书货源 / 要求补 8130-3"))
            else:
                findings.append(_finding(line, "certificate", "pass", "info",
                                         f"证书 {cert}" + (f" 满足客户要求 {'/'.join(required)}" if required else "")))
        elif required:
            ok = [c for c in candidates if c.get("source_type") == "supplier"
                  and certificate_satisfies(c.get("certificate"), required)]
            findings.append(_finding(line, "certificate", "warn", "advisory",
                                     f"客户要求 {'/'.join(required)}；满足该证书的候选 {len(ok)} 个，均因其他硬约束未入选"
                                     if ok else f"客户要求 {'/'.join(required)}；无满足该证书的候选",
                                     "要求供应商提供 FAA 8130-3 / EASA Form 1 报价"))
        elif line.product_requirement:
            findings.append(_finding(line, "certificate", "warn", "advisory",
                                     f"客户标注「{line.product_requirement}」", "向客户澄清要求内容"))
        else:
            findings.append(_finding(line, "certificate", "not_applicable", "info", "客户未提出证书要求"))

        # --- unit of measure (compared as written; never converted)
        if priced and chosen:
            unit = chosen.get("unit")
            if not unit:
                findings.append(_finding(line, "uom", "warn", "advisory",
                                         f"参考数据未记录单位，询价单位 {line.unit} 无法核对",
                                         "向供应商确认计量单位"))
            elif same_unit(line.unit, unit):
                findings.append(_finding(line, "uom", "pass", "info", f"询价单位 {line.unit} = 参考单位 {unit}"))
            elif resolution == "confirm_uom":
                findings.append(_finding(line, "uom", "warn", "advisory",
                                         f"询价单位 {line.unit} ≠ 参考单位 {unit}，已人工确认（未换算）"))
            else:
                findings.append(_finding(line, "uom", "fail", "blocking",
                                         f"询价单位 {line.unit} ≠ 参考单位 {unit}，却已报价",
                                         "退回人工确认单位"))
                issues.append(f"uom:{line.line_id}:fail")
        else:
            mismatched = [c for c in candidates if any(
                r.get("rule") == "uom_mismatch" for r in c.get("rejection_reasons") or [])]
            findings.append(_finding(
                line, "uom", "warn" if mismatched else "not_applicable",
                "advisory" if mismatched else "info",
                f"{len(mismatched)} 个候选单位与询价单位 {line.unit} 不一致" if mismatched
                else "无报价，单位未核对", "与客户/供应商确认单位" if mismatched else ""))

        # --- condition
        accept = list(line.condition_accept or [])
        if priced and chosen:
            condition = chosen.get("condition")
            if accept and condition in accept:
                findings.append(_finding(line, "condition", "pass", "info", f"件状 {condition} ∈ {accept}"))
            elif not accept:
                findings.append(_finding(line, "condition", "warn", "advisory",
                                         f"客户未声明件状，按 {condition or '(空)'} 报价",
                                         "报价单注明件状并请客户确认"))
            elif resolution == "accept_alternate_condition":
                findings.append(_finding(line, "condition", "warn", "advisory",
                                         f"件状 {condition} ∉ {accept}，已人工接受替代件状"))
            else:
                findings.append(_finding(line, "condition", "fail", "blocking",
                                         f"件状 {condition} ∉ {accept}，却已报价", "退回人工确认件状"))
                issues.append(f"condition:{line.line_id}:fail")
        else:
            findings.append(_finding(line, "condition", "not_applicable", "info",
                                     f"需求件状 {'/'.join(accept) or '未声明'}，无报价"))

        # --- quote-response deadline (截止报价日期 is the customer's reply deadline)
        due = (line.due_at or "")[:10]
        if not due or evaluated is None:
            findings.append(_finding(line, "quote_due", "not_applicable", "info", "未提供报价截止日期"))
        else:
            try:
                due_date = dt.date.fromisoformat(due)
            except ValueError:
                findings.append(_finding(line, "quote_due", "warn", "advisory",
                                         f"报价截止日期「{line.due_at}」无法解析", "向客户确认截止日期"))
            else:
                if due_date < evaluated:
                    findings.append(_finding(line, "quote_due", "warn", "advisory",
                                             f"报价截止 {due} 早于评估日 {evaluated}",
                                             "先与客户确认是否仍接受报价"))
                else:
                    findings.append(_finding(line, "quote_due", "pass", "info",
                                             f"报价截止 {due}，评估日 {evaluated}"))
    for item in findings:
        if item["status"] == "fail" and item["rule"] == "pn_match":
            issues.append(f"pn_match:{item['line_id']}:fail")
    return findings, sorted(set(issues))
