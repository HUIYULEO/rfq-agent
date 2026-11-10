"""Port the perception layer onto the pr-2 checkout (six-step desk).

Run from the workspace; it edits the target repo in place. Every anchor is asserted to
appear exactly once, so a silent mismatch fails loudly instead of half-patching.
"""
import shutil
import sys
from pathlib import Path

SRC = Path(r"F:\Codex\MatrixNEXT\rfq-agent")
DST = Path(r"F:\Codex\2026-11-03\https-github-com-huiyuleo-rfq-agent\work\rfq-agent")


def patch(rel: str, pairs: list[tuple[str, str]]) -> None:
    path = DST / rel
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"{rel}: anchor appears {count} times, expected 1:\n{old[:120]}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"patched {rel}")


# 1. new files, straight copies
for rel in ("rfq_agent/perception.py", "tests/test_perception.py"):
    shutil.copyfile(SRC / rel, DST / rel)
    print(f"copied  {rel}")

# 2. ingest.py — expose raw blocks
patch("rfq_agent/ingest.py", [(
    '''def _normalise_header(cell: str) -> str | None:
    return HEADER_ALIASES.get(re.sub(r"\\s+", " ", cell).strip().lower())''',
    '''def _normalise_header(cell: str) -> str | None:
    return HEADER_ALIASES.get(re.sub(r"\\s+", " ", cell).strip().lower())


# ---------- raw access for the perception layer ----------
# ingest_eml() picks ONE container and normalises it. Perception needs the opposite:
# every raw block, unnormalised, so the model can see what the code could not map.

def html_rows(markup: str) -> list[list[str]]:
    """Every row of the first table, as printed — headers are not interpreted."""
    parser = _TableParser()
    parser.feed(markup)
    return parser.rows


def xlsx_sheets(data: bytes) -> list[tuple[str, list[list[str]]]]:
    """Every worksheet, so a cover sheet in position 1 does not hide the real table."""
    from .deps import require

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
    return sheets''',
)])

# 3. sources.py — carry extraction provenance into the line evidence
patch("rfq_agent/sources.py", [(
    '''                **({"columns": {key: dict(value) for key, value in columns.items()}}
                   if columns else {}),''',
    '''                **({"columns": {key: dict(value) for key, value in columns.items()}}
                   if columns else {}),
                # Model extraction provenance (perception.py): which block/row it read
                # and how sure it was, so the reviewer can re-check the exact cell.
                **({"extraction": dict(item["_extraction"])} if item.get("_extraction") else {}),''',
)])

# 4. models.py — persist the understanding report on the task
patch("rfq_agent/models.py", [(
    '''    agent_trace: list[dict] = field(default_factory=list)

    def to_dict(self):''',
    '''    agent_trace: list[dict] = field(default_factory=list)
    # How the input was read (perception.py): blocks, confidence, refusals.
    extraction: dict = field(default_factory=dict)

    def to_dict(self):''',
), (
    '''        payload.setdefault("approval", {})
        return cls(**payload)''',
    '''        payload.setdefault("approval", {})
        payload.setdefault("extraction", {})
        return cls(**payload)''',
)])

# 5. service.py — record the understanding step on the trace
patch("rfq_agent/service.py", [(
    '''            self._trace(task, "rfq_validated", outcome="continue")
            self._save(task)
        return task.to_dict()

    # ---------- pricing gate ----------''',
    '''            self._trace(task, "rfq_validated", outcome="continue")
            self._save(task)
        return task.to_dict()

    def record_extraction(self, task_id: str, report: dict) -> dict:
        """Persist how the input was understood, before anything was priced.

        The report is the perception layer's audit trail (which blocks were read, what
        the model refused, which rows look misread). It is evidence, never a price
        input, so it lives on the trace and not in the results.
        """
        task = self.store.get(task_id)
        self._trace(task, "input_understood",
                    source=report.get("source", ""),
                    model=report.get("model", ""),
                    document_type=report.get("document_type", ""),
                    lines_model=report.get("lines_model", 0),
                    lines_deterministic=report.get("lines_deterministic", 0),
                    refused=len(report.get("refused") or []),
                    best_guess=len(report.get("best_guess") or []),
                    reason=report.get("reason", ""),
                    outcome="continue")
        task.extraction = report
        self._save(task)
        return task.to_dict()

    # ---------- pricing gate ----------''',
)])

# 6. web.py — the main path
patch("rfq_agent/web.py", [(
    "from . import analysis as analysis_report, quality, terms",
    "from . import analysis as analysis_report, perception, quality, terms",
), (
    "from .genesis import GenesisError, Session, load_config, missing_keys",
    "from .genesis import GenesisError, Session, load_config, missing_keys\n"
    "from .perception import PerceptionError",
), (
    "def _lines_view(rfq: dict) -> list[dict]:",
    '''def _understand(case: str):
    """Main path: the model reads the mail, code audits it, pricing still does the sums.

    Falls back to the deterministic parser when the model is unconfigured, unreachable
    or refuses the extraction — a dead model must never dead-end the demo. Which path
    actually ran is written to the task trace, so "the agent read this" is checkable
    rather than asserted.
    """
    path = case_by_key(ROOT, case).path
    config = load_config(ROOT)
    if missing_keys(config):
        rfq, _ = load_case(ROOT, case)
        return rfq, {"source": "deterministic", "model": "",
                     "reason": "模型未配置，使用确定性解析"}
    try:
        return perception.run(config, path)
    except (PerceptionError, GenesisError) as exc:
        rfq, _ = load_case(ROOT, case)
        return rfq, {"source": "deterministic", "model": config.get("GENESIS_MODEL", ""),
                     "reason": f"模型理解失败，回退确定性解析：{exc}"}


def _lines_view(rfq: dict) -> list[dict]:''',
), (
    '''    def _run(self, case: str, autonomy: str):
        rfq, _ = load_case(ROOT, case)
        state = _service().start_task(rfq.to_dict(), autonomy=autonomy)''',
    '''    def _run(self, case: str, autonomy: str):
        rfq, extraction = _understand(case)
        state = _service().start_task(rfq.to_dict(), autonomy=autonomy)
        if extraction:
            state = _service().record_extraction(state["task_id"], extraction)''',
), (
    '''        "agent_trace": (task.get("agent_trace") or [])[-30:],''',
    '''        "agent_trace": (task.get("agent_trace") or [])[-30:],
        "extraction": task.get("extraction") or {},''',
)])

# 7. index.html — show what the agent understood, on the intake step
patch("rfq_agent/static/index.html", [(
    '''    task_created: "建立任务并锁定输入版本",''',
    '''    task_created: "建立任务并锁定输入版本",
    input_understood: "模型读邮件：判断类型、抽条款、标记可疑行",''',
), (
    '''    </div>${reqBlock}
    <p class="muted">来源 ${esc((S.source_files || []).join("、"))}</p>''',
    '''    </div>${reqBlock}${readingBlock()}
    <p class="muted">来源 ${esc((S.source_files || []).join("、"))}</p>''',
), (
    '''function paintIntake() {''',
    '''/* ---------- what the agent understood about the input ---------- */
function readingBlock() {
  const ex = (S && S.extraction) || {};
  if (!ex.mode) return "";
  const mode = ex.mode === "audit" ? "代码解析 + 模型审查" : "模型抽取（逐字段校验）";
  const issues = (ex.issues || []).filter((i) => i.kind !== "reconciled").slice(0, 5);
  const stats = [];
  if (ex.mode === "audit" && ex.lines_deterministic) stats.push(`${ex.lines_deterministic} 行由解析器读出`);
  if (ex.mode === "extract" && ex.lines_model) stats.push(`${ex.lines_model} 行由模型抽出并回指原文`);
  if (ex.requirements_added) stats.push(`新增 ${ex.requirements_added} 条客户要求`);
  if ((ex.refused || []).length) stats.push(`拒收 ${ex.refused.length} 行（未通过校验）`);
  return `<div class="reading"><div class="h">输入理解 · ${esc(mode)}</div>
    <div>${esc(ex.judgement || ex.reason || "")}</div>
    ${stats.length ? `<div class="muted" style="margin-top:3px">${esc(stats.join(" · "))}</div>` : ""}
    ${issues.length ? `<ul>${issues.map((i) => `<li>${esc(i.detail)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

function paintIntake() {''',
), (
    '''  .runstep{display:grid;''',
    '''  .reading{border:1px solid #ddd6fe;background:#f5f3ff;border-radius:8px;padding:9px 11px;
           margin-top:9px;font-size:12px;color:var(--ink2)}
  .reading .h{font-size:10.5px;color:var(--accent);font-weight:650;margin-bottom:3px}
  .reading ul{margin:5px 0 0;padding-left:16px;color:var(--faint);font-size:11px;line-height:1.55}
  .runstep{display:grid;''',
)])

print("\nport complete")