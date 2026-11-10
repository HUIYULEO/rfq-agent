"""Render refer_doc/RFQ_SUBMISSION.md into a Word document for import into Feishu Docs.

The Markdown stays the single source of truth. This script only handles presentation:
cover block, real Heading 1/2 styles (so the receiving editor builds an outline),
banded tables, code blocks as shaded single-cell tables, and page numbers in the footer.

    python tools/build_doc_docx.py

Requires python-docx (present via the .deps cache or `pip install -e ".[documents]"`).
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "refer_doc" / "RFQ_SUBMISSION.md"
TARGET = ROOT / "docs" / "MatrixNEXT-RFQ-Agent-说明文档.docx"

INK = RGBColor(0x16, 0x20, 0x2B)
MUTED = RGBColor(0x5B, 0x6B, 0x7C)
ACCENT = RGBColor(0x1A, 0x4F, 0xA0)
# Default body font: PingFang SC (苹方), which is what the document is opened with on
# macOS. Override with --font for a machine that has a different Chinese typeface;
# nothing else in the file names a Chinese font, so no reader is left with a
# substitution they did not choose. Code is set to Courier New (ships with Windows
# and macOS).
DEFAULT_FONT = "PingFang SC"
MONO_FONT = "Courier New"
HEADER_FILL = "F2F5F9"
CODE_FILL = "F7F8FA"


def set_font(element, name: str) -> None:
    """Set the same typeface for Latin, East Asian and complex scripts."""
    r_pr = element.get_or_add_rPr()
    fonts = r_pr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        r_pr.append(fonts)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        fonts.attrib.pop(qn(attr), None)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(attr), name)


# ---------------------------------------------------------------- inline runs
INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def add_inline(paragraph, text: str, *, size: float | None = None, color: RGBColor | None = None) -> None:
    """**bold** and `code` inside a plain paragraph."""
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**"):
            run = paragraph.add_run(piece[2:-2])
            run.bold = True
        elif piece.startswith("`") and piece.endswith("`"):
            run = paragraph.add_run(piece[1:-1])
            run.font.name = MONO_FONT
            run.font.size = Pt((size or 10.5) - 0.5)
            run.font.color.rgb = ACCENT
            continue
        else:
            run = paragraph.add_run(piece)
        if size:
            run.font.size = Pt(size)
        if color:
            run.font.color.rgb = color


def shade(cell, fill: str) -> None:
    element = OxmlElement("w:shd")
    element.set(qn("w:val"), "clear")
    element.set(qn("w:color"), "auto")
    element.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(element)


def bottom_border(paragraph, color: str = "D8DEE6", size: int = 6) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "6")
    bottom.set(qn("w:color"), color)
    borders.append(bottom)
    p_pr.append(borders)


def page_field(paragraph) -> None:
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            field = OxmlElement("w:fldChar")
            field.set(qn("w:fldCharType"), kind)
            run._r.append(field)
        else:
            instr = OxmlElement("w:instrText")
            instr.set(qn("xml:space"), "preserve")
            instr.text = text
            run._r.append(instr)


# ---------------------------------------------------------------- md parsing
def blocks(lines: list[str]):
    """Yield (kind, payload) for the Markdown subset the document uses."""
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("```"):
            index += 1
            code: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            yield "code", code
            continue
        if stripped == "---":
            yield "rule", None
            index += 1
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            yield f"h{len(heading.group(1))}", heading.group(2).strip()
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[index + 1].strip()):
            rows = [stripped]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(lines[index].strip())
                index += 1
            yield "table", [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]
            continue
        if re.match(r"^[-*]\s+", stripped):
            items: list[str] = []
            while index < len(lines):
                current = lines[index]
                if re.match(r"^[-*]\s+", current.strip()):
                    items.append(re.sub(r"^[-*]\s+", "", current.strip()))
                elif current.startswith(("  ", "\t")) and current.strip() and items:
                    items[-1] += " " + current.strip()
                else:
                    break
                index += 1
            yield "bullets", items
            continue
        if re.match(r"^\d+\.\s+", stripped):
            items = []
            while index < len(lines):
                current = lines[index]
                if re.match(r"^\d+\.\s+", current.strip()):
                    items.append(re.sub(r"^\d+\.\s+", "", current.strip()))
                elif current.startswith(("  ", "\t")) and current.strip() and items:
                    items[-1] += " " + current.strip()
                else:
                    break
                index += 1
            yield "numbers", items
            continue
        if stripped.startswith(">"):
            quote: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            yield "quote", [line for line in quote if line]
            continue
        paragraph: list[str] = []
        while index < len(lines) and lines[index].strip() and not re.match(
                r"^(#{1,4}\s|```|\||[-*]\s|\d+\.\s|>|---$)", lines[index].strip()):
            paragraph.append(lines[index].strip())
            index += 1
        yield "paragraph", " ".join(paragraph)


# ---------------------------------------------------------------- rendering
def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Render the submission document as DOCX.")
    parser.add_argument("--font", default=DEFAULT_FONT,
                        help=f"中文字体名（默认 {DEFAULT_FONT}；Windows 可用「微软雅黑」）")
    parser.add_argument("--out", type=Path, default=TARGET, help="输出文件")
    args = parser.parse_args(argv)
    target: Path = args.out

    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    parsed = list(blocks(lines))

    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    for attr, value in (("left_margin", Cm(2.4)), ("right_margin", Cm(2.4)),
                        ("top_margin", Cm(2.4)), ("bottom_margin", Cm(2.2))):
        setattr(section, attr, value)

    normal = document.styles["Normal"]
    set_font(normal.element, args.font)
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = INK
    normal.paragraph_format.line_spacing = 1.45
    normal.paragraph_format.space_after = Pt(6)

    for name, size, before, after in (("Heading 1", 17, 20, 8), ("Heading 2", 13.5, 14, 5),
                                      ("Heading 3", 11.5, 10, 4)):
        style = document.styles[name]
        set_font(style.element, args.font)
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = INK
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    # ---- cover block: title, subtitle, meta, rule
    title_text = subtitle_text = meta_text = ""
    body = parsed
    if body and body[0][0] == "h1":
        title_text = body[0][1]
        body = body[1:]
    if body and body[0][0] == "paragraph" and body[0][1].startswith("**"):
        subtitle_text = body[0][1].strip("*")
        body = body[1:]
    if body and body[0][0] == "paragraph" and body[0][1].startswith("文档版本"):
        meta_text = body[0][1]
        body = body[1:]

    title = document.add_paragraph()
    title.paragraph_format.space_before = Pt(60)
    title.paragraph_format.space_after = Pt(6)
    run = title.add_run(title_text)
    run.font.size = Pt(26)
    run.font.bold = True
    run.font.color.rgb = INK

    if subtitle_text:
        subtitle = document.add_paragraph()
        subtitle.paragraph_format.space_after = Pt(18)
        run = subtitle.add_run(subtitle_text)
        run.font.size = Pt(13)
        run.font.color.rgb = MUTED

    if meta_text:
        meta = document.add_paragraph()
        meta.paragraph_format.space_after = Pt(10)
        run = meta.add_run(meta_text.replace(" ｜ ", "　·　"))
        run.font.size = Pt(9.5)
        run.font.color.rgb = MUTED
        bottom_border(meta, color="1A4FA0", size=8)

    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    for kind, payload in body:
        if kind == "h2":
            document.add_heading(payload, level=1)
        elif kind == "h3":
            document.add_heading(payload, level=2)
        elif kind == "h4":
            document.add_heading(payload, level=3)
        elif kind == "h1":
            document.add_heading(payload, level=1)
        elif kind == "rule":
            continue
        elif kind == "paragraph":
            add_inline(document.add_paragraph(), payload)
        elif kind == "quote":
            for line in payload:
                paragraph = document.add_paragraph()
                paragraph.paragraph_format.left_indent = Cm(0.6)
                run = paragraph.add_run(line)
                run.font.size = Pt(9.5)
                run.italic = True
                run.font.color.rgb = MUTED
        elif kind in ("bullets", "numbers"):
            for position, item in enumerate(payload, start=1):
                paragraph = document.add_paragraph()
                paragraph.paragraph_format.left_indent = Cm(0.75)
                paragraph.paragraph_format.first_line_indent = Cm(-0.45)
                paragraph.paragraph_format.space_after = Pt(3)
                marker = "•　" if kind == "bullets" else f"{position}.　"
                run = paragraph.add_run(marker)
                run.font.color.rgb = MUTED
                add_inline(paragraph, item)
        elif kind == "code":
            table = document.add_table(rows=1, cols=1)
            table.alignment = WD_TABLE_ALIGNMENT.LEFT
            cell = table.rows[0].cells[0]
            shade(cell, CODE_FILL)
            cell.paragraphs[0].text = ""
            for offset, line in enumerate(payload):
                paragraph = cell.paragraphs[0] if offset == 0 else cell.add_paragraph()
                paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.line_spacing = 1.1
                run = paragraph.add_run(line)
                run.font.name = MONO_FONT
                run.font.size = Pt(8.5)
        elif kind == "table":
            header, rows = payload[0], payload[1:]
            table = document.add_table(rows=len(payload), cols=len(header))
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for column, text in enumerate(header):
                cell = table.rows[0].cells[column]
                shade(cell, HEADER_FILL)
                paragraph = cell.paragraphs[0]
                paragraph.paragraph_format.space_after = Pt(2)
                add_inline(paragraph, text, size=9)
                for run in paragraph.runs:
                    run.bold = True
            for row_index, row in enumerate(rows, start=1):
                for column, text in enumerate(row[:len(header)]):
                    paragraph = table.rows[row_index].cells[column].paragraphs[0]
                    paragraph.paragraph_format.space_after = Pt(2)
                    paragraph.paragraph_format.line_spacing = 1.15
                    add_inline(paragraph, text, size=9)
            document.add_paragraph().paragraph_format.space_after = Pt(2)

    # ---- footer: document name + page number
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run("MatrixNEXT RFQ Agent　·　说明文档　·　第 ")
    run.font.size = Pt(8)
    run.font.color.rgb = MUTED
    page_field(footer)
    run = footer.add_run(" 页")
    run.font.size = Pt(8)
    run.font.color.rgb = MUTED

    core = document.core_properties
    core.title = "MatrixNEXT RFQ Agent 说明文档"
    core.subject = "航空备件询价处理智能体"
    core.author = "MatrixNEXT"

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        document.save(target)
    except PermissionError:
        # The reader has the file open; write beside it instead of failing.
        alternate = target.with_name(f"{target.stem}-new{target.suffix}")
        document.save(alternate)
        print(f"{target.name} 被占用（可能在 Word / 飞书中打开），已写入 {alternate.name}")
        target = alternate
    print(f"wrote {target.relative_to(ROOT)} ({target.stat().st_size:,} bytes), font={args.font}")


if __name__ == "__main__":
    main()