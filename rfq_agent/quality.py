"""Source-grounded observations and review findings for an RFQ.

Repeated parts and multiple units are normal across independent inquiry lines. Only
conflicts within the same inquiry number and part are review warnings. An unspecified
priority stays unspecified; this module never invents a default.
"""

from collections import Counter
from datetime import date

MAX_LINES = 60


def _ids(lines: list[dict]) -> list[str]:
    return [line["line_id"] for line in lines][:MAX_LINES]


def _counter_finding(kind: str, title: str, lines: list[dict], field: str,
                     label: str) -> dict | None:
    total = len(lines)
    missing = [line for line in lines if not line.get(field)]
    if not missing:
        return None
    if len(missing) == total:
        return {"kind": kind, "severity": "info", "title": title,
                "detail": f"该载体不含{label}，{total} 行全部为空 — 按“未声明”处理，不推断",
                "examples": [], "lines": _ids(missing), "count": total}
    return {"kind": kind, "severity": "warn", "title": title,
            "detail": f"{len(missing)}/{total} 行没有{label}（其余行有，属同单内不一致）",
            "examples": [], "lines": _ids(missing), "count": len(missing)}


def inspect(rfq) -> list[dict]:
    """Findings for one RFQ, ordered by how much they should worry a reviewer."""
    data = rfq.to_dict() if hasattr(rfq, "to_dict") else rfq
    lines = data.get("lines") or []
    if not lines:
        return []
    total = len(lines)
    findings: list[dict] = []

    groups: dict[tuple[str, str], list[dict]] = {}
    by_part: dict[str, list[dict]] = {}
    for line in lines:
        part = line["part_number"]
        inquiry = str(line.get("inquiry_no") or "").strip()
        groups.setdefault((inquiry, part), []).append(line)
        by_part.setdefault(part, []).append(line)

    duplicate_groups = {key: group for key, group in groups.items() if len(group) > 1}
    if duplicate_groups:
        extra = sum(len(group) - 1 for group in duplicate_groups.values())
        examples = sorted(duplicate_groups.items(), key=lambda pair: (-len(pair[1]), pair[0]))[:3]
        findings.append({
            "kind": "duplicate_part", "severity": "warn", "title": "同询价号重复料号",
            "detail": f"{len(duplicate_groups)} 组相同询价号和料号重复，共 {extra} 行重复；保留原行，核对是否为重复请求",
            "examples": [f"{part} ×{len(group)}（询价号 {inquiry or '未声明'}）"
                         for (inquiry, part), group in examples],
            "lines": _ids([line for group in duplicate_groups.values() for line in group]),
            "count": extra,
        })

    across = {part: group for part, group in by_part.items()
              if len({str(line.get("inquiry_no") or "").strip() for line in group} - {""}) > 1}
    if across:
        findings.append({
            "kind": "repeated_part_across_inquiries", "severity": "info",
            "title": "不同询价号同料号",
            "detail": f"{len(across)} 个料号出现在不同询价单号中；它们是独立询价行，不按重复请求处理",
            "examples": [f"{part} ×{len(group)}" for part, group in list(across.items())[:3]],
            "lines": _ids([line for part in across for line in across[part]]),
            "count": len(across),
        })

    units = Counter(line["unit"] for line in lines)
    if len(units) > 1:
        findings.append({
            "kind": "unit_mix", "severity": "info", "title": "多种计量单位",
            "detail": "不同询价行的计量单位：" + "、".join(
                f"{unit} {times} 行" for unit, times in units.most_common()) + "；按行保留，不据此判错",
            "examples": [], "lines": _ids([line for line in lines if line["unit"] != units.most_common(1)[0][0]]),
            "count": total - units.most_common(1)[0][1],
        })

    unit_conflicts = {key: group for key, group in groups.items()
                      if len({line["unit"] for line in group}) > 1}
    if unit_conflicts:
        findings.append({
            "kind": "unit_conflict", "severity": "warn", "title": "同询价号同料号单位不一致",
            "detail": f"{len(unit_conflicts)} 组相同询价号和料号使用不同单位，需核对数量口径",
            "examples": [f"{part}（{inquiry or '询价号未声明'}）："
                         + "/".join(sorted({line["unit"] for line in group}))
                         for (inquiry, part), group in list(unit_conflicts.items())[:3]],
            "lines": _ids([line for group in unit_conflicts.values() for line in group]),
            "count": len(unit_conflicts),
        })

    # The source column is 截止报价日期 / Due Date: it is the customer's
    # response deadline, not a delivery date or a supplier lead time.
    unspecified_priority = [line for line in lines if not line.get("priority")]
    if unspecified_priority:
        findings.append({
            "kind": "missing_priority", "severity": "info", "title": "优先级未声明",
            "detail": f"{len(unspecified_priority)}/{total} 行未填写优先级；保留空值，当前没有默认优先级规则",
            "examples": [], "lines": _ids(unspecified_priority), "count": len(unspecified_priority),
        })
    finding = _counter_finding("missing_due_at", "缺少报价回复截止日期", lines,
                               "due_at", "报价回复截止日期")
    if finding:
        findings.append(finding)

    evaluated = data.get("evaluation_date") or ""
    late = []
    for line in lines:
        due = (line.get("due_at") or "")[:10]
        if not due or not evaluated:
            continue
        try:
            if date.fromisoformat(due) < date.fromisoformat(evaluated):
                late.append(line)
        except ValueError:
            late.append(line)          # unparseable is a finding too, not a silent skip
    if late:
        findings.append({
            "kind": "date_anomaly", "severity": "warn", "title": "报价回复截止日期异常",
            "detail": f"{len(late)} 行报价回复截止日期早于评估日 {evaluated} 或无法解析",
            "examples": [f"{line['line_id']} → {line.get('due_at')}" for line in late[:3]],
            "lines": _ids(late), "count": len(late),
        })

    if not any(line.get("condition_accept") for line in lines):
        findings.append({
            "kind": "missing_condition", "severity": "info", "title": "未声明件状要求",
            "detail": f"{total} 行都没有件状要求 — 保留为未声明，候选件状需明确展示，是否接受由客户或企业政策确认",
            "examples": [], "lines": [], "count": total,
        })

    return findings


def summary(findings: list[dict]) -> dict:
    return {"warn": sum(1 for item in findings if item["severity"] == "warn"),
            "info": sum(1 for item in findings if item["severity"] == "info"),
            "kinds": [item["kind"] for item in findings]}
