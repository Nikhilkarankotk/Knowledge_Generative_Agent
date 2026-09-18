"""Generated-format exporters: TXT, MD, JSON, HTML, CSV, DOCX, PDF.

Every exporter renders an :class:`~app.export.registry.ExportPayload` into bytes.
These are the *generated* formats; native origl files never pass through here.
"""

from __future__ import annotations

import csv as _csv
import io
import json
import re
from html import unescape

from app.export.registry import Exporter, ExportPayload

# Stray HTML that occasionally survives in captured content (e.g. Confluence
# excerpts). Block tags become newlines, inline tags are dropped, entities decoded.
_HTML_BLOCK_TAG = re.compile(
    r"</?(?:p|div|br|h[1-6]|ul|ol|li|table|thead|tbody|tr|td|th|section|article)\b[^>]*>",
    re.IGNORECASE,
)
_ANY_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\b[^>]*>")


def _sanitize_html_residue(value: str) -> str:
    if not value or ("<" not in value and "&" not in value):
        return value or ""
    text = _HTML_BLOCK_TAG.sub("\n", value)
    text = _ANY_TAG.sub("", text)
    return unescape(text).replace("\xa0", " ")


def _sections_text(payload: ExportPayload) -> str:
    parts: list[str] = []
    for section in payload.sections:
        parts.append(section.heading)
        if section.evidence:
            parts.append(f"[{section.evidence}]")
        if section.rows:
            parts.append(_format_text_table(section.headers, section.rows))
        elif section.body:
            parts.append(_strip_markdown(section.body))
        parts.append("")
    return "\n".join(parts).strip()


_MD_HORIZONTAL_RULE = re.compile(r"^[\s*_\-—=]{3,}$")
_MD_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*$")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_MD_CODE = re.compile(r"`([^`]*)`")
_MD_EMPHASIS = re.compile(r"(?<!\*)\*([^*\s](?:[^*]*?[^*\s])?)\*(?!\*)")
_MD_ASTER_BULLET = re.compile(r"^(\s*)\*\s+", re.MULTILINE)


def _strip_markdown(value: str) -> str:
    """Remove markdown artifacts (``**``, ``***``, ``#``, accents, ``*`` bullets)

    so prose written by the LLM renders cleanly in every document format.
    """
    lines: list[str] = []
    for raw in _sanitize_html_residue(value or "").splitlines():
        if _MD_HORIZONTAL_RULE.match(raw):
            continue
        line = _MD_HEADING.sub(r"\1", raw)
        line = _MD_BOLD.sub(lambda m: m.group(1) or m.group(2), line)
        line = _MD_CODE.sub(r"\1", line)
        line = _MD_EMPHASIS.sub(r"\1", line)
        line = _MD_ASTER_BULLET.sub(r"\1- ", line)
        lines.append(line)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Shared markdown block model. Confluence pages arrive as markdown-ish text
# (see ``confluence_service.html_to_text``); the DOCX and PDF renderers both
# consume these blocks so headings, lists, tables and ASCII architecture
# diagrams are laid out professionally instead of as one wall of text.

_MD_PIPE_TABLE = re.compile(r"^\s*\|.*\|\s*$")
_MD_BULLET = re.compile(r"^(\s*)[-*+•]\s+(\S.*)$")
_MD_ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(\S.*)$")
_MD_CODE_FENCE = re.compile(r"^\s*```+\s*\w*\s*$")
# Confluence pages frequently use plain numbered headings ("1. Requirements",
# "1.2 Non-functional requirements", "2 High-Level Architecture") without any
# markdown '#'. Treat a short, title-cased line of that shape as a heading.
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+([A-Z][^\n]{2,90})$")
_BOX_DRAWING = "│─┌┐└┘├┤┬┴┼╭╮╰╯═║╔╗╚╝╠╣╦╩╬▲▼◄►◀▶→←↑↓⇒⇐↔"
_DIAGRAM_HINT = re.compile(
    r"[|+][-=]{2,}|[-=]{2,}[|+>]|-->|<--|──|[" + re.escape(_BOX_DRAWING) + r"]"
)


def _looks_like_diagram_line(line: str) -> bool:
    if not line.strip():
        return False
    if _DIAGRAM_HINT.search(line):
        return True
    return bool(re.match(r"^\s{4,}\S", line)) and bool(re.search(r"\s{2,}\S", line.strip()))


def _looks_like_numbered_heading(line: str) -> tuple[int, str] | None:
    """Return (level, text) when ``line`` reads like "1.2 Section Title"."""
    match = _NUMBERED_HEADING.match(line)
    if not match:
        return None
    number, title = match.group(1), match.group(2).strip()
    # A real sentence ("1. Register users, authenticate...") is not a heading.
    if title.endswith((".", ":", ";", ",")) or len(title.split()) > 12:
        return None
    level = min(number.count(".") + 1, 3)
    return level, f"{number} {title}"


def _is_md_separator_row(cells: list[str]) -> bool:
    filled = [cell for cell in cells if cell]
    return bool(filled) and all(re.match(r"^:?-+:?$", cell) for cell in filled)


def _looks_like_list_item(line: str) -> bool:
    """A short, single-sentence line that reads like one bullet of a list."""
    text = line.strip()
    if not text or len(text) > 220:
        return False
    # One sentence: at most one terminal punctuation mark, and it is at the end.
    body = text.rstrip(".;")
    if "." in body and not re.search(r"\d\.\d", body):
        return False
    return len(text.split()) <= 32


def parse_markdown_blocks(text: str | None) -> list[dict[str, object]]:
    """Parse markdown-ish ``text`` into ordered structural blocks.

    Block kinds: ``heading`` {level,text} | ``table`` {rows} | ``bullet`` /
    ``ordered`` {text, depth} | ``code`` {lines} | ``paragraph`` {text}.
    Consecutive prose lines are joined into a single paragraph.
    """
    lines = _sanitize_html_residue(text or "").splitlines()
    blocks: list[dict[str, object]] = []
    prose: list[str] = []

    def flush_prose() -> None:
        if not prose:
            return
        items = [p.strip() for p in prose if p.strip()]
        # Legacy captures flattened <li> items into bare consecutive lines. A run
        # of 2+ short, self-contained sentences directly under a heading reads as
        # a list, so render it as bullets instead of one run-on paragraph.
        if len(items) >= 2 and blocks and blocks[-1].get("kind") == "heading" and all(
            _looks_like_list_item(item) for item in items
        ):
            for item in items:
                blocks.append({"kind": "bullet", "text": item, "depth": 0})
        else:
            blocks.append({"kind": "paragraph", "text": " ".join(items)})
        prose.clear()

    index, count = 0, len(lines)
    while index < count:
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or _MD_HORIZONTAL_RULE.match(stripped):
            flush_prose()
            index += 1
            continue
        if re.fullmatch(r"#{1,6}", stripped) or re.fullmatch(r"[-*+]|\d+[.)]", stripped):
            index += 1
            continue
        if _MD_CODE_FENCE.match(stripped):
            flush_prose()
            index += 1
            code: list[str] = []
            while index < count and not _MD_CODE_FENCE.match(lines[index].strip()):
                code.append(lines[index])
                index += 1
            index += 1
            if any(line.strip() for line in code):
                blocks.append({"kind": "code", "lines": code})
            continue
        if _MD_PIPE_TABLE.match(raw):
            flush_prose()
            rows: list[list[str]] = []
            while index < count and _MD_PIPE_TABLE.match(lines[index]):
                cells = [c.strip() for c in lines[index].strip().strip("|").split("|")]
                rows.append(cells)
                index += 1
            data = [cells for cells in rows if not _is_md_separator_row(cells)]
            if data:
                blocks.append({"kind": "table", "rows": data})
            continue
        heading = _MD_HEADING.match(raw)
        if heading:
            flush_prose()
            level = min(len(raw) - len(raw.lstrip("#")), 6) or 1
            blocks.append({"kind": "heading", "level": level, "text": heading.group(1).strip()})
            index += 1
            continue
        if _looks_like_diagram_line(raw):
            flush_prose()
            block_lines: list[str] = []
            while index < count and (
                _looks_like_diagram_line(lines[index]) or (block_lines and lines[index].strip())
            ):
                block_lines.append(lines[index])
                index += 1
            if len(block_lines) >= 2:
                blocks.append({"kind": "code", "lines": block_lines})
            else:
                blocks.append({"kind": "paragraph", "text": block_lines[0].strip()})
            continue
        bullet = _MD_BULLET.match(raw)
        if bullet:
            flush_prose()
            blocks.append({"kind": "bullet", "text": bullet.group(2).strip(), "depth": len(bullet.group(1)) // 2})
            index += 1
            continue
        numbered = _looks_like_numbered_heading(raw)
        if numbered:
            flush_prose()
            blocks.append({"kind": "heading", "level": numbered[0], "text": numbered[1]})
            index += 1
            continue
        ordered = _MD_ORDERED.match(raw)
        if ordered:
            flush_prose()
            blocks.append({"kind": "ordered", "text": ordered.group(3).strip(), "depth": len(ordered.group(1)) // 2})
            index += 1
            continue
        prose.append(raw)
        index += 1
    flush_prose()
    return blocks


_INLINE_RUN = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def _inline_runs(text: str) -> list[tuple[str, bool, bool]]:
    """Split ``text`` into (chunk, bold, code) runs from ``**bold**`` / ```code```."""
    runs: list[tuple[str, bool, bool]] = []
    for part in _INLINE_RUN.split(text or ""):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            runs.append((part[2:-2], True, False))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            runs.append((part[1:-1], False, True))
        else:
            runs.append((_MD_EMPHASIS.sub(r"\1", part), False, False))
    return runs


def _format_text_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render ``headers``/``rows`` as aligned monospace columns (best effort)."""
    raw = [headers] + rows if headers else list(rows)
    if not raw:
        return ""
    max_total = 96
    column_count = max(len(line) for line in raw)
    caps: list[int] = []
    for col in range(column_count):
        values = [str(line[col]) for line in raw if col < len(line)]
        if not values:
            caps.append(8)
            continue
        width_hint = max(len(_collapsed(value)) for value in values)
        min_width = len(str(headers[col])) if headers and col < len(headers) else 4
        caps.append(max(width_hint, min_width + 1))
    # shrink the widest columns so the table fits on a readable line width
    while sum(caps) + 4 * (len(caps) - 1) > max_total and len(caps) > 1:
        widest = max(range(len(caps)), key=lambda i: caps[i])
        if caps[widest] <= 14:
            break
        caps[widest] = max(14, caps[widest] - 8)

    def cell(text: str, index: int) -> str:
        value = _collapsed(text)
        if len(value) > caps[index]:
            value = value[: caps[index] - 1] + "…"
        return value.ljust(caps[index])

    rendered: list[str] = []
    if headers:
        rendered.append("  " + "    ".join(cell(str(h), i) for i, h in enumerate(headers)))
        rendered.append("  " + "    ".join("-" * caps[i] for i in range(column_count)))
    for row in rows:
        padded = (row + [""] * column_count)[:column_count]
        rendered.append("  " + "    ".join(cell(str(v), i) for i, v in enumerate(padded)))
    return "\n".join(rendered)


def _collapsed(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


class TextExporter(Exporter):
    format_name = "txt"

    def render(self, payload: ExportPayload) -> bytes:
        lines: list[str] = []
        if payload.title:
            lines.append(payload.title)
        if payload.subtitle:
            lines.append(payload.subtitle)
        if payload.source_url:
            lines.append(f"Source: {payload.source_url}")
        if payload.content:
            lines.append(_strip_markdown(payload.content))
        sections = _sections_text(payload)
        if sections:
            lines.append(sections)
        text = "\n\n".join(filter(None, lines))
        return (text.strip() + "\n").encode("utf-8")


class MarkdownExporter(Exporter):
    format_name = "md"

    def render(self, payload: ExportPayload) -> bytes:
        lines: list[str] = []
        if payload.title:
            lines.append(f"# {payload.title}")
        if payload.subtitle:
            lines.append(f"_{payload.subtitle}_")
        if payload.source_url:
            lines.append(f"Source: [{payload.source_url}]({payload.source_url})")
        lines.append("")
        if payload.content:
            lines.append(_strip_markdown(payload.content))
            lines.append("")
        for section in payload.sections:
            lines.append(f"## {section.heading}")
            if section.evidence:
                lines.append(f"*{section.evidence}*")
            if section.rows:
                lines.append(_markdown_table(section.headers, section.rows))
            elif section.body:
                lines.append(_strip_markdown(section.body))
            lines.append("")
        return ("\n".join(lines).strip() + "\n").encode("utf-8")


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    text_rows = [headers] + rows if headers else list(rows)
    if not text_rows:
        return ""
    column_count = max(len(line) for line in text_rows)
    rendered: list[str] = []
    for row in text_rows:
        padded = (row + [""] * column_count)[:column_count]
        rendered.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in padded) + " |")
    if headers:
        rendered.insert(1, "| " + " | ".join("---" for _ in range(column_count)) + " |")
    return "\n".join(rendered)


class HtmlExporter(Exporter):
    format_name = "html"

    def render(self, payload: ExportPayload) -> bytes:
        body: list[str] = []
        if payload.title:
            body.append(f"<h1>{_escape(payload.title)}</h1>")
        if payload.subtitle:
            body.append(f"<p class='subtitle'>{_escape(payload.subtitle)}</p>")
        if payload.source_url:
            body.append(f"<p><a href='{_escape_url(payload.source_url)}'>Source</a></p>")
        if payload.content:
            body.append(f"<pre>{_escape(_strip_markdown(payload.content))}</pre>")
        for section in payload.sections:
            body.append(f"<h2>{_escape(section.heading)}</h2>")
            if section.evidence:
                body.append(f"<p class='evidence'>[{_escape(section.evidence)}]</p>")
            if section.rows:
                body.append(_html_table(section.headers, section.rows))
            elif section.body:
                body.append(f"<pre>{_escape(_strip_markdown(section.body))}</pre>")
        if payload.rows:
            body.append(_html_table(payload.headers, payload.rows))
        html = (
            "<!DOCTYPE html>\n<html><head><meta charset='utf-8'>"
            f"<title>{_escape(payload.title or 'Export')}</title></head>"
            "<body>" + "\n".join(body) + "</body></html>\n"
        )
        return html.encode("utf-8")


class JsonExporter(Exporter):
    format_name = "json"

    def render(self, payload: ExportPayload) -> bytes:
        document: dict = {
            "title": payload.title,
            "subtitle": payload.subtitle,
            "source_url": payload.source_url,
            "source_type": payload.source_type,
            "content": payload.content.strip(),
            "metadata": payload.metadata,
            "sections": [
                {
                    "heading": section.heading,
                    "evidence": section.evidence,
                    "body": _strip_markdown(section.body),
                    "headers": section.headers,
                    "rows": section.rows,
                }
                for section in payload.sections
            ],
        }
        if payload.rows:
            document["headers"] = payload.headers
            document["rows"] = payload.rows
        return json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


class CsvExporter(Exporter):
    format_name = "csv"

    def render(self, payload: ExportPayload) -> bytes:
        rows = list(payload.rows)
        if payload.headers:
            rows = [payload.headers] + rows
        if not rows and payload.content:
            rows = [line.split("\t") for line in payload.content.splitlines()]
        output = io.StringIO()
        writer = _csv.writer(output)
        writer.writerows(rows)
        return output.getvalue().encode("utf-8")


# --- DOCX theming helpers ---------------------------------------------------------

_DOCX_BODY_FONT = "Calibri"
_DOCX_CODE_FONT = "Consolas"
_DOCX_ACCENT = (0x1F, 0x3A, 0x5F)  # deep navy for headings/table header
_DOCX_MUTED = (0x59, 0x59, 0x59)
_DOCX_CODE_BG = "F2F2F2"
_DOCX_TABLE_HEADER_BG = "1F3A5F"
_DOCX_TABLE_ZEBRA_BG = "F7F9FC"


def _docx_rgb(rgb: tuple[int, int, int]):
    from docx.shared import RGBColor

    return RGBColor(*rgb)


def _docx_shade(cell_or_paragraph, hex_fill: str) -> None:
    """Apply a solid background fill to a table cell (or paragraph) via w:shd."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    element = cell_or_paragraph._element
    target = element.get_or_add_tcPr() if hasattr(element, "get_or_add_tcPr") else element.get_or_add_pPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), hex_fill)
    target.append(shading)


def _docx_horizontal_rule(document) -> None:
    """A thin accent-coloured rule under the title block (paragraph bottom border)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(2)
    paragraph.paragraph_format.space_after = Pt(10)
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "8")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), _DOCX_TABLE_HEADER_BG)
    borders.append(bottom)
    p_pr.append(borders)


def _docx_apply_theme(document, *, title: str) -> None:
    """Professional page setup: fonts, spacing, margins, header and page-number footer."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    styles = document.styles
    normal = styles["Normal"]
    normal.font.name = _DOCX_BODY_FONT
    normal.font.size = Pt(11)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), _DOCX_BODY_FONT)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15

    heading_sizes = {"Title": 26, "Heading 1": 16, "Heading 2": 13, "Heading 3": 12}
    for name, size in heading_sizes.items():
        try:
            style = styles[name]
        except KeyError:
            continue
        style.font.name = _DOCX_BODY_FONT
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = _docx_rgb(_DOCX_ACCENT)
        style.element.rPr.rFonts.set(qn("w:eastAsia"), _DOCX_BODY_FONT)
        style.paragraph_format.space_before = Pt(14 if name == "Heading 1" else 10)
        style.paragraph_format.space_after = Pt(4)
        style.paragraph_format.keep_with_next = True

    section = document.sections[0]
    section.top_margin = section.bottom_margin = Inches(1.0)
    section.left_margin = section.right_margin = Inches(1.0)

    header = section.header.paragraphs[0]
    header.text = title or "Knowledge Export"
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in header.runs:
        run.font.size = Pt(9)
        run.font.color.rgb = _docx_rgb(_DOCX_MUTED)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run("Page ")
    run.font.size = Pt(9)
    run.font.color.rgb = _docx_rgb(_DOCX_MUTED)
    page_run = footer.add_run()
    page_run.font.size = Pt(9)
    page_run.font.color.rgb = _docx_rgb(_DOCX_MUTED)
    for tag, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if tag:
            fld = OxmlElement("w:fldChar")
            fld.set(qn("w:fldCharType"), tag)
            page_run._r.append(fld)
        else:
            instr = OxmlElement("w:instrText")
            instr.set(qn("xml:space"), "preserve")
            instr.text = text
            page_run._r.append(instr)


def _drop_redundant_lead(content: str, payload: ExportPayload) -> str:
    """Remove a leading title / ``Source: <url>`` echo already shown in the title block."""
    lines = (content or "").splitlines()
    title = _strip_markdown(payload.title or "").strip().lower()
    index = 0
    while index < len(lines) and index < 4:
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        lowered = _strip_markdown(stripped).lower()
        # Any leading "Source: <url>" echo is redundant with the Link line above.
        if (title and lowered == title) or (lowered.startswith("source:") and "http" in lowered):
            index += 1
            continue
        break
    body = "\n".join(lines[index:])
    # Legacy captures inserted a guard space before punctuation ("plane .").
    body = re.sub(r"(\S)\s+([.,;:!?])(\s|$)", r"\1\2\3", body)
    return body


def _docx_add_runs(paragraph, text: str, *, base_size_pt: float | None = None) -> None:
    """Add ``text`` to ``paragraph`` honouring ``**bold**`` and ```code``` runs."""
    from docx.shared import Pt

    for chunk, bold, code in _inline_runs(text):
        run = paragraph.add_run(chunk)
        if bold:
            run.bold = True
        if code:
            run.font.name = _DOCX_CODE_FONT
            run.font.size = Pt((base_size_pt or 11) - 1)
        elif base_size_pt:
            run.font.size = Pt(base_size_pt)


def _docx_table(document, headers: list[str], rows: list[list[str]]) -> None:
    """Styled table: navy header row, white bold header text, zebra body rows."""
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Pt

    width = max([len(headers)] + [len(r) for r in rows]) if (headers or rows) else 0
    if width == 0:
        return
    table = document.add_table(rows=(1 if headers else 0) + len(rows), cols=width)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    row_index = 0
    if headers:
        for col, header in enumerate((headers + [""] * width)[:width]):
            cell = table.rows[0].cells[col]
            cell.text = ""
            para = cell.paragraphs[0]
            run = para.add_run(_strip_markdown(str(header)))
            run.bold = True
            run.font.size = Pt(10)
            run.font.color.rgb = _docx_rgb((0xFF, 0xFF, 0xFF))
            _docx_shade(cell, _DOCX_TABLE_HEADER_BG)
        row_index = 1
    for body_i, row in enumerate(rows):
        for col in range(width):
            cell = table.rows[row_index + body_i].cells[col]
            cell.text = ""
            value = str(row[col]) if col < len(row) else ""
            _docx_add_runs(cell.paragraphs[0], value, base_size_pt=10)
            if body_i % 2 == 1:
                _docx_shade(cell, _DOCX_TABLE_ZEBRA_BG)
    document.add_paragraph()


def _docx_code_block(document, lines: list[str]) -> None:
    """Monospace, shaded, non-wrapping block that keeps diagram alignment."""
    from docx.shared import Pt

    table = document.add_table(rows=1, cols=1)
    table.style = "Table Grid"
    cell = table.rows[0].cells[0]
    _docx_shade(cell, _DOCX_CODE_BG)
    cell.text = ""
    first = True
    for line in lines:
        para = cell.paragraphs[0] if first else cell.add_paragraph()
        first = False
        para.paragraph_format.space_after = Pt(0)
        para.paragraph_format.line_spacing = 1.0
        run = para.add_run(line.rstrip("\n") or " ")
        run.font.name = _DOCX_CODE_FONT
        run.font.size = Pt(9)
    document.add_paragraph()


def _append_docx_body(document, text: str | None) -> None:
    """Render markdown-ish ``text`` into the document as structured Word blocks."""
    from docx.shared import Pt

    for block in parse_markdown_blocks(text):
        kind = block.get("kind")
        if kind == "heading":
            level = min(max(int(block.get("level", 2)), 1), 3)
            document.add_heading(_strip_markdown(str(block["text"])), level=level)
        elif kind == "table":
            rows = [list(map(str, r)) for r in block["rows"]]  # type: ignore[index]
            _docx_table(document, rows[0] if rows else [], rows[1:] if len(rows) > 1 else [])
        elif kind == "code":
            _docx_code_block(document, list(block["lines"]))  # type: ignore[index]
        elif kind in {"bullet", "ordered"}:
            style = "List Bullet" if kind == "bullet" else "List Number"
            depth = int(block.get("depth", 0))
            if depth:
                style = f"{style} {min(depth + 1, 3)}"
            try:
                para = document.add_paragraph(style=style)
            except KeyError:
                para = document.add_paragraph(style="List Bullet" if kind == "bullet" else "List Number")
            para.paragraph_format.space_after = Pt(2)
            _docx_add_runs(para, str(block["text"]))
        else:
            para = document.add_paragraph()
            _docx_add_runs(para, str(block.get("text", "")))


class DocxExporter(Exporter):
    format_name = "docx"

    def render(self, payload: ExportPayload) -> bytes:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches

        from datetime import datetime

        from docx.shared import Pt

        stream = io.BytesIO()
        document = Document()
        _docx_apply_theme(document, title=payload.title or "")

        # --- Title block -------------------------------------------------------
        if payload.title:
            heading = document.add_heading(_strip_markdown(payload.title), level=0)
            heading.alignment = WD_ALIGN_PARAGRAPH.LEFT
        if payload.subtitle:
            paragraph = document.add_paragraph()
            run = paragraph.add_run(_strip_markdown(payload.subtitle))
            run.italic = True
            run.font.size = Pt(12)
            run.font.color.rgb = _docx_rgb(_DOCX_MUTED)
        meta_bits: list[str] = []
        if payload.source_type:
            meta_bits.append(f"Source: {str(payload.source_type).title()}")
        meta_bits.append(f"Exported: {datetime.now():%d %b %Y, %H:%M}")
        meta = document.add_paragraph()
        meta_run = meta.add_run("  |  ".join(meta_bits))
        meta_run.font.size = Pt(9)
        meta_run.font.color.rgb = _docx_rgb(_DOCX_MUTED)
        if payload.source_url:
            link = document.add_paragraph()
            link_label = link.add_run("Link: ")
            link_label.font.size = Pt(9)
            link_label.font.color.rgb = _docx_rgb(_DOCX_MUTED)
            url_run = link.add_run(payload.source_url)
            url_run.font.size = Pt(9)
            url_run.font.color.rgb = _docx_rgb((0x1A, 0x56, 0xDB))
            url_run.underline = True
        _docx_horizontal_rule(document)

        # --- Body ----------------------------------------------------------------
        if payload.content:
            _append_docx_body(document, _drop_redundant_lead(payload.content, payload))
        for section in payload.sections:
            document.add_heading(_strip_markdown(section.heading or ""), level=1)
            if section.evidence:
                evidence = document.add_paragraph()
                run = evidence.add_run(_strip_markdown(section.evidence))
                run.italic = True
                run.font.size = Pt(9)
                run.font.color.rgb = _docx_rgb(_DOCX_MUTED)
            if section.rows:
                _docx_table(document, list(section.headers or []), [list(map(str, r)) for r in section.rows])
            elif section.body:
                if section.heading == "System Architecture" and payload.diagram_png:
                    document.add_picture(io.BytesIO(payload.diagram_png), width=Inches(6.0))
                    caption = next(
                        (line for line in section.body.splitlines() if line.strip()),
                        "",
                    )
                    if caption:
                        cap = document.add_paragraph()
                        cap_run = cap.add_run(_strip_markdown(caption))
                        cap_run.italic = True
                        cap_run.font.size = Pt(9)
                        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                else:
                    _append_docx_body(document, section.body)
        if payload.rows:
            _docx_table(document, list(payload.headers or []), [list(map(str, r)) for r in payload.rows])
        document.save(stream)
        return stream.getvalue()


class PdfExporter(Exporter):
    format_name = "pdf"

    def render(self, payload: ExportPayload) -> bytes:
        architecture = getattr(payload, "architecture", None)
        if architecture is not None:
            from app.export.architecture_pdf import render_architecture_document

            return render_architecture_document(architecture, repo_url=payload.source_url)
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Image,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
        )

        stream = io.BytesIO()
        if payload.rows and len(payload.headers) > 3:
            page = landscape(A4)
        else:
            page = A4
        doc = SimpleDocTemplate(
            stream,
            pagesize=page,
            rightMargin=0.75 * inch,
            leftMargin=0.75 * inch,
            topMargin=0.75 * inch,
            bottomMargin=0.75 * inch,
        )
        styles = getSampleStyleSheet()
        story: list = []
        if payload.title:
            story.append(Paragraph(payload.title, styles["Title"]))
        if payload.subtitle:
            story.append(Paragraph(payload.subtitle, styles["Italic"]))
        if payload.source_url:
            story.append(Paragraph(f"Source: {payload.source_url}", styles["Normal"]))
        story.append(Spacer(1, 0.15 * inch))
        if payload.content:
            for para in _strip_markdown(payload.content).splitlines() or [""]:
                story.append(Paragraph(_pdf_escape(para or "&nbsp;"), styles["BodyText"]))
            story.append(Spacer(1, 0.15 * inch))
        for section in payload.sections:
            story.append(Paragraph(section.heading or "", styles["Heading2"]))
            if section.evidence:
                story.append(
                    Paragraph(f"<i>[{_pdf_escape(section.evidence)}]</i>", styles["Italic"])
                )
            if section.rows:
                story.append(_pdf_table(section.headers, section.rows, styles, colors))
                story.append(Spacer(1, 0.1 * inch))
            elif section.body:
                if section.heading == "System Architecture" and payload.diagram_png:
                    story.append(_fitted_image(payload.diagram_png, inch, Image))
                    caption = next(
                        (line for line in _strip_markdown(section.body).splitlines() if line.strip()),
                        "",
                    )
                    if caption:
                        story.append(Paragraph(_pdf_escape(caption), styles["Italic"]))
                else:
                    for para in _strip_markdown(section.body or "").splitlines() or [""]:
                        story.append(Paragraph(_pdf_escape(para or "&nbsp;"), styles["BodyText"]))
            story.append(Spacer(1, 0.12 * inch))
        if payload.rows:
            story.append(_pdf_table(payload.headers, payload.rows, styles, colors))
        doc.build(story)
        return stream.getvalue()


def _pdf_table(headers: list[str], rows: list[list[str]], styles, colors):
    """renderlab ``Table`` with wrapping ``Paragraph`` cells (8pt body text)."""
    from reportlab.platypus import Paragraph, Table, TableStyle

    data: list[list[object]] = []
    if headers:
        data.append([f"<b>{_pdf_escape(h)}</b>" for h in headers])
    for row in rows:
        data.append(
            [
                Paragraph(_pdf_escape(str(cell) or "&nbsp;"), styles["BodyText"])
                for cell in row
            ]
        )
    table = Table(data, repeatRows=1 if headers else 0)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.grey) if headers else ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white) if headers else ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold") if headers else ("FONTNAME", (0, 0), (-1, 0), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _fitted_image(png: bytes, inch: float, image_cls):
    """Wrap a PNG for rendering scaled to fit the printable frame (aspect kept)."""
    width = 6.0 * inch
    height = None
    try:
        from PIL import Image as _PILImage

        with _PILImage.open(io.BytesIO(png)) as pil:
            image_w, image_h = pil.size
            ratio = image_h / image_w
        candidate = width * ratio
        max_height = 8.6 * inch
        if candidate > max_height:
            width = max_height / ratio
            candidate = max_height
        height = candidate
    except Exception:  # noqa: BLE001 - fall back to reportlab's native sizing
        pass
    return image_cls(io.BytesIO(png), width=width, height=height)


def _html_table(headers: list[str], rows: list[list[str]]) -> str:
    table = ["<table><thead><tr>"]
    table.extend(f"<th>{_escape(h)}</th>" for h in headers)
    table.append("</tr></thead><tbody>")
    for row in rows:
        table.append("<tr>")
        table.extend(f"<td>{_escape(cell)}</td>" for cell in row)
        table.append("</tr>")
    table.append("</tbody></table>")
    return "".join(table)


def _escape(value: str) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _escape_url(value: str) -> str:
    return (value or "").replace('"', "%22").replace("<", "%3C").replace(">", "%3E")


def _pdf_escape(value: str) -> str:
    return _escape(value).replace("\n", "<br/>")


def _clean_text_without_newlines(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
