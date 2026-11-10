"""Render refer_doc/RFQ_SUBMISSION.md into a standalone HTML page for GitHub Pages.

The submission document is authored in Markdown; this script produces the public
``docs/index.html`` from it so there is exactly one source of truth. No network, no
external fonts or scripts: the page is a single self-contained file.

    python tools/build_doc_site.py

Requires markdown-it-py (present in the development environment). The generated page
is committed, so publishing never depends on this script being run.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "refer_doc" / "RFQ_SUBMISSION.md"
TARGET = ROOT / "docs" / "index.html"

TITLE = "MatrixNEXT RFQ Agent｜说明文档"

CSS = """
:root{
  --ink:#16202b; --muted:#5b6b7c; --line:#e3e8ee; --soft:#f6f8fa;
  --accent:#1a4fa0; --accent-soft:#eef3fb; --warn:#8a5a00; --warn-soft:#fdf5e6;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:#fff; color:var(--ink);
  font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",
       "Microsoft YaHei","Source Han Sans SC","Noto Sans CJK SC",sans-serif;
  font-feature-settings:"kern" 1;
}
.layout{display:grid; grid-template-columns:264px minmax(0,1fr); gap:0; max-width:1240px; margin:0 auto}
nav.toc{
  position:sticky; top:0; align-self:start; height:100vh; overflow:auto;
  padding:34px 22px 40px 26px; border-right:1px solid var(--line); background:var(--soft);
}
nav.toc .brand{font-weight:700; font-size:14px; letter-spacing:.02em; margin-bottom:4px}
nav.toc .brand span{display:block; font-weight:500; color:var(--muted); font-size:12px; margin-top:4px}
nav.toc ol{list-style:none; margin:18px 0 0; padding:0; font-size:13.5px}
nav.toc li{margin:0 0 2px}
nav.toc li a{
  display:block; padding:5px 8px; color:var(--muted); text-decoration:none;
  border-left:2px solid transparent; border-radius:0 4px 4px 0;
}
nav.toc li a:hover{color:var(--accent); background:var(--accent-soft)}
nav.toc li.lvl3 a{padding-left:22px; font-size:12.8px}
nav.toc li a.active{color:var(--accent); border-left-color:var(--accent); background:var(--accent-soft); font-weight:600}
main{padding:46px 56px 96px; min-width:0}
header.doc-head{border-bottom:1px solid var(--line); padding-bottom:22px; margin-bottom:34px}
header.doc-head .kicker{color:var(--accent); font-size:12.5px; font-weight:650; letter-spacing:.08em; text-transform:uppercase}
header.doc-head h1{font-size:31px; line-height:1.3; margin:10px 0 6px; letter-spacing:-.01em}
header.doc-head p.sub{margin:0; color:var(--muted); font-size:13.5px}
header.doc-head .meta{display:flex; flex-wrap:wrap; gap:8px 18px; margin-top:14px; font-size:12.5px; color:var(--muted)}
header.doc-head .cta{display:inline-flex; align-items:center; gap:8px; margin-top:16px; padding:9px 16px;
  background:var(--accent); color:#fff; border-radius:6px; text-decoration:none; font-size:13.5px; font-weight:600}
header.doc-head .cta:hover{background:#153f81}
header.doc-head .cta code{background:rgba(255,255,255,.18); border-color:rgba(255,255,255,.28); color:#eaf1fb}
h2{font-size:22px; margin:52px 0 14px; padding-top:12px; border-top:1px solid var(--line); letter-spacing:-.005em}
h3{font-size:17px; margin:30px 0 10px}
p{margin:12px 0}
a{color:var(--accent)}
strong{font-weight:650}
code{font-family:var(--mono); font-size:.88em; background:var(--soft); border:1px solid var(--line);
     border-radius:4px; padding:1px 5px; word-break:break-word}
pre{background:#0f1721; color:#e6edf5; padding:16px 18px; border-radius:8px; overflow:auto; font-size:12.8px; line-height:1.65}
pre code{background:none; border:0; padding:0; color:inherit; font-size:1em}
table{border-collapse:collapse; width:100%; margin:18px 0; font-size:13.6px; display:block; overflow-x:auto}
thead th{text-align:left; background:var(--soft); border-bottom:2px solid var(--line); font-weight:650; white-space:nowrap}
th,td{border:1px solid var(--line); padding:8px 11px; vertical-align:top}
tbody tr:nth-child(even){background:#fbfcfd}
ul,ol{margin:12px 0; padding-left:22px}
li{margin:6px 0}
hr{border:0; border-top:1px solid var(--line); margin:34px 0}
blockquote{margin:18px 0; padding:12px 16px; background:var(--warn-soft); border-left:3px solid #d8b25e; color:#4a3a12; border-radius:0 6px 6px 0}
blockquote p{margin:6px 0}
footer.doc-foot{margin-top:64px; padding-top:20px; border-top:1px solid var(--line); color:var(--muted); font-size:12.5px}
@media (max-width:920px){
  .layout{grid-template-columns:1fr}
  nav.toc{position:static; height:auto; border-right:0; border-bottom:1px solid var(--line); padding:22px 20px}
  nav.toc ol{columns:2; column-gap:18px}
  main{padding:30px 20px 64px}
  header.doc-head h1{font-size:25px}
  h2{font-size:20px}
}
@media print{
  nav.toc{display:none}
  .layout{display:block; max-width:none}
  main{padding:0}
  h2{page-break-after:avoid}
  table,pre,blockquote{page-break-inside:avoid}
  a{color:inherit; text-decoration:none}
}
"""

SCRIPT = """
(function(){
  var links = Array.prototype.slice.call(document.querySelectorAll('nav.toc a[href^="#"]'));
  var targets = links.map(function(a){ return document.getElementById(a.getAttribute('href').slice(1)); });
  function sync(){
    var y = window.scrollY + 96, active = 0;
    targets.forEach(function(el, i){ if (el && el.getBoundingClientRect().top + window.scrollY <= y) active = i; });
    links.forEach(function(a, i){ a.classList.toggle('active', i === active); });
  }
  window.addEventListener('scroll', sync, {passive:true});
  window.addEventListener('resize', sync);
  sync();
})();
"""

SLUG = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
DEMO_URL = "https://rfq-agent-xk2m.onrender.com/"


def slugify(text: str, seen: dict[str, int]) -> str:
    words = re.sub(r"<[^>]+>", "", text)
    base = SLUG.sub("-", words.strip().lower()).strip("-") or "section"
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if not count else f"{base}-{count}"


def build(rendered: str) -> tuple[str, str]:
    """Add heading ids, collect the table of contents, and return (body, toc)."""
    toc: list[tuple[int, str, str]] = []
    counter = {"n": 0}

    def add_id(match: re.Match) -> str:
        level, attrs, text = int(match.group(1)), match.group(2), match.group(3)
        counter["n"] += 1
        anchor = f"sec-{counter['n']}"
        if level in (2, 3):
            toc.append((level, anchor, re.sub(r"<[^>]+>", "", text).strip()))
        return f'<h{level}{attrs} id="{anchor}">{text}</h{level}>'

    body = re.sub(r"<h([23])([^>]*)>(.*?)</h\1>", add_id, rendered, flags=re.S)
    items = []
    for level, anchor, label in toc:
        cls = "lvl3" if level == 3 else "lvl2"
        items.append(f'<li class="{cls}"><a href="#{anchor}">{html.escape(label)}</a></li>')
    return body, "\n".join(items)


def main() -> None:
    from markdown_it import MarkdownIt

    source = SOURCE.read_text(encoding="utf-8")
    md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    rendered = md.render(source)
    body, toc = build(rendered)

    # The first H1 becomes the page header; the redundant subtitle paragraph right
    # after it is dropped (the header already carries the positioning line).
    header = ""
    match = re.search(r"<h1[^>]*>(.*?)</h1>", body, flags=re.S)
    if match:
        header = match.group(1)
        body = body[match.end():].lstrip()
    subtitle = re.match(r"<p><strong>.*?</strong></p>\s*", body, flags=re.S)
    if subtitle and "说明文档" in subtitle.group(0):
        body = body[subtitle.end():]

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(TITLE)}</title>
<meta name="description" content="MatrixNEXT RFQ Agent：航空备件询价处理智能体说明文档">
<style>{CSS}</style>
</head>
<body>
<div class="layout">
<nav class="toc" aria-label="目录">
  <div class="brand">MatrixNEXT RFQ Agent<span>说明文档 · 目录</span></div>
  <ol>{toc}</ol>
</nav>
<main>
  <header class="doc-head">
    <div class="kicker">命题赛道 · 参赛作品说明文档</div>
    <h1>{header}</h1>
    <p class="sub">航空备件询价处理智能体：四种输入、确定性定价、人工闸门、客户模板草案。</p>
    <a class="cta" href="{DEMO_URL}" target="_blank" rel="noopener">打开可运行智能体 <code>{DEMO_URL.rstrip('/')}</code></a>
  </header>
  <article>
{body}
  </article>
  <footer class="doc-foot">本页由 <code>refer_doc/RFQ_SUBMISSION.md</code> 生成，内容以源文件为准。</footer>
</main>
</div>
<script>{SCRIPT}</script>
</body>
</html>
"""
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(page, encoding="utf-8")
    (TARGET.parent / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(page):,} bytes)")


if __name__ == "__main__":
    main()