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

from rfq_agent.domain.models import Requirement, RFQ
from rfq_agent.ingestion.pdf_text import extract_lines
from rfq_agent.ingestion.sources import build_rfq

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
    "询价产品要求": "询价产品要求", "product requirement": "询价产品要求",
    "part no": "Part Number", "part no.": "Part Number", "p/n": "Part Number",
    "qty": "Quantity", "uom": "Unit Of Measure",
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


# ---------- raw access for the perception layer ----------
# ingest_eml() picks ONE container and normalises it. Perception needs the opposite:
# every raw block, unnormalised, so the model can see what the code could not map.

def html_rows(markup: str) -> list[list[str]]:
    """Every row of the first table, as printed — headers are not interpreted."""
    parser = _TableParser()
    parser.feed(markup)
    return parser.rows


class _ProseParser(HTMLParser):
    """Text of the mail outside every <table>, with block tags becoming line breaks.

    A mail whose body is HTML may carry no text/plain part at all. Reading only
    ``text/plain`` therefore loses every sentence around the inquiry table — the
    "please quote DDP by the 15th" that is not a cell anywhere.
    """

    BREAKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._skip += 1
        elif tag in self.BREAKS and not self._skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "table" and self._skip:
            self._skip -= 1
        elif tag in self.BREAKS and not self._skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_prose(markup: str) -> str:
    """The mail's wording, tables removed. Used when there is no text/plain part."""
    parser = _ProseParser()
    try:
        parser.feed(markup or "")
    except Exception:                       # malformed markup: fall back to stripping
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup or "")).strip()
    text = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


class _TablesParser(HTMLParser):
    """Each <table> kept separately: a disclaimer table must not merge into the data."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._rows = []
        elif tag == "tr" and self._depth == 1:
            self._row = []
        elif tag in ("td", "th") and self._row is not None and self._depth == 1:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(cell for cell in self._row) and self._rows is not None:
                self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0 and self._rows:
                self.tables.append(self._rows)
                self._rows = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_tables(markup: str) -> list[list[list[str]]]:
    """Every table in the body, in document order, rows and cells untouched."""
    parser = _TablesParser()
    parser.feed(markup or "")
    return parser.tables


def docx_text(data: bytes) -> list[str]:
    """Paragraph text out of a .docx, stdlib only (a .docx is a zip of XML parts).

    Word tables put every cell in its own ``w:p``, so iterating paragraphs also
    recovers tabular text without pretending to understand the table structure.
    """
    import zipfile
    from xml.etree import ElementTree

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(f"不是可读的 .docx：{exc}") from exc
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError(f".docx 正文 XML 损坏：{exc}") from exc
    lines = []
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
        text = " ".join(text.split())
        if text:
            lines.append(text)
    return lines


def xlsx_sheets(data: bytes) -> list[tuple[str, list[list[str]]]]:
    """Every worksheet, so a cover sheet in position 1 does not hide the real table."""
    from rfq_agent.infrastructure.deps import require

    openpyxl = require("openpyxl", "reading the .xlsx attachment")
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets = []
    try:
        for sheet in workbook.worksheets:
            rows = [[("" if cell is None else str(cell)) for cell in row]
                    for row in sheet.iter_rows(values_only=True)]
            while rows and all(not cell for cell in rows[-1]):
                rows.pop()
            if rows:
                sheets.append((sheet.title, rows))
    finally:
        workbook.close()
    return sheets


def _column_letter(index: int) -> str:
    """1 -> A, 27 -> AA: the cell address a reviewer types into Excel."""
    letters = ""
    while index > 0:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _to_records(header: list[str], rows: list[list[str]], container: str,
                location: dict | None = None) -> list[dict]:
    keys = [_normalise_header(cell) for cell in header]
    if not all(name in keys for name in REQUIRED):
        raise ValueError(
            f"{container} 表头无法识别：需要 {list(REQUIRED)}，实际 {header}")
    # Column provenance: canonical name -> where it physically sits in the source.
    columns = {key: {"index": i + 1, "letter": _column_letter(i + 1), "header": str(header[i])}
               for i, key in enumerate(keys) if key}
    records = []
    for offset, row in enumerate(rows, start=2):
        if all(not str(cell).strip() for cell in row):
            continue
        record = {key: (row[i] if i < len(row) else "") for i, key in enumerate(keys) if key}
        record["_container"] = container
        record["_source_row"] = offset
        record["_columns"] = columns
        record.update({f"_{name}": value for name, value in (location or {}).items()})
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
    """Pick the table that actually carries the inquiry, not simply the first one.

    A body can hold an address block or a disclaimer table before the real one; using
    "the first table" then fails on a mail that is perfectly readable.
    """
    tables = html_tables(markup)
    if not tables:
        raise ValueError("HTML 正文里没有可用的表格")
    problems = []
    for index, table in enumerate(tables, start=1):
        if len(table) < 2:
            continue
        try:
            return _to_records(table[0], table[1:], "body-html",
                               {"sheet": f"正文表格 {index}" if index > 1 else "正文表格 1"})
        except ValueError as exc:
            problems.append(f"表格 {index}：{exc}")
    raise ValueError("HTML 正文里没有可用的表格"
                     + ("；" + "；".join(problems[:3]) if problems else ""))


def _rows_from_text(text: str) -> list[dict]:
    if "\t" not in text:
        raise ValueError("纯文本正文没有制表符分隔，无法可靠切列")
    lines = [line for line in text.splitlines() if line.strip()]
    header_at = next((i for i, line in enumerate(lines) if "Part Number" in line), None)
    if header_at is None:
        raise ValueError("纯文本正文里找不到表头行")
    rows = [line.split("\t") for line in lines[header_at + 1:]]
    return _to_records(lines[header_at].split("\t"), rows, "body-text", {"sheet": "正文文本"})


def _rows_from_xlsx(data: bytes, filename: str = "") -> list[dict]:
    from rfq_agent.infrastructure.deps import require

    openpyxl = require("openpyxl", "reading the .xlsx attachment")
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    sheet_name = sheet.title
    rows = [[("" if cell is None else str(cell)) for cell in row]
            for row in sheet.iter_rows(values_only=True)]
    workbook.close()
    while rows and all(not cell for cell in rows[-1]):
        rows.pop()
    if len(rows) < 2:
        raise ValueError("附件表格为空")
    return _to_records(rows[0], rows[1:], "attachment-xlsx",
                       {"attachment": filename, "sheet": sheet_name})


PDF_ITEM = re.compile(r"^(\d{4}) \| ([A-Z0-9-]+)\s*$")
PDF_QTY = re.compile(r"^(\d+(?:\.\d+)?) (Each|EA|PCS|SET)\s*$")


def _rows_from_pdf(data: bytes, filename: str = "") -> tuple[dict, list[dict]]:
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
    for number, line in enumerate(lines, start=1):
        item = PDF_ITEM.match(line)
        if item:
            current = {"询价单号": item.group(1), "Part Number": item.group(2),
                       "_container": "attachment-pdf", "_source_row": len(records) + 2,
                       "_attachment": filename, "_sheet": "PDF 文本",
                       "_pdf_line": number, "_raw_line": line}
            records.append(current)
            stage = 1
            continue
        if current is None:
            continue
        qty = PDF_QTY.match(line)
        if qty and stage == 2:
            current["Quantity"] = qty.group(1)
            # Keep the unit as printed (Each / PCS / SET): no silent mapping to EA.
            current["Unit Of Measure"] = qty.group(2)
            current["_unit_raw"] = qty.group(2)
            current["_qty_line"] = number
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
                rows = _rows_from_xlsx(payload, filename)
                break
            if suffix == ".pdf":
                meta, rows = _rows_from_pdf(payload, filename)
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

    # An HTML-only mail has no text/plain part: without the fallback, every sentence
    # in the body is invisible to requirement extraction.
    body_prose = mail.text or html_prose(mail.html)
    requirements = [Requirement(text=text, source="邮件正文")
                    for text in _requirements_from_body(body_prose)]
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