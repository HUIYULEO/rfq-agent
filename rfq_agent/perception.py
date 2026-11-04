"""Perception layer: let the model understand the mail, then check it against the source.

Why this exists
---------------
``ingest.py`` recognises one container per mail and maps headers through a fixed alias
table; the model was only ever asked to explain numbers it had already been given. That
is backwards for RFQ work: the hard part is *reading* a customer's mail, not doing the
sums. See ``refer_doc/RFQ_ARCHITECTURE_ASSESSMENT_2026-11-03.md`` §7.2–7.3.

Contract
--------
The model returns the §7.3 shape: every field carries ``value`` / ``raw`` /
``anchor`` / ``confidence``, and columns or rows it could not read come back as
``unknown_columns`` / ``unparsed_rows`` instead of disappearing. Code then proves the
values are real before anything reaches pricing:

* a part number or quantity must be findable in the block/row it claims to come from,
* a quantity must parse as a positive decimal,
* confidence ≥ 0.95 is used as-is, 0.70–0.95 is used but flagged for review,
  below 0.70 the line is refused (never guessed).

Arithmetic never happens here: this module produces structure, ``pricing.py`` produces
numbers.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from decimal import InvalidOperation
from pathlib import Path

from .genesis import Genesis, GenesisError, missing_keys
from .ingest import docx_text, html_prose, html_tables, read_eml, xlsx_sheets
from .models import RFQ, Requirement, positive_decimal
from .pdf_text import extract_lines
from .sources import build_rfq

# Confidence bands from the assessment: adopt / adopt-but-ask / refuse.
AUTO_ACCEPT = 0.95
REVIEW_ACCEPT = 0.70
# Payload guard: a 122-row table is fine, a 5000-row dump is not.
MAX_ROWS_PER_BLOCK = 400
MAX_VALUE_CHARS = 200
# One model call never echoes more than this many rows.
PAGE_ROWS = 50

UNITS = {"EA", "IN", "FT", "M", "PCS", "PC", "SET", "BOX", "KG", "LB", "GAL", "L"}
DOCUMENT_TYPES = ("rfq", "quote", "statement", "other")

EXTRACT_PROMPT = """你是询价邮件解析器。判断这封邮件是什么，并把询价内容抽成结构化 JSON。

铁律：
1. 只依据给定内容块，禁止补全、推测或改写任何值。找不到的值写 "" 并把 confidence 写 0。
2. 每个字段写成 {"value": 规范值, "raw": 原文片段, "anchor": {"block": 块号, "row": 行号},
   "confidence": 0~1, "evidence": ["判断依据"]}。
3. 料号、数量、单位必须能在 anchor 指向的那一行里原样找到（允许空格/连字符差异）。
4. 认不出的列放 unknown_columns，读不懂的行放 unparsed_rows，不要静默丢弃。
5. 数字只抄录，不做单位换算、不补小数、不计算金额。
6. 只输出一个 JSON 对象，不要代码围栏，不要解释。

输出格式：
{"document_type": "rfq|quote|statement|other",
 "document_confidence": 0.0,
 "customer": 字段对象, "rfq_number": 字段对象, "evaluation_date": 字段对象,
 "urgency": "NORMAL|URGENT", "currency": "CNY",
 "requirements": ["客户原文提出的报价要求"],
 "lines": [{"part_number": 字段对象, "quantity": 字段对象, "unit": 字段对象,
            "description": 字段对象, "condition": 字段对象, "due_at": 字段对象}],
 "unknown_columns": ["列名"],
 "unparsed_rows": [{"block": "b3", "row": 18, "reason": "数量为 N/A"}]}"""


class PerceptionError(RuntimeError):
    """Nothing usable came out of the mail."""


@dataclass
class Block:
    """One readable piece of a mail: body text, an HTML table, a sheet, a PDF page range."""

    id: str
    kind: str
    name: str
    rows: list[list[str]] = field(default_factory=list)
    text: str = ""
    note: str = ""

    @property
    def line_count(self) -> int:
        return len(self.rows) if self.rows else len(self.text.splitlines())

    def lines(self) -> list[str]:
        if self.rows:
            return [" | ".join(cell for cell in row if str(cell).strip()) for row in self.rows]
        return [line for line in self.text.splitlines() if line.strip()]

    def render(self) -> str:
        lines = self.lines()
        shown = lines[:MAX_ROWS_PER_BLOCK]
        body = "\n".join(f"{number:>4}| {line[:400]}" for number, line in enumerate(shown, start=1))
        if len(lines) > len(shown):
            body += f"\n…… 还有 {len(lines) - len(shown)} 行未展示"
        header = f"[{self.id}] {self.kind} · {self.name} · {len(lines)} 行"
        if self.note:
            header += f"（{self.note}）"
        return f"{header}\n{body}"

    def row_text(self, row: int) -> str:
        lines = self.lines()
        if 1 <= row <= len(lines):
            return lines[row - 1]
        return ""


# ---------- blocks ----------

def list_blocks(path: Path) -> list[Block]:
    """Every readable container in the mail — wording *and* attachments, not either/or.

    Order is deliberate: the mail's own wording first (that is where a buyer states
    the conditions), then each body table on its own, then every attachment. Block
    ids are stable per position so an anchor written by the model still resolves.
    """
    mail = read_eml(path)
    blocks: list[Block] = []

    # 1) The wording. An HTML-only mail has no text/plain part at all, so reading
    #    mail.text alone loses every sentence that is not inside a cell.
    prose = (mail.text or "").strip()
    if not prose and (mail.html or "").strip():
        prose = html_prose(mail.html)
    if prose.strip():
        blocks.append(Block("b1", "body-text", "邮件正文（文字）", text=prose))

    # 2) Each table separately. Keeping them in one bucket merged a disclaimer or
    #    address block into the real inquiry rows and shifted the header row.
    for index, table in enumerate(html_tables(mail.html), start=1):
        if table:
            blocks.append(Block(f"b{index + 1}", "body-html",
                                f"邮件正文（HTML 表格 {index}）", rows=table))

    # 3) Every attachment, in the order it appeared.
    for index, (filename, payload) in enumerate(mail.attachments, start=1):
        block_id = f"a{index}"
        suffix = Path(filename).suffix.lower()
        if suffix == ".xlsx":
            try:
                sheets = xlsx_sheets(payload)
            except (ValueError, OSError) as exc:
                blocks.append(Block(block_id, "attachment-xlsx", filename,
                                    note=f"打不开：{exc}"))
                continue
            if not sheets:
                blocks.append(Block(block_id, "attachment-xlsx", filename, note="工作簿是空的"))
            for sheet_index, (title, rows) in enumerate(sheets, start=1):
                blocks.append(Block(f"{block_id}-{sheet_index}", "attachment-xlsx",
                                    f"{filename} · {title}", rows=rows))
        elif suffix == ".pdf":
            try:
                lines = extract_lines(payload)
            except (ValueError, OSError) as exc:
                blocks.append(Block(block_id, "attachment-pdf", filename, note=f"抽不出文本：{exc}"))
                continue
            blocks.append(Block(block_id, "attachment-pdf", filename, text="\n".join(lines)))
        elif suffix == ".docx":
            try:
                lines = docx_text(payload)
            except (ValueError, OSError) as exc:
                blocks.append(Block(block_id, "attachment-docx", filename, note=f"读不出正文：{exc}"))
                continue
            blocks.append(Block(block_id, "attachment-docx", filename, text="\n".join(lines)))
        elif suffix in (".txt", ".csv"):
            text = payload.decode("utf-8", "replace")
            blocks.append(Block(block_id, "attachment-text", filename, text=text))
        else:
            # Reported, never silently skipped — that is how a mail used to vanish.
            blocks.append(Block(block_id, "attachment-other", filename,
                                note=f"暂不解析的附件类型：{suffix or '无扩展名'}"))
    return blocks


def render_blocks(blocks: list[Block]) -> str:
    usable = [block for block in blocks if block.kind != "attachment-other" and block.line_count]
    return "\n\n".join(block.render() for block in usable)


def _block_by_id(blocks: list[Block], block_id: str) -> Block | None:
    for block in blocks:
        if block.id == block_id:
            return block
    # A model may shorten "a2-2" to "a2"; accept the unique prefix instead of failing.
    matches = [block for block in blocks if block.id.startswith(block_id)]
    return matches[0] if len(matches) == 1 else None


# ---------- model ----------

def _usable(blocks: list[Block]) -> list[Block]:
    return [block for block in blocks if block.kind != "attachment-other" and block.line_count]


def _chunks(blocks: list[Block]) -> list[tuple[Block, int, int]]:
    """Page big tables so one call never has to echo 122 rows x 6 fields."""
    chunks: list[tuple[Block, int, int]] = []
    for block in _usable(blocks):
        total = block.line_count
        if total <= PAGE_ROWS:
            chunks.append((block, 1, total))
            continue
        for start in range(1, total + 1, PAGE_ROWS):
            chunks.append((block, start, min(start + PAGE_ROWS - 1, total)))
    return chunks


def _render_chunk(block: Block, start: int, end: int) -> str:
    lines = block.lines()
    window = lines[start - 1:end]
    body = "\n".join(f"{start + offset:>4}| {line[:400]}"
                     for offset, line in enumerate(window))
    header = f"[{block.id}] {block.kind} · {block.name} · {len(lines)} 行"
    if len(lines) > len(window):
        header += f"，本次给出第 {start}–{end} 行"
    return f"{header}\n{body}"


def _chat_json(config: dict, system: str, payload: str, timeout: int) -> dict:
    client = Genesis(config, timeout=timeout)
    message = client.chat([{"role": "system", "content": system},
                           {"role": "user", "content": payload}])
    content = (message.get("content") or "").strip()
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise PerceptionError("模型没有返回 JSON")
    try:
        return json.loads(content[start:end + 1])
    except json.JSONDecodeError as exc:
        raise PerceptionError(f"模型返回的 JSON 解析失败：{exc}") from exc


def extract(config: dict, blocks: list[Block], timeout: int = 180) -> dict:
    """Full extraction, paged. Used only when the deterministic parser could not read.

    Every page keeps the original row numbers in the left margin, so anchors still
    point at the real line instead of a page-local index.
    """
    if not _usable(blocks):
        raise PerceptionError("邮件里没有可读的内容块")
    merged: dict = {"lines": [], "requirements": [], "unknown_columns": [], "unparsed_rows": []}
    chunks = _chunks(blocks)
    for number, (block, start, end) in enumerate(chunks, start=1):
        page_note = ""
        if (start, end) != (1, block.line_count):
            page_note = (f"\n\n注意：本页只给出 [{block.id}] 的第 {start}–{end} 行，"
                         f"lines 只返回这些行，anchor.row 用左侧原始行号。")
        first = number == 1
        prompt = EXTRACT_PROMPT if first else _PAGE_PROMPT
        result = _chat_json(config, prompt, _render_chunk(block, start, end) + page_note, timeout)
        if first:
            for key in ("document_type", "document_confidence", "customer", "rfq_number",
                        "evaluation_date", "urgency", "currency"):
                if key in result:
                    merged[key] = result[key]
        merged["lines"].extend(result.get("lines") or [])
        for key in ("requirements", "unknown_columns", "unparsed_rows"):
            merged.setdefault(key, [])
            merged[key].extend(result.get(key) or [])
    return merged


_PAGE_PROMPT = EXTRACT_PROMPT.replace(
    "判断这封邮件是什么，并把询价内容抽成结构化 JSON。",
    "只把这一页的行项目抽成结构化 JSON；document_type 等邮件级字段可以省略。")


# ---------- audit mode: the code parsed it, the model checks the reading ----

AUDIT_PROMPT = """你是询价审核员。确定性解析器已经读出了行项目，你只做四件事，不重算任何数字：

1. 判断这封邮件是什么：rfq（询价单）/ quote（报价单）/ statement（对账单）/ other，并给出把握。
2. 抽取客户在正文里提出的报价要求，保留原文，不要改写。
3. 指出可疑之处：解析结果里看起来错的行、被忽略的列、读不懂的行、疑似单位混用。
4. 一句话结论。

unknown_columns 只列「解析器已识别的列」里没有的列；已识别的列不要重复报告。

铁律：禁止重算数量或价格，禁止编造行项目，禁止补全原文没有的字段。只输出一个 JSON 对象。

输出格式：
{"document_type": "rfq", "document_confidence": 0.0,
 "requirements": ["客户原文要求"],
 "unknown_columns": ["没被解析器认出来的列"],
 "suspicious_rows": [{"row": 12, "why": "数量列看起来是单价"}],
 "unparsed_rows": [{"block": "b3", "row": 18, "reason": "读不懂"}],
 "judgement": "一句话结论"}"""


def _baseline_digest(baseline: RFQ, limit: int = 8) -> str:
    """What the parser claims it read — small enough that the audit call stays cheap."""
    lines = baseline.lines
    head = "\n".join(
        f"{index:>4}| {line.part_number} | {line.description[:40]} | "
        f"{line.quantity} {line.unit} | {','.join(line.condition_accept) or '-'}"
        for index, line in enumerate(lines[:limit], start=1))
    parts = [f"客户：{baseline.customer}", f"询价单号：{baseline.rfq_number}",
             f"评估日期：{baseline.evaluation_date}", f"紧急度：{baseline.urgency}",
             f"行数：{len(lines)}（模型只看前 {min(limit, len(lines))} 行）"]
    # Tell the model which headers were already understood, so "unknown_columns" means
    # genuinely unmapped ones instead of re-reporting the parser's own dictionary.
    columns = ((lines[0].evidence or {}).get("columns") or {}) if lines else {}
    if columns:
        parts.append("解析器已识别的列：" + "、".join(
            f"{name}←「{meta.get('header')}」" for name, meta in columns.items()))
    parts.append("")
    parts.append(head)
    if len(lines) > limit:
        parts.append(f"…… 另有 {len(lines) - limit} 行未展示")
    return "\n".join(parts)


def audit(config: dict, blocks: list[Block], baseline: RFQ, timeout: int = 90) -> dict:
    """One small call: classify the mail and challenge the parser's reading."""
    prose = [block for block in _usable(blocks)
             if block.kind in ("body-text", "body-html", "attachment-pdf",
                               "attachment-docx", "attachment-text")]
    payload = ["## 邮件内容"]
    payload += [block.render() for block in prose] or ["（正文为空）"]
    payload += ["", "## 附件", *[f"- [{b.id}] {b.name}（{b.line_count} 行）"
                                 for b in _usable(blocks) if b not in prose]]
    payload += ["", "## 确定性解析结果", _baseline_digest(baseline)]
    return _chat_json(config, AUDIT_PROMPT, "\n".join(payload), timeout)


# ---------- validation ----------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _squash(text: str) -> str:
    """Case/space/hyphen-insensitive form: 'MS28775 - 225' == 'ms28775-225'."""
    return re.sub(r"[\s\-_]+", "", str(text or "")).casefold()


def _field(entry, name: str) -> dict:
    """Read one contract field, tolerating a model that answered with a bare value."""
    value = (entry or {}).get(name)
    if isinstance(value, dict):
        raw = value.get("raw")
        return {
            "value": "" if value.get("value") is None else str(value.get("value")).strip(),
            "raw": "" if raw is None else str(raw).strip(),
            "confidence": _confidence(value.get("confidence")),
            "anchor": value.get("anchor") or {},
            "evidence": [str(item)[:MAX_VALUE_CHARS] for item in (value.get("evidence") or [])][:4],
        }
    if value is None:
        return {"value": "", "raw": "", "confidence": 0.0, "anchor": {}, "evidence": []}
    return {"value": str(value).strip(), "raw": str(value).strip(),
            "confidence": 1.0, "anchor": {}, "evidence": []}


def _confidence(value) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))


def _found(value: str, anchor: str, haystack: str) -> bool:
    """Is the claimed value actually in the text it says it came from?"""
    if not value:
        return False
    for needle, hay in ((_norm(value), _norm(anchor)), (_squash(value), _squash(anchor))):
        if needle and needle in hay:
            return True
    for needle, hay in ((_norm(value), _norm(haystack)), (_squash(value), _squash(haystack))):
        if needle and needle in hay:
            return True
    return False


@dataclass
class Check:
    """What survived validation, plus every reason something did not."""

    document_type: str
    document_confidence: float
    lines: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)
    unparsed_rows: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        """Refuse to price when the mail is not an RFQ or nothing usable came out."""
        if self.document_type != "rfq" and self.document_confidence >= REVIEW_ACCEPT:
            return True
        return not self.lines


def validate(extraction: dict, blocks: list[Block]) -> Check:
    """Apply the §7.3 confidence bands and prove every value against the source."""
    document_type = str(extraction.get("document_type") or "").strip().lower()
    if document_type not in DOCUMENT_TYPES:
        document_type = "rfq" if extraction.get("lines") else "other"
    check = Check(document_type=document_type,
                  document_confidence=_confidence(extraction.get("document_confidence")))
    haystack = "\n".join(block.text + "\n" + "\n".join(block.lines()) for block in blocks)
    check.unknown_columns = [str(item)[:80] for item in (extraction.get("unknown_columns") or [])][:20]
    check.unparsed_rows = [item for item in (extraction.get("unparsed_rows") or [])
                           if isinstance(item, dict)][:50]
    check.meta = {
        "customer": _field(extraction, "customer")["value"],
        "rfq_number": _field(extraction, "rfq_number")["value"],
        "evaluation_date": _field(extraction, "evaluation_date")["value"],
        "urgency": str(extraction.get("urgency") or "NORMAL").upper(),
        "currency": str(extraction.get("currency") or "CNY").upper()[:8],
        "requirements": [str(item)[:300] for item in (extraction.get("requirements") or [])
                         if str(item).strip()][:20],
    }
    if check.meta["urgency"] not in ("NORMAL", "URGENT"):
        check.meta["urgency"] = "NORMAL"

    if check.document_type != "rfq" and check.document_confidence >= REVIEW_ACCEPT:
        check.issues.append({
            "kind": "not_an_rfq", "severity": "blocking",
            "detail": f"模型判断这不是询价单（{check.document_type}，"
                      f"置信度 {check.document_confidence:.2f}）"})
        return check

    for index, entry in enumerate(extraction.get("lines") or [], start=1):
        if not isinstance(entry, dict):
            continue
        part = _field(entry, "part_number")
        quantity = _field(entry, "quantity")
        unit = _field(entry, "unit")
        anchor = part.get("anchor") or quantity.get("anchor") or {}
        block = _block_by_id(blocks, str(anchor.get("block") or ""))
        try:
            anchor_text = block.row_text(int(anchor.get("row"))) if block else ""
        except (TypeError, ValueError):
            anchor_text = ""

        reasons = []
        if not part["value"]:
            reasons.append("料号为空")
        elif not _found(part["value"], anchor_text, haystack):
            reasons.append(f"料号 {part['value']!r} 在原文找不到")
        if not quantity["value"]:
            reasons.append("数量为空")
        else:
            try:
                positive_decimal(quantity["value"])
            except (ValueError, InvalidOperation):
                reasons.append(f"数量 {quantity['value']!r} 不是正数")
            else:
                if quantity["raw"] and not _found(quantity["raw"], anchor_text, haystack):
                    reasons.append(f"数量原文 {quantity['raw']!r} 在该行找不到")
        if not unit["value"]:
            reasons.append("单位为空")
        elif unit["value"].upper() not in UNITS:
            check.issues.append({"kind": "unit_unknown", "severity": "review",
                                 "line": index, "detail": f"单位 {unit['value']!r} 不在已知集合里"})

        confidence = min(part["confidence"], quantity["confidence"])
        record = {"index": index, "confidence": round(confidence, 3),
                  "part_number": part, "quantity": quantity, "unit": unit,
                  "description": _field(entry, "description"),
                  "condition": _field(entry, "condition"),
                  "due_at": _field(entry, "due_at"),
                  "anchor": {"block": block.id if block else "", "row": anchor.get("row")}}

        if reasons:
            check.rejected.append({**record, "reasons": reasons})
            check.issues.append({"kind": "line_refused", "severity": "blocking",
                                 "line": index, "detail": "；".join(reasons)})
            continue
        if confidence < REVIEW_ACCEPT:
            check.rejected.append({**record, "reasons": [f"置信度 {confidence:.2f} 低于 {REVIEW_ACCEPT}"]})
            check.issues.append({"kind": "line_refused", "severity": "blocking", "line": index,
                                 "detail": f"置信度 {confidence:.2f} 低于 {REVIEW_ACCEPT}，不采用"})
            continue
        if confidence < AUTO_ACCEPT:
            check.review.append(record)
            check.issues.append({"kind": "line_best_guess", "severity": "review", "line": index,
                                 "detail": f"置信度 {confidence:.2f}：采用，但按最佳猜测对待"})
        check.lines.append(_to_row(record, block))
    return check


def _to_row(record: dict, block: Block | None) -> dict:
    """Canonical row dict — the same shape ingest.py feeds to build_rfq()."""
    anchor = record["anchor"]
    return {
        "Part Number": record["part_number"]["value"],
        "Quantity": record["quantity"]["value"],
        "Unit Of Measure": record["unit"]["value"],
        "Description": record["description"]["value"],
        "件状条件": record["condition"]["value"],
        "截止报价日期": record["due_at"]["value"],
        "询价单号": record["part_number"]["anchor"].get("inquiry_no") or "",
        "_container": "agent-extract",
        "_source_row": anchor.get("row"),
        "_sheet": block.name if block else "",
        "_extraction": {
            "block": anchor.get("block"),
            "row": anchor.get("row"),
            "confidence": record["confidence"],
            "fields": {name: {"raw": record[name]["raw"], "confidence": record[name]["confidence"]}
                       for name in ("part_number", "quantity", "unit", "description", "condition")},
        },
    }


# ---------- the main path ----------

def _deterministic(path: Path) -> tuple[RFQ | None, str]:
    """The existing parser, kept as the audit baseline and the fallback."""
    from .ingest import ingest_eml

    try:
        return ingest_eml(path), ""
    except (ValueError, OSError) as exc:
        return None, str(exc)


def _blocks_report(blocks: list[Block]) -> list[dict]:
    return [{"id": block.id, "kind": block.kind, "name": block.name,
             "lines": block.line_count, "note": block.note} for block in blocks]


def _model_rfq(check: Check, path: Path, mail) -> RFQ:
    rows = check.lines
    number = check.meta.get("rfq_number") or ""
    if not number:
        match = re.search(r"RFQ[- ]?(\d{4,})", mail.subject or "")
        number = f"RFQ-{match.group(1)}" if match else path.stem
    date = check.meta.get("evaluation_date") or ""
    if not date:
        from .ingest import _mail_date

        date = _mail_date(mail.date) or dt.date.today().isoformat()
    else:
        try:
            dt.date.fromisoformat(date[:10])
            date = date[:10]
        except ValueError:
            date = dt.date.today().isoformat()
    return build_rfq(
        rows,
        rfq_number=number,
        evaluation_date=date,
        source_files=[path.name] + [name for name, _ in mail.attachments],
        customer=check.meta.get("customer") or "未标注客户",
        subject=mail.subject,
        urgency=check.meta.get("urgency") or "NORMAL",
        requirements=[Requirement(text=text, source="模型抽取")
                      for text in check.meta.get("requirements") or []],
    )


def _run_audit(config: dict, blocks: list[Block], baseline: RFQ) -> tuple[RFQ, dict]:
    """Code parsed the mail; the model judges what it is and what looks wrong.

    The parser's numbers are kept as-is (they came from cells, not from wording), so
    this call stays small and cannot move a price. What it adds is the judgement the
    parser has no way to make: is this even an inquiry, what did the buyer demand in
    prose, and which rows look misread.
    """
    result = audit(config, blocks, baseline)
    document_type = str(result.get("document_type") or "rfq").strip().lower()
    if document_type not in DOCUMENT_TYPES:
        document_type = "rfq"
    confidence = _confidence(result.get("document_confidence"))
    issues = []
    if document_type != "rfq" and confidence >= REVIEW_ACCEPT:
        # Judgement is advisory, not a veto: the human sees it and decides.
        issues.append({"kind": "not_an_rfq", "severity": "review",
                       "detail": f"模型判断这是 {document_type}（置信度 {confidence:.2f}），"
                                 f"不是询价单；仍按询价处理，请人工确认"})
    for item in result.get("suspicious_rows") or []:
        if isinstance(item, dict):
            issues.append({"kind": "suspicious_row", "severity": "review",
                           "detail": f"第 {item.get('row')} 行：{str(item.get('why') or '')[:160]}"})

    # Requirements are additive, never a replacement: the regex found the numbered
    # ones, the model finds the ones written as prose. Discarding the model's list
    # whenever the regex found *anything* silently dropped the harder half.
    added = 0
    known = {" ".join(req.text.split()).casefold() for req in baseline.requirements}
    for text in (result.get("requirements") or [])[:20]:
        cleaned = " ".join(str(text).split())[:300]
        key = cleaned.casefold()
        if not cleaned or key in known:
            continue
        baseline.requirements.append(Requirement(text=cleaned, source="模型审查"))
        known.add(key)
        added += 1

    report = {
        "mode": "audit",
        "source": "agent+deterministic",
        "reason": "确定性解析成功；模型负责判断文档类型、条款与可疑行",
        "model": config.get("GENESIS_MODEL", ""),
        "document_type": document_type,
        "document_confidence": confidence,
        "judgement": str(result.get("judgement") or "")[:300],
        "blocks": _blocks_report(blocks),
        "lines_model": 0,
        "lines_deterministic": len(baseline.lines),
        "requirements_added": added,
        "unknown_columns": [str(item)[:80] for item in (result.get("unknown_columns") or [])][:20],
        "unparsed_rows": [item for item in (result.get("unparsed_rows") or [])
                          if isinstance(item, dict)][:50],
        "refused": [],
        "best_guess": [],
        "issues": issues,
    }
    return baseline, report


def _run_extract(config: dict, path: Path, blocks: list[Block],
                 baseline_error: str) -> tuple[RFQ, dict]:
    """The parser could not read this mail: the model extracts, code validates every value."""
    extraction = extract(config, blocks)
    check = validate(extraction, blocks)
    report = {
        "mode": "extract",
        "model": config.get("GENESIS_MODEL", ""),
        "document_type": check.document_type,
        "document_confidence": check.document_confidence,
        "blocks": _blocks_report(blocks),
        "lines_model": len(check.lines),
        "lines_deterministic": 0,
        "baseline_error": baseline_error,
        "requirements_added": len(check.meta.get("requirements") or []),
        "unknown_columns": check.unknown_columns,
        "unparsed_rows": check.unparsed_rows,
        "refused": [{"line": item["index"], "part_number": item["part_number"]["value"],
                     "reasons": item["reasons"]} for item in check.rejected],
        "best_guess": [item["index"] for item in check.review],
        "issues": check.issues,
    }
    if check.blocking:
        raise PerceptionError("模型抽取未通过校验，且确定性解析也失败："
                              + "；".join(issue["detail"] for issue in check.issues[:3]))
    report["source"] = "agent"
    report["reason"] = "确定性解析失败，采用模型抽取（已逐字段校验并回指原文）"
    return _model_rfq(check, path, read_eml(path)), report


def run(config: dict, path: Path) -> tuple[RFQ, dict]:
    """Main path. Raises only when neither the model nor the parser produced a usable RFQ."""
    if missing_keys(config):
        raise PerceptionError("模型未配置：缺少 " + "、".join(missing_keys(config)))
    blocks = list_blocks(path)
    if not _usable(blocks):
        raise PerceptionError("邮件里没有可读的内容块")
    baseline, baseline_error = _deterministic(path)
    if baseline is not None:
        return _run_audit(config, blocks, baseline)
    return _run_extract(config, path, blocks, baseline_error)