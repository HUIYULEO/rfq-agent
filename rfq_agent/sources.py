"""Read the official material into domain objects (no LLM involved)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .deps import require
from .models import RFQ, Line


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


def build_rfq(inquiry_rows: list[dict], *, rfq_number: str, evaluation_date: str,
              source_files: list[str], customer: str = "", subject: str = "",
              urgency: str = "NORMAL") -> RFQ:
    lines = []
    for number, item in enumerate(inquiry_rows, start=1):
        condition = str(item.get("件状条件") or "").strip()
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
            evidence={
                "file": source_files[0] if source_files else "",
                "container": item.get("_container") or "attachment-xlsx",
                "row": item.get("_source_row"),
                "condition_raw": condition,
                "quantity_raw": str(item["Quantity"]),
                **({"unit_raw": item["_unit_raw"]} if item.get("_unit_raw") else {}),
            },
        ))
    return RFQ(rfq_number=rfq_number, evaluation_date=evaluation_date, lines=lines,
               source_files=source_files, customer=customer, subject=subject, urgency=urgency)


def rfq_from_inquiry_xlsx(path: Path, **kwargs) -> RFQ:
    rows = _load_sheet(path)[1]
    kwargs.setdefault("source_files", [path.name])
    return build_rfq(rows, **kwargs)