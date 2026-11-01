"""Assemble the offline single-file diagram viewer from the .mmd + .svg pairs.

    python refer_doc/diagrams/build_page.py

Output: refer_doc/architecture-diagrams.html — every SVG is inlined (with the
kroki id prefix rewritten per file so the six <style> blocks do not collide),
so the page works with no network, no JavaScript, no external assets.
"""
from __future__ import annotations

import datetime as dt
import html
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent / "architecture-diagrams.html"

PAGES = [
    ("01-architecture", "一 · 系统分层架构图",
     "接口层（web / cli / 对话 / Deep Agents 适配）→ 服务层 RFQService → 确定性引擎层 → "
     "解析与导出层 → 数据层，两侧是素材与 runtime 存储。箭头即真实调用方向；"
     "「数字唯一来源」在引擎层，模型永远在门外。"),
    ("02-dataflow", "二 · 数据流图",
     "从素材到产物的完整数据通道：.eml 四种载体归一为带 evidence 的 RFQ 行；"
     "参考表按 sha256 指纹一次性 sync 成 SQLite 快照；定价引擎纯函数出逐行结果；"
     "复核项与裁决构成闭环；三条出口——task.json、草案 JSON/DOCX、页面与模型视图。"),
    ("03-process-flow", "三 · 处理流（端到端管道）",
     "从收件到导出的完整管道：解析 → 建任务 → 分析 → 逐行定价 → 复核门 → "
     "（人工 / auto_draft 裁决循环，每次回答 revision+1 后局部重定价）→ 导出。"
     "全案无一行有价时收敛为一个 RI-TASK 整案问题；改数量则回炉 received 重跑。"),
    ("04-task-state", "四 · Task 状态机",
     "Task.status 的五种状态（received · analyzed · awaiting_review · draft_ready · exported）"
     "与迁移事件。awaiting_review 的自环承载「一问一答重定价」与「先落草案产物但状态不变」两种语义；"
     "auto_draft 只会按默认选项闭环，不会放宽任何约束。"),
    ("05-agent-workflow", "五 · Agent 工作流",
     "genesis.Session 的对话工具循环：模型每轮只能选 dispatch 的 7 个工具之一，"
     "工具答案全部来自确定性核心；载荷压缩 ≤6000 字节防 122 行轰炸，6 轮上限防死循环，"
     "未配置时降级为提示+按钮流程照常。右上角是 agent.py 的 Deep Agents 平行栈。"),
    ("06-review-loop", "六 · 复核循环时序图",
     "一次典型阻塞复核的时序：按钮直答与对话转答是两条等价入口，"
     "resolve 之后纯函数重放（未放松的行结果逐位一致），revision+1 进入导出。"
     "模型在链路里只负责转述选项，不生成任何数字。"),
]

TOUCHED = ("id=\"container", "#container")


def neutralise_ids(svg: str, token: str) -> str:
    """Give each inline SVG a unique id namespace (root, markers, url refs, CSS)."""
    svg = svg.replace('id="container', f'id="{token}_container')
    svg = svg.replace("#container", f"#{token}_container")
    return svg


def figure(stem: str, index: int) -> str:
    svg = (HERE / f"{stem}.svg").read_text(encoding="utf-8")
    svg = neutralise_ids(svg, f"k{index:02d}")
    source = html.escape((HERE / f"{stem}.mmd").read_text(encoding="utf-8"))
    return (
        f'    <figure id="{stem}">\n'
        f'      <div class="canvas">{svg}</div>\n'
        f"      <details>\n"
        f"        <summary>Mermaid 源码（可复制回 mmd 文件改后再渲染）</summary>\n"
        f'        <pre><code class="mermaid-src">{source}</code></pre>\n'
        f"      </details>\n"
        f"    </figure>\n"
    )


def main() -> None:
    nav = "\n".join(
        f'      <a href="#{stem}">{title.split("·")[1].strip()}</a>'
        for stem, title, _ in PAGES
    )
    sections = []
    for index, (stem, title, blurb) in enumerate(PAGES, start=1):
        sections.append(
            f'  <section>\n'
            f'    <h2>{title}</h2>\n'
            f'    <p class="blurb">{blurb}</p>\n'
            + figure(stem, index)
            + "  </section>\n"
        )
    stamp = dt.date.today().isoformat()
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>架构图集 · MatrixNEXT RFQ Agent</title>
<style>
  :root {{
    --ink: #1c2733; --muted: #5b6b7b; --line: #d9e1e8; --accent: #2f5f9e;
    --badge: #eef3f9; --paper: #f6f8fa; --card: #ffffff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; color: var(--ink); background: var(--paper);
    font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 15px; line-height: 1.65;
  }}
  header {{
    background: linear-gradient(135deg, #1d3350 0%, #2f5f9e 60%, #3b74b8 100%);
    color: #fff; padding: 42px 24px 36px;
  }}
  header .inner {{ max-width: 1180px; margin: 0 auto; }}
  header h1 {{ margin: 0 0 10px; font-size: 26px; letter-spacing: 0.5px; }}
  header p {{ margin: 0; color: #cfe0f2; max-width: 62ch; }}
  header p.meta {{ margin-top: 14px; color: #9db8d6; font-size: 12.5px; }}
  nav {{
    max-width: 1180px; margin: -18px auto 0; padding: 0 24px;
    display: flex; flex-wrap: wrap; gap: 8px; position: relative;
  }}
  nav a {{
    background: var(--badge); border: 1px solid var(--line); color: var(--accent);
    text-decoration: none; padding: 6px 14px; border-radius: 999px; font-size: 13px;
  }}
  nav a:hover {{ border-color: var(--accent); }}
  main {{ max-width: 1180px; margin: 0 auto; padding: 18px 24px 60px; }}
  section {{
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 26px 26px 18px; margin: 22px 0;
  }}
  h2 {{
    font-size: 19px; margin: 0 0 6px; color: var(--accent);
    border-bottom: 1px solid var(--line); padding-bottom: 10px;
  }}
  p.blurb {{ color: var(--muted); margin: 8px 0 16px; }}
  figure {{ margin: 0; }}
  .canvas {{
    overflow-x: auto; border: 1px solid var(--line); border-radius: 8px;
    padding: 10px; background: #fff;
  }}
  .canvas svg {{ max-width: 100%; height: auto; display: block; margin: 0 auto; }}
  details {{
    margin-top: 12px; border-top: 1px dashed var(--line); padding-top: 10px;
  }}
  summary {{
    cursor: pointer; color: var(--muted); font-size: 13px; user-select: none;
  }}
  summary:hover {{ color: var(--accent); }}
  pre {{
    margin: 10px 0 0; padding: 14px; background: #0f1c2b; color: #d7e3ef;
    border-radius: 8px; overflow-x: auto; font-size: 12.5px; line-height: 1.5;
    font-family: Consolas, "Cascadia Mono", monospace;
  }}
  footer {{
    max-width: 1180px; margin: 0 auto; padding: 0 24px 40px;
    color: var(--muted); font-size: 12.5px;
  }}
  @media print {{
    header {{ padding: 24px; }}
    section {{ break-inside: avoid; padding: 16px; }}
    nav, details {{ display: none; }}
  }}
</style>
</head>
<body>
<header>
  <div class="inner">
    <h1>架构图集 · MatrixNEXT RFQ Agent</h1>
    <p>六张图看懂这个项目：系统怎么分层、数据怎么流动、一个询价案例怎么从收件走到导出、
       任务状态怎么迁移、对话层的模型在链路里到底做什么。</p>
    <p class="meta">渲染自 refer_doc/diagrams/*.mmd（kroki.io · Mermaid 11） · 本页完全离线可用 · 更新于 {stamp}</p>
  </div>
</header>
<nav>
{nav}
</nav>
<main>
{"".join(sections)}</main>
<footer>
  图示内容由代码事实推导（rfq_agent/*.py），数字口径与 <a href="RFQ_SOLUTION_DESIGN.md">方案设计</a> 一致：
  策略版本 demo-2026-11-01 · 默认加价 0.20 · 价格表 342 行 · 成本表 163 行（快照 snap-85782d2240bcbba5）。
  重新渲染：<code>python refer_doc/diagrams/render_svg.py</code> 再 <code>python refer_doc/diagrams/build_page.py</code>。
</footer>
</body>
</html>
"""
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT} ({len(page)} bytes)")


if __name__ == "__main__":
    main()
