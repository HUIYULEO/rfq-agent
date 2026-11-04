"""Read the official material into domain objects (no LLM involved)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from rfq_agent.infrastructure.deps import require
from rfq_agent.domain.models import RFQ, Line


def _load_sheet(path: Path) -> tuple[list[str], list[dict]]:
    openpyxl = require("openpyxl", "reading .xlsx material")
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    header = [str(value) for value in rows[0]]
    records = []
    for index, row in enumerate(rows[1:], start=2):
        if all(cell in (None, "") for cell in row):
            continue
        record = {header[i]: (row[i] if i < len(row) else None) for i in range(len(header))}
        record["_source_row"] = index
        records.append(record)
    workbook.close()
    return header, records


def load_price_table(path: Path) -> list[dict]:
    return _load_sheet(path)[1]


def load_cost_table(path: Path) -> list[dict]:
    return _load_sheet(path)[1]


def _iso(value):
    if isinstance(value, dt.datetime):
        return value.isoformat(sep="T")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(value.strip(), pattern).isoformat(sep="T")
            except ValueError:
                continue
    return None


RAW_FIELDS = ("询价单号", "Part Number", "Quantity", "Unit Of Measure", "件状条件", "询价产品要求",
              "询价MPN", "手册件号", "产品范围", "截止报价日期", "Description")


def _text(value) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def build_rfq(inquiry_rows: list[dict], *, rfq_number: str, evaluation_date: str,
              source_files: list[str], customer: str = "", subject: str = "",
              urgency: str = "NORMAL", requirements: list | None = None) -> RFQ:
    from rfq_agent.domain.compliance import required_certificates

    lines = []
    for number, item in enumerate(inquiry_rows, start=1):
        condition = str(item.get("件状条件") or "").strip()
        requirement = _text(item.get("询价产品要求"))
        columns = item.get("_columns") or {}
        lines.append(Line(
            line_id=f"L{number:03d}",
            part_number=str(item["Part Number"]).strip(),
            quantity=str(item["Quantity"]),
            unit=str(item["Unit Of Measure"]),
            description=str(item.get("Description") or ""),
            condition_accept=[condition] if condition else [],
            priority=item.get("优先级"),
            due_at=_iso(item.get("截止报价日期")),
            inquiry_no=str(item.get("询价单号") or "") or None,
            product_requirement=requirement,
            mpn=_text(item.get("询价MPN")),
            manual_pn=_text(item.get("手册件号")),
            product_scope=_text(item.get("产品范围")),
            required_certificates=required_certificates(requirement),
            evidence={
                "file": source_files[0] if source_files else "",
                "container": item.get("_container") or "attachment-xlsx",
                "row": item.get("_source_row"),
                "condition_raw": condition,
                "quantity_raw": str(item["Quantity"]),
                **({"unit_raw": item["_unit_raw"]} if item.get("_unit_raw") else {}),
                # Where each field physically came from, so any finding can point back
                # to email -> attachment -> sheet/page -> row -> column -> raw value.
                **({"attachment": item["_attachment"]} if item.get("_attachment") else {}),
                **({"sheet": item["_sheet"]} if item.get("_sheet") else {}),
                **({"pdf_line": item["_pdf_line"]} if item.get("_pdf_line") else {}),
                **({"qty_line": item["_qty_line"]} if item.get("_qty_line") else {}),
                **({"columns": {key: dict(value) for key, value in columns.items()}}
                   if columns else {}),
                # Model extraction provenance (perception.py): which block/row it read
                # and how sure it was, so the reviewer can re-check the exact cell.
                **({"extraction": dict(item["_extraction"])} if item.get("_extraction") else {}),
                "raw": {key: ("" if item.get(key) is None else str(item.get(key)))
                        for key in RAW_FIELDS if key in item},
            },
        ))
    return RFQ(rfq_number=rfq_number, evaluation_date=evaluation_date, lines=lines,
               source_files=source_files, customer=customer, subject=subject, urgency=urgency,
               requirements=list(requirements or []))


def rfq_from_inquiry_xlsx(path: Path, **kwargs) -> RFQ:
    rows = _load_sheet(path)[1]
    kwargs.setdefault("source_files", [path.name])
    return build_rfq(rows, **kwargs)
