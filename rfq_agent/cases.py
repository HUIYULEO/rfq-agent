"""The six official cases, discovered from the material itself.

No case table is hard-coded: every *.eml under data/询价信息素材 is ingested, and the
case letter comes from the buyer address (rfq-a@…). Adding a seventh mail needs no code
change, which is the point — the agent is driven by the material, not by a case list.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .ingest import ingest_eml, read_eml
from .policy import POLICY_VERSION
from .pricing import SQLiteQuoteProvider

MATERIAL = Path("data/询价信息素材")
PRICE_TABLE = "价格资源表_演示数据.xlsx"
COST_TABLE = "内部物料成本表_演示数据.xlsx"
TEMPLATE = "报价单样例.docx"

BUYER = re.compile(r"rfq-([a-z])@", re.I)
UPLOADS = Path("runtime") / "uploads"


def DB_PATH(root: Path) -> Path:
    return root / "runtime" / "rfq.db"

_CACHE: dict[tuple[str, float], list[Case]] = {}


def _mail_files(root: Path) -> list[Path]:
    files = list((root / MATERIAL).rglob("*.eml"))
    files += sorted((root / UPLOADS).glob("*.eml")) if (root / UPLOADS).is_dir() else []
    return files


@dataclass
class Case:
    key: str          # "A".."F", from the buyer address
    path: Path        # the .eml the agent is given
    label: str        # buyer + line count
    container: str    # where the table lives inside the mail
    customer: str = ""
    subject: str = ""
    received: str = ""
    lines: int = 0


def discover(root: Path, *, refresh: bool = False) -> list[Case]:
    """List the inbox without re-parsing mail that has not changed.

    Two cache layers: this process (by newest mtime) and SQLite (by file mtime), so a
    server restart lists the inbox in milliseconds and only new mail gets ingested.
    """
    files = _mail_files(root)
    stamp = max((path.stat().st_mtime for path in files), default=0.0)
    cache_key = (str(root.resolve()), stamp)
    if not refresh and cache_key in _CACHE:
        return _CACHE[cache_key]

    connection = None
    if files:
        try:
            from .db import connect

            connection = connect(DB_PATH(root))
        except (OSError, ValueError):
            connection = None

    cases: list[Case] = []
    uploads = (root / UPLOADS).resolve()
    for path in files:
        mtime = path.stat().st_mtime
        cached = None
        if connection is not None and not refresh:
            from .db import cached_mail

            cached = cached_mail(connection, path, mtime)
        if cached:
            cases.append(Case(key=cached["case_key"], path=path,
                              label=f"{cached['customer']} · {cached['lines']} 行",
                              container=cached["container"], customer=cached["customer"],
                              subject=cached["subject"], received=cached["received"],
                              lines=cached["lines"]))
            continue
        try:
            mail = read_eml(path)
            rfq = ingest_eml(path)
        except (ValueError, OSError):
            continue
        match = BUYER.search(mail.sender)
        if uploads in path.resolve().parents:
            key = f"UP-{path.stem}"          # never collides with an official case letter
        else:
            key = match.group(1).upper() if match else path.stem
        container = rfq.lines[0].evidence.get("container", "") if rfq.lines else ""
        case = Case(key=key, path=path, container=container,
                    label=f"{rfq.customer} · {len(rfq.lines)} 行",
                    customer=rfq.customer, subject=rfq.subject or mail.subject,
                    received=str(rfq.evaluation_date or ""),
                    lines=len(rfq.lines))
        cases.append(case)
        if connection is not None:
            try:
                from .db import remember_mail

                remember_mail(connection, path, mtime, case)
            except OSError:
                pass
    ordered = sorted(cases, key=lambda case: (len(case.key) > 1, case.key))
    _CACHE[cache_key] = ordered
    return ordered


def save_upload(root: Path, name: str, payload: bytes) -> Path:
    """Accept a judge's own .eml: it becomes another case, same pipeline."""
    if not name.lower().endswith(".eml"):
        raise ValueError("目前只接受 .eml 询价邮件")
    target = root / UPLOADS
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_bytes(payload)
    discover(root, refresh=True)
    return path


def provider_for(root: Path) -> SQLiteQuoteProvider:
    """SQLite-backed: the workbooks are ingested once per snapshot, then queried."""
    from .db import connect, sync

    connection = connect(DB_PATH(root))
    info = sync(connection, root)
    return SQLiteQuoteProvider(connection, info["snapshot_id"])


def data_status(root: Path) -> dict:
    """What the reference data currently is — shown in the UI so it is never a guess."""
    from .db import connect, counts, latest, sync

    connection = connect(DB_PATH(root))
    info = sync(connection, root)
    row = latest(connection)
    return {"snapshot_id": info["snapshot_id"], "created_at": info["created_at"],
            "policy_version": info["policy_version"], "changed": info["changed"],
            "price_rows": info["price_rows"], "cost_rows": info["cost_rows"],
            "sources": json.loads(row["sources"]) if row else [],
            "db": str(DB_PATH(root).relative_to(root)) if DB_PATH(root).is_relative_to(root)
                  else str(DB_PATH(root)),
            "live_counts": counts(connection, info["snapshot_id"])}


def load_case(root: Path, key: str):
    wanted = key.strip()
    for case in discover(root):
        if case.key == wanted or case.key.upper() == wanted.upper():
            return ingest_eml(case.path), provider_for(root)
    available = ", ".join(case.key for case in discover(root))
    raise ValueError(f"素材里没有案例 {key}；可用的是 {available}")


def case_path(root: Path, key: str) -> Path:
    for case in discover(root):
        if case.key == key.strip() or case.key.upper() == key.strip().upper():
            return case.path
    raise ValueError(f"素材里没有案例 {key}")


def template_for(root: Path) -> Path:
    return root / MATERIAL / TEMPLATE