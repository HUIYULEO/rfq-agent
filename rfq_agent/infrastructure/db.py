"""SQLite operational store for the reference data.

Excel stays the source of truth, but it is read exactly once per data snapshot:
`sync()` fingerprints the two workbook files, and if nothing changed it does no work.
Every request afterwards reads indexed rows from SQLite — never the workbooks.

    runtime/rfq.db
      snapshot(id, created_at, source_hash, price_rows, cost_rows, policy_version)
      price_row(snapshot_id, part_number, row_no, payload)
      cost_row (snapshot_id, part_number, row_no, payload)

Rows are stored as JSON payloads with a part_number column for indexing: the pricing
engine keeps its plain-dict contract, and lookups become `WHERE part_number IN (...)`
instead of a table scan.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id             TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    source_hash    TEXT NOT NULL,
    price_rows     INTEGER NOT NULL,
    cost_rows      INTEGER NOT NULL,
    policy_version TEXT NOT NULL,
    sources        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_row (
    snapshot_id TEXT NOT NULL,
    part_number TEXT NOT NULL,
    row_no      INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, row_no)
);
CREATE TABLE IF NOT EXISTS cost_row (
    snapshot_id TEXT NOT NULL,
    part_number TEXT NOT NULL,
    row_no      INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, row_no)
);
CREATE INDEX IF NOT EXISTS idx_price_pn ON price_row(snapshot_id, part_number);
CREATE INDEX IF NOT EXISTS idx_cost_pn  ON cost_row (snapshot_id, part_number);
CREATE TABLE IF NOT EXISTS mail (
    path      TEXT PRIMARY KEY,
    mtime     REAL NOT NULL,
    case_key  TEXT NOT NULL,
    customer  TEXT NOT NULL,
    subject   TEXT NOT NULL,
    received  TEXT NOT NULL,
    lines     INTEGER NOT NULL,
    container TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    path        TEXT PRIMARY KEY,
    imported_at TEXT NOT NULL,
    task_id     TEXT,
    stage       TEXT NOT NULL DEFAULT 'received'
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Approved quote history: written only by RFQService.activate_quote, after a named
-- human approved the revision. Drafts and supplier inquiries never land here.
CREATE TABLE IF NOT EXISTS quote_header (
    quote_id         TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    rfq_number       TEXT NOT NULL,
    customer         TEXT NOT NULL,
    currency         TEXT NOT NULL,
    disposition      TEXT NOT NULL,
    total_lines      INTEGER NOT NULL,
    priced_lines     INTEGER NOT NULL,
    subtotal_cny     TEXT NOT NULL,
    approved_by      TEXT NOT NULL,
    approved_at      TEXT NOT NULL,
    approval_mode    TEXT NOT NULL,
    policy_version   TEXT NOT NULL,
    data_snapshot_id TEXT NOT NULL,
    document         TEXT,
    UNIQUE (task_id, revision)
);
CREATE TABLE IF NOT EXISTS quote_line (
    quote_id      TEXT NOT NULL REFERENCES quote_header(quote_id),
    line_id       TEXT NOT NULL,
    line_number   INTEGER,
    part_number   TEXT NOT NULL,
    description   TEXT NOT NULL,
    quantity      TEXT NOT NULL,
    unit          TEXT NOT NULL,
    unit_price    TEXT NOT NULL,
    line_total    TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    certificate   TEXT,
    condition     TEXT,
    match_type    TEXT,
    PRIMARY KEY (quote_id, line_id)
);
CREATE INDEX IF NOT EXISTS idx_quote_line_pn ON quote_line(part_number);
"""

DATE_FIELDS = ("有效期至", "成本更新日期", "价格更新日期", "最近采购日期")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def _hash(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _dump(row: dict) -> str:
    clean = {}
    for key, value in row.items():
        if isinstance(value, (dt.date, dt.datetime)):
            clean[key] = {"__date__": value.isoformat(), "__time__": isinstance(value, dt.datetime)}
        elif value is None or isinstance(value, (str, int, float, bool)):
            clean[key] = value
        else:
            clean[key] = str(value)
    return json.dumps(clean, ensure_ascii=False)


def _load(payload: str) -> dict:
    row = json.loads(payload)
    for key, value in row.items():
        if not isinstance(value, dict) or "__date__" not in value:
            continue
        try:
            stamp = dt.datetime.fromisoformat(value["__date__"])
        except ValueError:
            row[key] = value["__date__"]
            continue
        row[key] = stamp if value.get("__time__") else stamp.date()
    return row


def latest(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()


def sync(connection: sqlite3.Connection, root: Path, *, force: bool = False) -> dict:
    """Ingest the workbooks into a snapshot. Returns what happened, not what to do."""
    from rfq_agent.application.cases import COST_TABLE, MATERIAL, PRICE_TABLE
    from rfq_agent.domain.policy import POLICY_VERSION
    from rfq_agent.ingestion.sources import load_cost_table, load_price_table

    price_path = root / MATERIAL / PRICE_TABLE
    cost_path = root / MATERIAL / COST_TABLE
    digest = _hash([price_path, cost_path])

    current = latest(connection)
    if current and current["source_hash"] == digest and not force:
        return {"changed": False, "snapshot_id": current["id"],
                "created_at": current["created_at"],
                "price_rows": current["price_rows"], "cost_rows": current["cost_rows"],
                "policy_version": current["policy_version"]}

    price_rows = load_price_table(price_path)
    cost_rows = load_cost_table(cost_path)
    snapshot_id = f"snap-{digest}"
    created = dt.datetime.now().isoformat(timespec="seconds")

    with connection:
        connection.execute("DELETE FROM price_row WHERE snapshot_id = ?", (snapshot_id,))
        connection.execute("DELETE FROM cost_row  WHERE snapshot_id = ?", (snapshot_id,))
        connection.executemany(
            "INSERT INTO price_row(snapshot_id, part_number, row_no, payload) VALUES (?,?,?,?)",
            [(snapshot_id, str(row.get("Part Number") or ""), index, _dump(row))
             for index, row in enumerate(price_rows, start=1)])
        connection.executemany(
            "INSERT INTO cost_row(snapshot_id, part_number, row_no, payload) VALUES (?,?,?,?)",
            [(snapshot_id, str(row.get("Part Number") or ""), index, _dump(row))
             for index, row in enumerate(cost_rows, start=1)])
        connection.execute(
            "INSERT OR REPLACE INTO snapshot(id, created_at, source_hash, price_rows, cost_rows,"
            " policy_version, sources) VALUES (?,?,?,?,?,?,?)",
            (snapshot_id, created, digest, len(price_rows), len(cost_rows), POLICY_VERSION,
             json.dumps([price_path.name, cost_path.name], ensure_ascii=False)))
    return {"changed": True, "snapshot_id": snapshot_id, "created_at": created,
            "price_rows": len(price_rows), "cost_rows": len(cost_rows),
            "policy_version": POLICY_VERSION}


# Same normalisation as compliance.normalize_pn, expressed in SQL (case/space/hyphen).
_NORMALIZED_PN = "REPLACE(REPLACE(UPPER(part_number), ' ', ''), '-', '')"


def rows_for(connection: sqlite3.Connection, snapshot_id: str, parts: set[str],
             normalized: set[str] | None = None) -> tuple[list, list]:
    """Only the rows this RFQ can possibly use — one query per table.

    ``parts`` are matched exactly (indexed); ``normalized`` keys additionally pick up
    rows whose part number differs only in case/space/hyphen, or that carry the
    line's MPN / manual part number. The pricing engine labels how each row matched.
    """
    if not parts and not normalized:
        return [], []
    exact = sorted(parts or [])
    keys = sorted(normalized or [])
    clauses, args = [], [snapshot_id]
    if exact:
        clauses.append(f"part_number IN ({','.join('?' for _ in exact)})")
        args += exact
    if keys:
        clauses.append(f"{_NORMALIZED_PN} IN ({','.join('?' for _ in keys)})")
        args += keys
    where = " OR ".join(clauses)
    price = [_load(row["payload"]) for row in connection.execute(
        f"SELECT payload FROM price_row WHERE snapshot_id = ? AND ({where}) ORDER BY row_no", args)]
    cost = [_load(row["payload"]) for row in connection.execute(
        f"SELECT payload FROM cost_row WHERE snapshot_id = ? AND ({where}) ORDER BY row_no", args)]
    return price, cost


def counts(connection: sqlite3.Connection, snapshot_id: str) -> dict:
    price = connection.execute("SELECT COUNT(*) FROM price_row WHERE snapshot_id = ?",
                               (snapshot_id,)).fetchone()[0]
    cost = connection.execute("SELECT COUNT(*) FROM cost_row WHERE snapshot_id = ?",
                              (snapshot_id,)).fetchone()[0]
    return {"price_rows": price, "cost_rows": cost}


# ---------- mail cache: listing the inbox must not re-parse six mails ----------

def cached_mail(connection: sqlite3.Connection, path: Path, mtime: float) -> dict | None:
    row = connection.execute("SELECT * FROM mail WHERE path = ? AND mtime = ?",
                             (str(path), mtime)).fetchone()
    return dict(row) if row else None


def remember_mail(connection: sqlite3.Connection, path: Path, mtime: float, case) -> None:
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO mail(path, mtime, case_key, customer, subject, received,"
            " lines, container) VALUES (?,?,?,?,?,?,?,?)",
            (str(path), mtime, case.key, case.customer, case.subject,
             case.received, case.lines, case.container))


# ---------- inbox: what the user has actually taken in ----------

def seeded(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone()
    return bool(row)


def mark_seeded(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('seeded', '1')")


def admit(connection: sqlite3.Connection, path: Path) -> None:
    with connection:
        connection.execute(
            "INSERT OR IGNORE INTO inbox(path, imported_at, stage) VALUES (?, ?, 'received')",
            (str(path), dt.datetime.now().isoformat(timespec="seconds")))


def inbox_rows(connection: sqlite3.Connection) -> dict[str, dict]:
    return {row["path"]: dict(row) for row in connection.execute("SELECT * FROM inbox")}


def set_stage(connection: sqlite3.Connection, path: Path, stage: str,
              task_id: str | None = None) -> None:
    with connection:
        connection.execute("UPDATE inbox SET stage = ? WHERE path = ?", (stage, str(path)))
        if task_id:
            connection.execute("UPDATE inbox SET task_id = ? WHERE path = ?", (task_id, str(path)))


def forget(connection: sqlite3.Connection, path: Path) -> None:
    with connection:
        connection.execute("DELETE FROM inbox WHERE path = ?", (str(path),))


def clear_inbox(connection: sqlite3.Connection) -> None:
    """Empty the inbox and drop the seed flag, so the next read re-seeds the example."""
    with connection:
        connection.execute("DELETE FROM inbox")
        # Run history would otherwise resurrect the tasks this reset is meant to
        # forget: inbox() promotes the newest remembered run back into task_id, so
        # "清空当前查看" (and a new visitor's reset) still showed 已有任务 / 已完成.
        connection.execute("CREATE TABLE IF NOT EXISTS inbox_runs "
                           "(path TEXT, task_id TEXT, PRIMARY KEY(path,task_id))")
        connection.execute("DELETE FROM inbox_runs")
        connection.execute("DELETE FROM meta WHERE key = 'seeded'")


# ---------- approved quote history ----------

def insert_quote(connection: sqlite3.Connection, header: dict, lines: list[dict]) -> None:
    """Insert one approved quote. The caller owns the transaction (no commit here)."""
    connection.execute(
        "INSERT INTO quote_header(quote_id, task_id, revision, rfq_number, customer, currency,"
        " disposition, total_lines, priced_lines, subtotal_cny, approved_by, approved_at,"
        " approval_mode, policy_version, data_snapshot_id, document)"
        " VALUES (:quote_id, :task_id, :revision, :rfq_number, :customer, :currency,"
        " :disposition, :total_lines, :priced_lines, :subtotal_cny, :approved_by, :approved_at,"
        " :approval_mode, :policy_version, :data_snapshot_id, :document)", header)
    connection.executemany(
        "INSERT INTO quote_line(quote_id, line_id, line_number, part_number, description,"
        " quantity, unit, unit_price, line_total, source_type, source_id, certificate,"
        " condition, match_type) VALUES (:quote_id, :line_id, :line_number, :part_number,"
        " :description, :quantity, :unit, :unit_price, :line_total, :source_type, :source_id,"
        " :certificate, :condition, :match_type)", lines)


def quote_history(connection: sqlite3.Connection, part_number: str | None = None) -> list[dict]:
    """Approved quote lines, newest first — the basis for later price comparison."""
    sql = ("SELECT h.quote_id, h.rfq_number, h.customer, h.approved_by, h.approved_at,"
           " h.approval_mode, l.* FROM quote_line l JOIN quote_header h USING (quote_id)")
    args: tuple = ()
    if part_number:
        sql += " WHERE l.part_number = ?"
        args = (part_number,)
    sql += " ORDER BY h.approved_at DESC, l.line_number"
    return [dict(row) for row in connection.execute(sql, args)]
