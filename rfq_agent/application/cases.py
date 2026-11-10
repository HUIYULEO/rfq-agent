"""The six official cases, discovered from the material itself.

No case table is hard-coded: every *.eml under data/询价信息素材 is ingested, and the
case letter comes from the buyer address (rfq-a@…). Adding a seventh mail needs no code
change, which is the point — the agent is driven by the material, not by a case list.
"""
from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from rfq_agent.ingestion.ingest import ingest_eml, read_eml
from rfq_agent.domain.policy import POLICY_VERSION
from rfq_agent.domain.pricing import SQLiteQuoteProvider

MATERIAL = Path("data/询价信息素材")
PRICE_TABLE = "价格资源表_演示数据.xlsx"
COST_TABLE = "内部物料成本表_演示数据.xlsx"
TEMPLATE = "报价单样例.docx"

BUYER = re.compile(r"rfq-([a-z])@", re.I)
UPLOADS = Path("runtime") / "uploads"
SEED = ("B",)             # the demo ships with one example; the rest the user admits


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
            from rfq_agent.infrastructure.db import connect

            connection = connect(DB_PATH(root))
        except (OSError, ValueError):
            connection = None

    cases: list[Case] = []
    uploads = (root / UPLOADS).resolve()
    for path in files:
        mtime = path.stat().st_mtime
        cached = None
        if connection is not None and not refresh:
            from rfq_agent.infrastructure.db import cached_mail

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
                from rfq_agent.infrastructure.db import remember_mail

                remember_mail(connection, path, mtime, case)
            except OSError:
                pass
    ordered = sorted(cases, key=lambda case: (len(case.key) > 1, case.key))
    _CACHE[cache_key] = ordered
    return ordered


def save_upload(root: Path, name: str, payload: bytes) -> Path:
    """Accept the user's own .eml: it becomes another case, same pipeline."""
    if not name.lower().endswith(".eml"):
        raise ValueError("目前只接受 .eml 询价邮件")
    target = root / UPLOADS
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_bytes(payload)
    discover(root, refresh=True)
    return path


PENDING = Path("runtime") / "uploads" / ".pending"


def stage_pending(root: Path, name: str, payload: bytes) -> tuple[str, Path, "RFQ"]:
    """Park an upload for preview: parse it, but admit nothing yet.

    The file lives outside the discover() scan path; only confirm_pending() turns it
    into a case. A cancelled preview leaves nothing but a temp file swept at boot.
    """
    if not name.lower().endswith(".eml"):
        raise ValueError("目前只接受 .eml 询价邮件")
    target = root / PENDING
    target.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = target / f"{token}.eml"
    staged.write_bytes(payload)
    try:
        rfq = ingest_eml(staged)
    except (ValueError, OSError):
        staged.unlink(missing_ok=True)
        raise
    (target / f"{token}.name").write_text(Path(name).name, encoding="utf-8")
    return token, staged, rfq


def confirm_pending(root: Path, token: str) -> Path:
    """Admit a staged preview into the material and rescan the inbox."""
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise ValueError("无效的预览令牌")
    staged = root / PENDING / f"{token}.eml"
    if not staged.exists():
        raise ValueError("预览已失效，请重新选择文件")
    digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    for case in discover(root):
        if hashlib.sha256(case.path.read_bytes()).hexdigest() == digest:
            staged.unlink()
            (root / PENDING / f"{token}.name").unlink(missing_ok=True)
            return case.path
    name_file = root / PENDING / f"{token}.name"
    name = name_file.read_text(encoding="utf-8").strip() if name_file.exists() else "upload.eml"
    name = Path(name).name or "upload.eml"
    if not name.lower().endswith(".eml"):
        name += ".eml"
    target_dir = root / UPLOADS
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / name
    if path.exists():
        path = target_dir / f"{path.stem}-{token[:6]}{path.suffix}"
    staged.replace(path)
    name_file.unlink(missing_ok=True)
    discover(root, refresh=True)
    return path


def sweep_pending(root: Path) -> int:
    """Drop previews nobody confirmed (server restart, closed tab)."""
    directory = root / PENDING
    if not directory.is_dir():
        return 0
    removed = 0
    for orphan in directory.iterdir():
        orphan.unlink(missing_ok=True)
        removed += 1
    return removed


def provider_for(root: Path) -> SQLiteQuoteProvider:
    """SQLite-backed: the workbooks are ingested once per snapshot, then queried."""
    from rfq_agent.infrastructure.db import connect, sync

    connection = connect(DB_PATH(root))
    info = sync(connection, root)
    return SQLiteQuoteProvider(connection, info["snapshot_id"])


def data_status(root: Path) -> dict:
    """What the reference data currently is — shown in the UI so it is never a guess."""
    from rfq_agent.infrastructure.db import connect, counts, latest, sync

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


def case_by_key(root: Path, key: str) -> Case:
    wanted = key.strip().upper()
    for case in discover(root):
        if case.key.upper() == wanted:
            return case
    raise ValueError(f"素材里没有案例 {key}")


# ---------- inbox: the demo ships small, the user admits the rest ----------

def _inbox_db(root: Path):
    from rfq_agent.infrastructure.db import admit, connect, inbox_rows, mark_seeded, seeded, set_stage

    connection = connect(DB_PATH(root))
    return connection, admit, inbox_rows, mark_seeded, seeded, set_stage


def inbox(root: Path) -> list[dict]:
    """Cases the user has admitted. First run seeds two examples, not all six."""
    connection, admit, inbox_rows, mark_seeded, seeded, _ = _inbox_db(root)
    if not seeded(connection):
        for key in SEED:
            try:
                admit(connection, case_by_key(root, key).path)
            except ValueError:
                continue
        mark_seeded(connection)
    rows = inbox_rows(connection)
    by_path = {case.path.resolve(): case for case in discover(root)}
    items = []
    for path_str, row in rows.items():
        case = by_path.get(Path(path_str).resolve())
        if case is None:
            continue
        items.append({"case": case, "stage": row["stage"], "task_id": row["task_id"],
                      "imported_at": row["imported_at"]})
    items.sort(key=lambda item: (len(item["case"].key) > 1, item["case"].key))
    connection.execute("CREATE TABLE IF NOT EXISTS inbox_runs (path TEXT, task_id TEXT, PRIMARY KEY(path,task_id))")
    with connection:
        for item in items:
            if item["task_id"]:
                connection.execute("INSERT OR IGNORE INTO inbox_runs VALUES (?,?)", (str(item["case"].path), item["task_id"]))
    groups = {}
    for item in items:
        digest = hashlib.sha256(item["case"].path.read_bytes()).hexdigest()
        group = groups.setdefault(digest, {**item, "history": []})
        for record in connection.execute("SELECT task_id FROM inbox_runs WHERE path=?", (str(item["case"].path),)):
            tid = record[0]
            if any(h["task_id"] == tid for h in group["history"]):
                continue
            path = root / "runtime" / tid / "desk.json"
            if not path.exists():
                continue
            task = json.loads(path.read_text(encoding="utf-8"))
            at = next((e.get("at", "") for e in task.get("agent_trace", []) if e.get("action") == "task_created"), "")
            group["history"].append({"task_id": tid, "created_at": at, "revision": task.get("revision"), "status": task.get("status")})
    for group in groups.values():
        group["history"].sort(key=lambda h: h["created_at"], reverse=True)
        if group["history"]:
            latest = group["history"][0]
            group["task_id"] = latest["task_id"]
            group["stage"] = "draft" if latest["status"] in {"exported", "active"} else "priced"
        elif group["task_id"] and not _task_exists(root, group["task_id"]):
            # The row still remembers a task whose files are gone: the container was
            # recycled (a free PaaS keeps no disk) or the directory was cleaned up by
            # hand. Reporting that id makes the desk offer "打开已有任务" and then fail
            # with an empty screen, so forget it and go back to "not run yet".
            group["task_id"] = None
            group["stage"] = "received"
    return list(groups.values())


def _task_exists(root: Path, task_id: str) -> bool:
    """A task is real only while its snapshot is on this instance's disk."""
    directory = root / "runtime" / task_id
    return (directory / "task.json").exists() or (directory / "desk.json").exists()


def available(root: Path) -> list[Case]:
    """Material that has not been admitted yet — what the user may still pull in."""
    taken = {hashlib.sha256(item["case"].path.read_bytes()).hexdigest() for item in inbox(root)}
    return [case for case in discover(root) if hashlib.sha256(case.path.read_bytes()).hexdigest() not in taken]


def admit_case(root: Path, key: str) -> Case:
    case = case_by_key(root, key)
    connection, admit, *_ = _inbox_db(root)
    admit(connection, case.path)
    return case


def reset_inbox(root: Path) -> None:
    """Back to first-run state: nothing admitted but the seeded example.

    Uploaded files stay on disk (they are the user's own material); only the inbox
    and its stage markers are cleared, so the next demo starts from the gate.
    """
    from rfq_agent.infrastructure.db import clear_inbox, connect

    connection = connect(DB_PATH(root))
    clear_inbox(connection)
    inbox(root)


def mark_stage(root: Path, key: str, stage: str, task_id: str | None = None) -> None:
    case = case_by_key(root, key)
    connection, _, _, _, _, set_stage = _inbox_db(root)
    connection.execute("CREATE TABLE IF NOT EXISTS inbox_runs (path TEXT, task_id TEXT, PRIMARY KEY(path,task_id))")
    with connection:
        connection.execute("INSERT OR IGNORE INTO inbox_runs SELECT path,task_id FROM inbox WHERE path=? AND task_id IS NOT NULL", (str(case.path),))
        if task_id:
            connection.execute("INSERT OR IGNORE INTO inbox_runs VALUES (?,?)", (str(case.path),task_id))
    set_stage(connection, case.path, stage, task_id)


def case_path(root: Path, key: str) -> Path:
    for case in discover(root):
        if case.key == key.strip() or case.key.upper() == key.strip().upper():
            return case.path
    raise ValueError(f"素材里没有案例 {key}")


def template_for(root: Path) -> Path:
    return root / MATERIAL / TEMPLATE
