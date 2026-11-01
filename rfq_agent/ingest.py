"""Ingest the official material: one .eml in, one RFQ out.

The six cases arrive in four containers, and the agent must not care which:
  A, D  inquiry table inside the mail body (HTML table / tab-separated text)
  B, E  .xlsx attachment
  C, F  .pdf attachment

Everything is normalised to the column names the .xlsx attachment uses, so one
row->Line mapping serves all four. Nothing here guesses a file name or a case.
"""
from __future__ import annotations

import datetime as dt
import email
import email.policy
import email.utils
import html as html_mod
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .models import Requirement, RFQ
from .pdf_text import extract_lines
from .sources import build_rfq

# Header cell -> the canonical column name used by the .xlsx attachment.
HEADER_ALIASES = {
    "询价单号": "询价单号", "rfq no.": "询价单号", "rfq no": "询价单号",
    "part number": "Part Number", "料号": "Part Number", "件号": "Part Number",
    "description": "Description", "描述": "Description",
    "quantity": "Quantity", "数量": "Quantity",
    "unit of measure": "Unit Of Measure", "unit of measure ": "Unit Of Measure",
    "单位": "Unit Of Measure",
    "优先级": "优先级", "priority": "优先级",
    "件状条件": "件状条件", "condition": "件状条件", "件状": "件状条件",
    "截止报价日期": "截止报价日期", "due date": "截止报价日期",
    "询价mpn": "询价MPN", "mpn": "询价MPN",
    "手册件号": "手册件号",
    "产品范围": "产品范围",
    "询价产品要求": "询价产品要求",
}
REQUIRED = ("Part Number", "Quantity", "Unit Of Measure")


@dataclass
class Email:
    path: Path
    subject: str = ""
    sender: str = ""
    date: str = ""
    text: str = ""
    html: str = ""
    attachments: list[tuple[str, bytes]] = field(default_factory=list)


class _TableParser(HTMLParser):
    """Collect the first <table> as rows of cell text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
        elif tag == "tr" and self._depth:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _normalise_header(cell: str) -> str | None:
    return HEADER_ALIASES.get(re.sub(r"\s+", " ", cell).strip().lower())


def _to_records(header: list[str], rows: list[list[str]], container: str) -> list[dict]:
    keys = [_normalise_header(cell) for cell in header]
    if not all(name in keys for name in REQUIRED):
        raise ValueError(
            f"{container} 表头无法识别：需要 {list(REQUIRED)}，实际 {header}")
    records = []
    for offset, row in enumerate(rows, start=2):
        if all(not str(cell).strip() for cell in row):
            continue
        record = {key: (row[i] if i < len(row) else "") for i, key in enumerate(keys) if key}
        record["_container"] = container
        record["_source_row"] = offset
        records.append(record)
    return records


def read_eml(path: Path) -> Email:
    message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    mail = Email(path=path, subject=str(message.get("Subject") or ""),
                 sender=str(message.get("From") or ""), date=str(message.get("Date") or ""))
    for part in message.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            mail.attachments.append((filename, payload))
        elif part.get_content_type() == "text/plain":
            mail.text = payload.decode("utf-8", "replace")
        elif part.get_content_type() == "text/html":
            mail.html = payload.decode("utf-8", "replace")
    return mail


def _rows_from_html(markup: str) -> list[dict]:
    parser = _TableParser()
    parser.feed(markup)
    if len(parser.rows) < 2:
        raise ValueError("HTML 正文里没有可用的表格")
    return _to_records(parser.rows[0], parser.rows[1:], "body-html")


def _rows_from_text(text: str) -> list[dict]:
    if "\t" not in text:
        raise ValueError("纯文本正文没有制表符分隔，无法可靠切列")
    lines = [line for line in text.splitlines() if line.strip()]
    header_at = next((i for i, line in enumerate(lines) if "Part Number" in line), None)
    if header_at is None:
        raise ValueError("纯文本正文里找不到表头行")
    rows = [line.split("\t") for line in lines[header_at + 1:]]
    return _to_records(lines[header_at].split("\t"), rows, "body-text")


def _rows_from_xlsx(data: bytes) -> list[dict]:
    from .deps import require

    openpyxl = require("openpyxl", "reading the .xlsx attachment")
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows = [[("" if cell is None else str(cell)) for cell in row]
            for row in sheet.iter_rows(values_only=True)]
    workbook.close()
    while rows and all(not cell for cell in rows[-1]):
        rows.pop()
    if len(rows) < 2:
        raise ValueError("附件表格为空")
    return _to_records(rows[0], rows[1:], "attachment-xlsx")


PDF_ITEM = re.compile(r"^(\d{4}) \| ([A-Z0-9-]+)\s*$")
PDF_QTY = re.compile(r"^(\d+(?:\.\d+)?) (Each|EA|PCS|SET)\s*$")


def _rows_from_pdf(data: bytes) -> tuple[dict, list[dict]]:
    """The Quote.pdf layout is a flat form: item no | part no, then description, then qty."""
    lines = extract_lines(data)
    meta = {"pdf_lines": lines}
    for line in lines:
        if "REQUEST FOR QUOTATION" in line and "RFQ" in line:
            match = re.search(r"RFQ\s*[-#]?\s*([A-Za-z0-9]+)", line)
            if match:
                meta["rfq_number"] = f"RFQ-{match.group(1)}"
        elif line.startswith("Report date:"):
            meta["report_date"] = line.split(":", 1)[1].strip()
        elif line.startswith("Respond by:"):
            meta["respond_by"] = line.split(":", 1)[1].strip()
        elif line in ("CRITICAL", "AOG", "URGENT"):
            meta["urgency"] = "URGENT"
        elif line.startswith("FOR "):
            meta["aircraft"] = line[4:].strip()

    records: list[dict] = []
    current: dict | None = None
    stage = 0
    for line in lines:
        item = PDF_ITEM.match(line)
        if item:
            current = {"询价单号": item.group(1), "Part Number": item.group(2),
                       "_container": "attachment-pdf", "_source_row": len(records) + 2}
            records.append(current)
            stage = 1
            continue
        if current is None:
            continue
        qty = PDF_QTY.match(line)
        if qty and stage == 2:
            current["Quantity"] = qty.group(1)
            current["Unit Of Measure"] = "EA"
            current["_unit_raw"] = qty.group(2)
            current["件状条件"] = ""
            stage = 3
        elif stage == 1:
            current["Description"] = line
            stage = 2
    missing = [r["Part Number"] for r in records if "Quantity" not in r]
    if missing:
        raise ValueError(f"PDF 中有行未解析出数量：{missing}")
    return meta, records


def _parse_date(value: str) -> str | None:
    for pattern in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _requirements_from_body(text: str) -> list[str]:
    """Numbered items in the mail body — the buyer's own checklist.

    The marker often sits alone on its line ("1.&nbsp;") with the wording below it,
    so an item stays open until it ends with a terminator or prose starts.
    """
    cleaned = re.sub(r"&nbsp;|&amp;", " ", text or "").replace("\xa0", " ")
    items: list[str] = []
    pending: str | None = None

    def flush():
        nonlocal pending
        if pending:
            value = " ".join(pending.split()).strip().rstrip(";").strip()
            if len(value) > 3:
                items.append(value)
        pending = None

    for raw in cleaned.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.match(r"^(\d+)[.)]\s*(.*)$", line)
        if match:
            flush()
            pending = match.group(2).strip()
            continue
        if pending is None or pending.endswith((";", ".", "；", "。")):
            if pending is not None:
                flush()
            continue
        pending = f"{pending} {line}"
    flush()
    return items


def _requirements_from_pdf(lines: list[str]) -> list[str]:
    """The instruction block after the last item: 'Use this form as a quote response.'"""
    start = None
    for index, line in enumerate(lines):
        if line.strip().lower().startswith("remarks") and index + 1 < len(lines):
            start = index + 1
    if start is None:
        return []
    collected = []
    for line in lines[start:]:
        text = line.strip()
        if not text:
            continue
        if text.upper().startswith(("AUTHORIZED", "PAGE ")):
            break
        if text.endswith(".") and " " in text:
            collected.append(text)
    return collected[:8]


def _mail_date(value: str) -> str | None:
    """The mail Date header is the cheapest defensible evaluation date."""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed.date().isoformat() if parsed else None


def ingest_eml(path: Path, *, evaluation_date: str | None = None) -> RFQ:
    mail = read_eml(path)
    meta: dict = {}
    rows: list[dict] = []

    try:
        if mail.html:
            rows = _rows_from_html(mail.html)
        elif mail.text:
            rows = _rows_from_text(mail.text)
    except ValueError:
        rows = []

    if not rows:
        for filename, payload in mail.attachments:
            suffix = Path(filename).suffix.lower()
            if suffix == ".xlsx":
                rows = _rows_from_xlsx(payload)
                break
            if suffix == ".pdf":
                meta, rows = _rows_from_pdf(payload)
                meta["pdf_name"] = filename
                break
    if not rows:
        raise ValueError(f"{path.name}: 正文和附件里都没有可识别的询价表")

    customer = re.sub(r"<.*?>", "", mail.sender).strip() or "未标注客户"
    rfq_number = meta.get("rfq_number") or ""
    if not rfq_number:
        match = re.search(r"RFQ[- ]?(\d{4,})", mail.subject)
        rfq_number = f"RFQ-{match.group(1)}" if match else path.stem
    urgency = meta.get("urgency") or ("URGENT" if re.search(r"紧急|CRITICAL|AOG", mail.subject + mail.text[:400]) else "NORMAL")
    date = (evaluation_date or _parse_date(meta.get("report_date", ""))
            or _mail_date(mail.date))

    requirements = [Requirement(text=text, source="邮件正文")
                    for text in _requirements_from_body(mail.text or "")]
    requirements += [Requirement(text=text, source=meta.get("pdf_name") or "PDF 附件")
                     for text in _requirements_from_pdf(meta.get("pdf_lines") or [])]

    return build_rfq(
        rows,
        rfq_number=rfq_number,
        evaluation_date=date or "",
        source_files=[path.name] + [name for name, _ in mail.attachments],
        customer=customer,
        subject=mail.subject,
        urgency=urgency,
        requirements=requirements,
    )