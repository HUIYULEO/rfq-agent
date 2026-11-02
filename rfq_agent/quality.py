"""Parse-quality findings: duplicates, missing fields, unit and date anomalies.

Required by §8.1.3 of the brief. These never change a price — they describe what the
document itself is inconsistent about, and each one points back to the rows it came from.
A column that is empty in *every* row is a property of the carrier (a PDF simply has no
priority column), not a defect; only partial coverage is reported as a problem.
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

    counts = Counter(line["part_number"] for line in lines)
    repeated = {part: times for part, times in counts.items() if times > 1}
    if repeated:
        extra = sum(times - 1 for times in repeated.values())
        worst = sorted(repeated.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
        findings.append({
            "kind": "duplicate_part", "severity": "warn", "title": "重复料号",
            "detail": f"{len(repeated)} 个料号重复出现，共 {extra} 行重复；按原样保留，不合并也不删除",
            "examples": [f"{part} ×{times}" for part, times in worst],
            "lines": _ids([line for line in lines if counts[line["part_number"]] > 1]),
            "count": extra,
        })

    units = Counter(line["unit"] for line in lines)
    if len(units) > 1:
        findings.append({
            "kind": "unit_mix", "severity": "warn", "title": "单位混用",
            "detail": "同一单据内出现多种计量单位：" + "、".join(
                f"{unit} {times} 行" for unit, times in units.most_common()),
            "examples": [], "lines": _ids([line for line in lines if line["unit"] != units.most_common(1)[0][0]]),
            "count": total - units.most_common(1)[0][1],
        })

    # The source column is 截止报价日期 / Due Date: it is the customer's
    # response deadline, not a delivery date or a supplier lead time.
    for field, label, title in (("priority", "优先级", "缺少优先级"),
                                ("due_at", "报价回复截止日期", "缺少报价回复截止日期")):
        finding = _counter_finding(f"missing_{field}", title, lines, field, label)
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
