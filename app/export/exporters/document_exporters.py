"""Generated-format exporters: TXT, MD, JSON, HTML, CSV, DOCX, PDF.

Every exporter renders an :class:`~app.export.registry.ExportPayload` into bytes.
These are the *generated* formats; native origl files never pass through here.
"""

from __future__ import annotations

import csv as _csv
import io
import json
import re

from app.export.registry import Exporter, ExportPayload


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
    for raw in (value or "").splitlines():
        if _MD_HORIZONTAL_RULE.match(raw):
            continue
        line = _MD_HEADING.sub(r"\1", raw)
        line = _MD_BOLD.sub(lambda m: m.group(1) or m.group(2), line)
        line = _MD_CODE.sub(r"\1", line)
        line = _MD_EMPHASIS.sub(r"\1", line)
        line = _MD_ASTER_BULLET.sub(r"\1- ", line)
        lines.append(line)
    return "\n".join(lines).strip()


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


class DocxExporter(Exporter):
    format_name = "docx"

    def render(self, payload: ExportPayload) -> bytes:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches

        stream = io.BytesIO()
        document = Document()
        if payload.title:
            heading = document.add_heading(payload.title or "", level=0)
            heading.alignment = WD_ALIGN_PARAGRAPH.LEFT
        if payload.subtitle:
            paragraph = document.add_paragraph(payload.subtitle or "")
            for run in paragraph.runs:
                run.italic = True
        if payload.source_url:
            document.add_paragraph(f"Source: {payload.source_url}")
        if payload.content:
            document.add_paragraph(_strip_markdown(payload.content))
        for section in payload.sections:
            document.add_heading(section.heading or "", level=1)
            if section.evidence:
                paragraph = document.add_paragraph(section.evidence or "")
                for run in paragraph.runs:
                    run.italic = True
            if section.rows:
                table = document.add_table(
                    rows=len(section.rows) + 1, cols=len(section.headers or (section.rows[0] if section.rows else []))
                )
                if section.headers:
                    for index, header in enumerate(section.headers):
                        table.rows[0].cells[index].text = str(header)
                    body_start = 1
                else:
                    body_start = 0
                for row_index, row in enumerate(section.rows, start=body_start):
                    for col_index, cell in enumerate(row):
                        table.rows[row_index].cells[col_index].text = str(cell)
            elif section.body:
                if section.heading == "System Architecture" and payload.diagram_png:
                    document.add_picture(io.BytesIO(payload.diagram_png), width=Inches(6.0))
                    caption = next(
                        (line for line in section.body.splitlines() if line.strip()),
                        "",
                    )
                    if caption:
                        document.add_paragraph(caption)
                else:
                    for para in _strip_markdown(section.body or "").splitlines():
                        document.add_paragraph(para)
        if payload.rows:
            table = document.add_table(rows=len(payload.rows) + 1, cols=len(payload.headers or (payload.rows[0] if payload.rows else [])))
            if payload.headers:
                for index, header in enumerate(payload.headers):
                    table.rows[0].cells[index].text = str(header)
                body_start = 1
            else:
                body_start = 0
            for row_index, row in enumerate(payload.rows, start=body_start):
                for col_index, cell in enumerate(row):
                    table.rows[row_index].cells[col_index].text = str(cell)
        document.save(stream)
        return stream.getvalue()


class PdfExporter(Exporter):
    format_name = "pdf"

    def render(self, payload: ExportPayload) -> bytes:
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
