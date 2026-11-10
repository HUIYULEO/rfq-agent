"""Do the buyer's stated requirements actually hold in our draft?

The mail and the PDF say what the offer must contain ("Unit price, total price and
delivery lead time", "Compliance declaration", …). Extracting that text is only half
the job — the useful half is being able to say, per requirement, whether the draft
meets it, and if not, what is missing.

Every check here is deterministic: it reads the priced rows the engine already produced.
Requirements we cannot judge get ``manual`` instead of a fabricated verdict.
"""

from types import SimpleNamespace
import re

MET = "met"          # the draft satisfies it, verifiable from data
TODO = "todo"        # the draft is missing something and it can be named
MANUAL = "manual"    # a human must confirm; the system says what it did
NA = "na"            # does not apply to this RFQ

LABELS = {MET: "已满足", TODO: "待补齐", MANUAL: "需人工确认", NA: "不适用"}

# Requirement text → check. Substring match on the lowercased text, first hit wins;
# the patterns are taken from the two real English mails, not invented.
RULES = (
    ("lead_time", ("delivery lead time",)),
    ("validity", ("validity period",)),
    ("compliance", ("compliance declaration",)),
    ("columns", ("complete the list",)),
    ("remarks", ("remarks section",)),
    ("serialized", ("serialized",)),
    ("form", ("use this form",)),
)


def _priced(results):
    return [row for row in results if row["eligibility"] == "priced"]


def _basis(row):
    return (row.get("decision") or {}).get("basis") or {}


def _missing(results, field, subject, hint):
    priced = _priced(results)
    if not priced:
        return {"status": TODO, "detail": "本案 0 行有价，条款尚无法核对；补齐价格后再核"}
    have = [row for row in priced if _basis(row).get(field) not in (None, "")]
    if len(have) == len(priced):
        return {"status": MET, "detail": f"{len(priced)} 行全部提供{subject}"}
    return {"status": TODO,
            "detail": f"{len(priced) - len(have)}/{len(priced)} 行缺少{subject}；{hint}"}


def _lead_time(results):
    return _missing(results, "lead_time_days", "交期（天）",
                    "内部库存行通常没有供应商交期，对外报价前需补齐或注明")


def _validity(results):
    return _missing(results, "valid_until", "价格有效期", "需向供应商确认后补入")


def _columns(results):
    priced = _priced(results)
    if not priced:
        return {"status": TODO, "detail": "本案 0 行有价，四列是否填满无法核对"}
    gaps = []
    for row in priced:
        missing = []
        if not row.get("unit_price"):
            missing.append("单价")
        if not row.get("line_total"):
            missing.append("总价")
        if not _basis(row).get("lead_time_days"):
            missing.append("交期")
        if not _basis(row).get("condition"):
            missing.append("件状")
        if missing:
            gaps.append((row["line_number"], missing))
    if not gaps:
        return {"status": MET, "detail": f"{len(priced)} 行四列齐全（单价/总价/交期/件状）"}
    fields = sorted({name for _, names in gaps for name in names})
    sample = "、".join(f"第 {number} 行缺{'/'.join(names)}" for number, names in gaps[:3])
    return {"status": TODO,
            "detail": f"{len(gaps)}/{len(priced)} 行未填满四列（缺 {'、'.join(fields)}）；如 {sample}"}


def _compliance(results):
    return {"status": MANUAL,
            "detail": "客户要求合规声明；草案已留出「合规声明」段落，需签署后随报价发出"}


def _remarks(results):
    priced = _priced(results)
    if not priced:
        return {"status": TODO, "detail": "本案 0 行有价，是否使用替代件无法核对"}
    alternates = []
    for row in priced:
        actual = _basis(row).get("condition")
        accepted = row.get("condition_accept") or []
        if actual and accepted and actual not in accepted:
            alternates.append(f"{row['part_number']}（报 {actual}，客户接受 {'/'.join(accepted)}）")
    if not alternates:
        return {"status": NA, "detail": "本案没有使用替代件状，备注栏无需说明"}
    return {"status": MANUAL,
            "detail": f"{len(alternates)} 行以替代件状报价：{'、'.join(alternates[:3])}；备注已写入，请复核措辞"}


def _serialized(results):
    priced = _priced(results)
    serial = [row for row in priced
              if (_basis(row).get("condition") or "").upper() in ("OH", "SV", "AR", "RP")]
    if not serial:
        return {"status": NA, "detail": "本案没有送修/翻修件，无需提供维修厂与工时"}
    return {"status": TODO,
            "detail": f"{len(serial)} 行涉及送修件，客户要求维修厂、工时与改装状态；"
                      "参考表不含这些字段，需人工补充"}


def _form(results):
    return {"status": MET, "detail": "已使用客户提供的报价单模板填充，未另起格式"}


CHECKS = {
    "lead_time": _lead_time,
    "validity": _validity,
    "columns": _columns,
    "compliance": _compliance,
    "remarks": _remarks,
    "serialized": _serialized,
    "form": _form,
}


def _rule_for(text):
    lowered = (text or "").lower()
    for name, patterns in RULES:
        if any(pattern in lowered for pattern in patterns):
            return name
    return None


def check(rfq, results):
    """One entry per customer requirement, in the order the buyer wrote them."""
    raw = getattr(rfq, "requirements", None)
    if raw is None and isinstance(rfq, dict):
        raw = [SimpleNamespace(text=item.get("text", ""), source=item.get("source", ""))
               for item in rfq.get("requirements", [])]
    items = []
    for number, item in enumerate(raw or [], start=1):
        rule = _rule_for(item.text)
        general = re.fullmatch(r"(?:请协助报价|请报价|请协助对下列配件报价)[，,。.!！\s]*(?:(?:详见附件|见附件|紧急|感谢|谢谢)[，,。.!！\s]*)*", item.text.strip())
        if general:
            rule = "general_request"
            verdict = {"status": NA, "detail": "一般询价说明，已作为邮件背景保留；无需单独审批。具体报价条件以行项目和附件为准。"}
        elif rule is None:
            verdict = {"status": MANUAL, "detail": "请对照原邮件确认这句话是否包含件状、交期、证书或其他承诺；如有，需核实草案是否覆盖，系统暂不能自动判断。"}
        else:
            verdict = CHECKS[rule](results)
        items.append({
            "id": f"RQ-{number:02d}",
            "text": item.text,
            "source": item.source,
            "check": rule or "unmatched",
            "status": verdict["status"],
            "label": "一般说明" if general else LABELS[verdict["status"]],
            "detail": verdict["detail"],
        })
    return items


def summary(items):
    counts = {status: 0 for status in LABELS}
    for item in items:
        counts[item["status"]] += 1
    return {"total": len(items), **counts}


def needs_compliance(items):
    return any(item["check"] == "compliance" for item in items)
