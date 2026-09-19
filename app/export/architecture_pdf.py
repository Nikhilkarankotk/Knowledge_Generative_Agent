"""Graphical architecture pages for the GitHub PDF report.

Eight landscape pages are rendered straight from an :class:`ArchitectureModel`
(reportlab vector graphics - no bitmaps, no LLM text, no source-code dumps):

1. Application Overview & High-Level Architecture (large layered diagram)
2. Component & Communication View (column graph with operations, routes, evidence)
3. Key Application Flows (sequence/data-flow diagrams with observed call paths)
4. Data & Integration Architecture (services -> stores, application -> externals)
5. Deployment Architecture (evidence only, or an honest "not mapped")
6. Repository Structure & Code Flow (directory map + observed flow + areas)
7. Security Architecture (controls observed in the repository, with evidence)
8. Architecture Style & Mapping Tables (style banner + component/tech/endpoint
   mapping tables, all evidence-backed)

Every box carries evidence-backed content (operations, routes, owning types,
referencing files) rather than an empty label. Layout constants were tuned
against reportlab's ``SimpleDocTemplate`` frame: the available drawing area is
the landscape page minus 2 * margin minus the 12pt Frame padding; each drawing
stays a few points under that so no page ever trips a mid-cycle ``LayoutError``
or spawns a trailing blank page.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from reportlab.graphics.shapes import Drawing, Line, Path, Polygon, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import PageBreak, SimpleDocTemplate

from app.export.architecture import (
    KIND_CACHE,
    KIND_CLIENT,
    KIND_CONTROLLER,
    KIND_CROSS_CUTTING,
    KIND_DATABASE,
    KIND_EXTERNAL,
    KIND_GATEWAY,
    KIND_MESSAGE_BROKER,
    KIND_SERVICE,
    LAYER_CLIENT,
    LAYER_DATA,
    LAYER_EXTERNAL,
    LAYER_GATEWAY,
    LAYER_SERVICES,
    ArchitectureComponent,
    ArchitectureFlow,
    ArchitectureModel,
)

_NUM_PAGES = 8

_MARGIN = 20.0
# SimpleDocTemplate adds 6pt Frame padding on each side, so the *available*
# drawing area is pagesize - 2*margin - 12. We stay a little under the exact
# available area so a full-height flowable never trips a mid-cycle LayoutError
# or spawns a trailing blank page.
PAGE_W = landscape(A4)[0] - 2 * _MARGIN - 12.0 - 6.0
PAGE_H = landscape(A4)[1] - 2 * _MARGIN - 12.0 - 6.0

# ── palette ----------------------------------------------------------------

INK = colors.HexColor("#1F2937")
MUTED = colors.HexColor("#6B7280")
BORDER = colors.HexColor("#94A3B8")
BORDER_LIGHT = colors.HexColor("#CBD5E1")
WHITE = colors.HexColor("#FFFFFF")
SOFT = colors.HexColor("#F8FAFC")

NAVY = colors.HexColor("#1F3864")
BLUE = colors.HexColor("#2E75B6")
LIGHT_BLUE = colors.HexColor("#DEEBF7")
GREEN = colors.HexColor("#548235")
LIGHT_GREEN = colors.HexColor("#E2EFDA")
AMBER = colors.HexColor("#C55A11")
LIGHT_AMBER = colors.HexColor("#FBE5D6")
GREY = colors.HexColor("#5B6573")
LIGHT_GREY = colors.HexColor("#EFF1F4")
PURPLE = colors.HexColor("#7030A0")
LIGHT_PURPLE = colors.HexColor("#E8E0F5")
RED = colors.HexColor("#C00000")
LIGHT_RED = colors.HexColor("#FCE4E1")
TEAL = colors.HexColor("#00796B")
LIGHT_TEAL = colors.HexColor("#E0F2F1")

LAYER_COLORS: dict[str, tuple[colors.Color, colors.Color]] = {
    LAYER_CLIENT: (NAVY, LIGHT_BLUE),
    LAYER_GATEWAY: (BLUE, LIGHT_BLUE),
    LAYER_SERVICES: (GREEN, LIGHT_GREEN),
    LAYER_DATA: (AMBER, LIGHT_AMBER),
    LAYER_EXTERNAL: (GREY, LIGHT_GREY),
}

KIND_STORE_COLORS: dict[str, tuple[colors.Color, colors.Color]] = {
    KIND_DATABASE: (AMBER, LIGHT_AMBER),
    KIND_CACHE: (TEAL, LIGHT_TEAL),
    KIND_MESSAGE_BROKER: (PURPLE, LIGHT_PURPLE),
}

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"

# Short, table-friendly labels for the mapping tables.
_LAYER_SHORT: dict[str, str] = {
    LAYER_CLIENT: "Client / UI",
    LAYER_GATEWAY: "API / Gateway",
    LAYER_SERVICES: "Services",
    LAYER_DATA: "Data / Infra",
    LAYER_EXTERNAL: "External",
}

_KIND_SHORT: dict[str, str] = {
    KIND_CLIENT: "Frontend",
    KIND_GATEWAY: "Entry point",
    KIND_CONTROLLER: "API controller",
    KIND_SERVICE: "Service",
    KIND_DATABASE: "Database",
    KIND_CACHE: "Cache",
    KIND_MESSAGE_BROKER: "Message broker",
    KIND_EXTERNAL: "External API",
    KIND_CROSS_CUTTING: "Cross-cutting",
}

# ── primitive helpers ------------------------------------------------------


def _rrect(d: Drawing, x: float, y: float, w: float, h: float, *, r: float = 4.0,
           fill: colors.Color = WHITE, stroke: colors.Color | None = BORDER,
           stroke_width: float = 0.7) -> None:
    path = Path()
    radius = max(0.0, min(r, (w - 1) / 2, (h - 1) / 2))
    k = 0.5522847498 * radius
    path.moveTo(x + radius, y)
    path.lineTo(x + w - radius, y)
    path.curveTo(x + w - radius + k, y, x + w, y + radius - k, x + w, y + radius)
    path.lineTo(x + w, y + h - radius)
    path.curveTo(x + w, y + h - radius + k, x + w - radius + k, y + h, x + w - radius, y + h)
    path.lineTo(x + radius, y + h)
    path.curveTo(x + radius - k, y + h, x, y + h - radius + k, x, y + h - radius)
    path.lineTo(x, y + radius)
    path.curveTo(x, y + radius - k, x + radius - k, y, x + radius, y)
    path.closePath()
    path.fillColor = fill
    path.strokeColor = stroke
    path.strokeWidth = stroke_width
    d.add(path)


def _line(d: Drawing, x1: float, y1: float, x2: float, y2: float, *,
          color: colors.Color = BORDER, width: float = 1.0, dashed: Sequence[float] | None = None) -> None:
    line = Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=width)
    if dashed:
        line.strokeDashArray = list(dashed)
    d.add(line)


def _text(d: Drawing, x: float, y: float, value: str, *, size: float = 8.0,
          font: str = FONT, color: colors.Color = INK, anchor: str = "start") -> None:
    d.add(String(x, y, value, fontSize=size, fontName=font, fillColor=color, textAnchor=anchor))


def _text_center(d: Drawing, cx: float, cy: float, value: str, *, size: float = 8.0,
                 font: str = FONT, color: colors.Color = INK) -> None:
    _text(d, cx, cy - size * 0.33, value, size=size, font=font, color=color, anchor="middle")


def _polyline(d: Drawing, points: Sequence[tuple[float, float]], *,
              color: colors.Color = BLUE, width: float = 1.2, dashed: Sequence[float] | None = None,
              arrow: bool = True, head: float = 6.0) -> None:
    if len(points) < 2:
        return
    for index in range(len(points) - 1):
        x1, y1 = points[index]
        x2, y2 = points[index + 1]
        if index == len(points) - 2 and arrow:
            _arrow(d, x1, y1, x2, y2, color=color, width=width, head=head)
        else:
            _line(d, x1, y1, x2, y2, color=color, width=width, dashed=dashed)


def _arrow(d: Drawing, x1: float, y1: float, x2: float, y2: float, *,
           color: colors.Color = BLUE, width: float = 1.3, head: float = 6.0,
           dashed: Sequence[float] | None = None) -> None:
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return
    angle = math.atan2(dy, dx)
    tip_x = x2 - math.cos(angle) * (head * 0.6)
    tip_y = y2 - math.sin(angle) * (head * 0.6)
    line = Line(x1, y1, tip_x, tip_y, strokeColor=color, strokeWidth=width)
    if dashed:
        line.strokeDashArray = list(dashed)
    d.add(line)
    spread = 0.5
    left = (tip_x - head * math.cos(angle - spread), tip_y - head * math.sin(angle - spread))
    right = (tip_x - head * math.cos(angle + spread), tip_y - head * math.sin(angle + spread))
    d.add(
        Polygon(
            [tip_x, tip_y, left[0], left[1], right[0], right[1]],
            fillColor=color, strokeColor=color,
        )
    )


def _char_width(size: float) -> float:
    return size * 0.52


def _wrap(value: str, size: float, max_w: float, *, max_lines: int = 6) -> list[str]:
    max_chars = max(4, int(max_w / _char_width(size)))
    words = (value or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        while len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:max_chars])
            word = word[max_chars:]
            if len(lines) >= max_lines:
                return lines[:max_lines]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:max_lines]


def _fit(value: str, size: float, max_w: float, max_lines: int, fallback: str) -> str:
    lines = _wrap(value, size, max_w, max_lines=max_lines)
    if not lines:
        return fallback
    if len(lines) <= max_lines:
        return " ".join(lines)
    if max_lines <= 1:
        max_chars = max(4, int(max_w / _char_width(size)) - 1)
        return lines[0][:max_chars].rstrip() + "…"
    kept = " ".join(lines[: max_lines - 1])
    return kept.rstrip() + " …"


# ── shared page scaffolding --------------------------------------------------


def _new_page(page_no: int, title: str, subtitle: str) -> Drawing:
    d = Drawing(PAGE_W, PAGE_H)
    _rrect(d, 0, PAGE_H - 34, PAGE_W, 34, r=0, fill=NAVY, stroke=NAVY)
    _text(d, 14, PAGE_H - 24, title, size=13, font=FONT_BOLD, color=WHITE)
    _text(d, 14, PAGE_H - 11, _fit(subtitle, 7.5, PAGE_W - 220, 1, subtitle), size=7.5, color=WHITE)
    _text(d, PAGE_W - 14, PAGE_H - 24, f"Architecture Overview  ·  {page_no} / {_NUM_PAGES}",
          size=7.5, font=FONT_BOLD, color=WHITE, anchor="end")
    return d


def _footer(d: Drawing, page_no: int, model: ArchitectureModel) -> None:
    _line(d, 14, 14, PAGE_W - 14, 14, color=BORDER, width=0.6)
    _text(d, 14, 7, f"Repository: {model.repo}",
          size=6.5, color=MUTED)
    _text(d, PAGE_W - 14, 7,
          "Every box/arrow is grounded in a sampled repository path; anything not observed is not shown.",
          size=6.5, color=MUTED, anchor="end")


def _component_fill(component: ArchitectureComponent) -> tuple[colors.Color, colors.Color]:
    if component.kind in KIND_STORE_COLORS:
        return KIND_STORE_COLORS[component.kind]
    if component.kind == KIND_CROSS_CUTTING:
        return PURPLE, LIGHT_PURPLE
    return LAYER_COLORS.get(component.layer, (AMBER, LIGHT_AMBER))


def _component_box(d: Drawing, x: float, y: float, w: float, h: float,
                   component: ArchitectureComponent, *,
                   show_evidence: bool = True, title_size: float = 7.2,
                   highlighted: bool = False, compact: bool = False,
                   fact_lines: int = 0) -> None:
    acc, light = _component_fill(component)
    stroke = RED if highlighted else BORDER
    _rrect(d, x, y, w, h, r=3, fill=light, stroke=stroke, stroke_width=1.1 if highlighted else 0.7)
    header_h = 13.0 if not compact else 11.0
    _rrect(d, x, y + h - header_h, w, header_h, r=3, fill=acc, stroke=acc)
    _text_center(d, x + w / 2, y + h - header_h / 2, _fit(component.name, title_size, w - 8, 1, component.name),
                 size=title_size, font=FONT_BOLD, color=WHITE)

    body = y + h - header_h - 4
    resp_lines = _wrap(component.responsibility or "See evidence.", 6.3, w - 10)
    max_resp = 1 if compact else 2
    _draw_body_lines(d, x, body, resp_lines[:max_resp], size=6.3, color=INK)
    body -= 8.0 * len(resp_lines[:max_resp])

    if fact_lines:
        rows: list[str] = []
        budget = fact_lines
        for fact in component.fact_lines(limit=fact_lines * 2):
            if budget <= 0:
                break
            take = _wrap(fact, 5.8, w - 10, max_lines=min(budget, 2))
            rows.extend(take)
            budget -= len(take)
        rows = rows[:fact_lines]
        if rows:
            _draw_body_lines(d, x, body + 1.5, rows, size=5.8, color=GREY)
            body -= 7.2 * len(rows)

    if show_evidence:
        ev = f"Evidence: {component.primary_evidence}"
        if body >= y + 2:
            _text(d, x + 5, body, _fit(ev, 5.4, w - 10, 1, ev), size=5.4, color=MUTED)


def _draw_body_lines(d: Drawing, x: float, top: float, lines: Sequence[str], *,
                     size: float, color: colors.Color) -> None:
    cursor = top
    for line in lines:
        _text(d, x + 5, cursor, line, size=size, color=color)
        cursor -= 8.0


def _group_box(d: Drawing, x: float, y: float, w: float, h: float, label: str,
               acc: colors.Color, light: colors.Color) -> None:
    _rrect(d, x, y, w, h, r=3, fill=light, stroke=acc, stroke_width=1.0)
    _text_center(d, x + w / 2, y + h / 2, label, size=6.5, font=FONT_BOLD, color=acc)


def _legend(d: Drawing, x: float, y: float, items: Sequence[tuple[str, colors.Color]]) -> None:
    cursor = x
    for label, color in items:
        _rrect(d, cursor, y, 10, 10, r=2, fill=color, stroke=BORDER, stroke_width=0.5)
        _text(d, cursor + 14, y + 2.5, label, size=6.5, color=INK)
        cursor += 16 + len(label) * 4.1 + 12


def _note_line(d: Drawing, x: float, y: float, text: str, max_w: float) -> None:
    _text(d, x, y, _fit(text, 6.5, max_w, 1, text), size=6.5, color=MUTED)


# ── shared helpers ----------------------------------------------------------


def _rollup_groups(model: ArchitectureModel, max_per_row: int) -> list[tuple[str, list[ArchitectureComponent], int]]:
    groups: list[tuple[str, list[ArchitectureComponent]]] = [
        (LAYER_CLIENT, model.by_layer(LAYER_CLIENT)),
        (LAYER_GATEWAY, model.by_layer(LAYER_GATEWAY)),
        (LAYER_SERVICES, model.by_layer(LAYER_SERVICES)),
        (LAYER_DATA, model.by_layer(LAYER_DATA)),
        (LAYER_EXTERNAL, model.by_layer(LAYER_EXTERNAL)),
    ]
    rows: list[tuple[str, list[ArchitectureComponent], int]] = []
    for title, components in groups:
        if not components:
            continue
        rows.append((title, components[:max_per_row], len(components) - len(components[:max_per_row])))
    return rows


def _primary_flow_ids(model: ArchitectureModel) -> list[str]:
    if not model.flows:
        return []
    by_name = {component.name: component.component_id for component in model.components}
    ids: list[str] = []
    for step in model.flows[0].steps:
        cid = by_name.get(step)
        if cid:
            ids.append(cid)
        if len(ids) >= 5:
            break
    return ids


def _component_counts(model: ArchitectureModel) -> str:
    def count(kind: str) -> int:
        return len(model.by_kind(kind))

    parts: list[str] = []
    services = count(KIND_SERVICE)
    if services:
        parts.append(f"{services} service module{'s' if services != 1 else ''}")
    api = count(KIND_CONTROLLER) + count(KIND_GATEWAY)
    if api:
        parts.append(f"{api} API entr{'y' if api == 1 else 'ies'}")
    stored = count(KIND_DATABASE) + count(KIND_CACHE) + count(KIND_MESSAGE_BROKER)
    if stored:
        parts.append(f"{stored} data store{'s' if stored != 1 else ''}")
    ext = count(KIND_EXTERNAL)
    if ext:
        parts.append(f"{ext} external integration{'s' if ext != 1 else ''}")
    if model.security:
        parts.append(f"{len(model.security)} security control{'s' if len(model.security) != 1 else ''}")
    if not parts:
        return ""
    return "Composition: " + ", ".join(parts)


def _stack_badges(model: ArchitectureModel) -> list[str]:
    priority = {
        "Language / Runtime": 0, "Backend Framework": 1, "Frontend": 2,
        "Database": 3, "Data Access / ORM": 4, "API / Data Protocol": 5,
        "Messaging": 6, "Cache": 7, "Security / Auth": 8, "Observability": 9,
    }
    entries = sorted(
        model.technologies,
        key=lambda e: (priority.get(e.category, 90), e.name.lower()),
    )
    names: list[str] = []
    for entry in entries:
        if entry.category == "Testing":
            continue
        if entry.name in names:
            continue
        names.append(entry.name)
        if len(names) >= 8:
            break
    return names


# ── Page 1 · Application Overview & High-Level Architecture ------------------


def _protocol_between(model: ArchitectureModel, lower_layer: str, upper_layer: str) -> str:
    if lower_layer == LAYER_DATA:
        if any(c.kind == KIND_MESSAGE_BROKER for c in model.by_layer(LAYER_DATA)) and not model.by_kind(KIND_DATABASE):
            return "async messages"
        if model.by_kind(KIND_DATABASE) and model.by_kind(KIND_CACHE):
            return "SQL + cache"
        if model.by_kind(KIND_DATABASE):
            return "SQL / data access"
        return "read / write"
    if lower_layer == LAYER_EXTERNAL:
        return "HTTP APIs"
    if lower_layer == LAYER_SERVICES:
        return "internal calls"
    if lower_layer == LAYER_GATEWAY:
        return "HTTP / REST"
    return "calls"


def _chip(d: Drawing, x: float, y: float, text: str, *, acc: colors.Color, light: colors.Color,
          size: float = 6.0, max_w: float = 150.0, h: float = 16.0) -> float:
    width = min(max_w, len(text) * (size * 0.58) + 12)
    _rrect(d, x, y, width, h, r=3, fill=light, stroke=acc, stroke_width=0.6)
    _text_center(d, x + width / 2, y + h / 2, _fit(text, size, width - 8, 1, text), size=size,
                 font=FONT_BOLD, color=acc)
    return width


def _page_application_architecture(model: ArchitectureModel) -> Drawing:
    d = _new_page(1, "Application Overview & High-Level Architecture", model.repo)

    # ── Top overview panel ──────────────────────────────────────────────────
    ov_top = PAGE_H - 48.0
    ov_height = 82.0
    ov_bottom = ov_top - ov_height

    _text(d, 14, ov_top - 17, model.repo, size=14, font=FONT_BOLD, color=NAVY)
    description = model.description or (
        "The repository contains the application whose architecture is summarised "
        "on the following pages."
    )
    lines = _wrap(description, 7.5, 380, max_lines=4)
    cursor = ov_top - 26
    for line in lines:
        _text(d, 14, cursor, line, size=7.5, color=INK)
        cursor -= 9.0

    counts = _component_counts(model)
    if counts:
        _text(d, 14, cursor - 1, counts, size=6.6, font=FONT_BOLD, color=GREEN)
        cursor -= 9.0

    right_x = 424.0
    right_w = PAGE_W - right_x - 14.0
    style_label = f"Architecture · {model.style}" if model.style else "Architecture style not inferred"
    _chip(d, right_x, ov_top - 17, _fit(style_label, 7.0, right_w, 1, style_label),
          acc=NAVY, light=LIGHT_BLUE, size=7.0, h=18.0, max_w=right_w)
    _text(d, right_x, ov_top - 31, "Technology stack", size=6.5, font=FONT_BOLD, color=MUTED)
    badges = _stack_badges(model)
    chip_y = ov_top - 42
    chip_x = right_x
    for badge in badges:
        width = _chip(d, chip_x, chip_y, badge, acc=GREY, light=LIGHT_GREY, size=6.0, max_w=140.0, h=15.0)
        chip_x += width + 6
        if chip_x + 80 > right_x + right_w:
            chip_x = right_x
            chip_y -= 18
        if chip_y < ov_bottom + 4:
            break

    _line(d, 14, ov_bottom - 8, PAGE_W - 14, ov_bottom - 8, color=BORDER_LIGHT, width=0.6)

    # ── Large layered diagram ──────────────────────────────────────────────
    dia_top = ov_bottom - 16.0
    dia_bottom = 70.0
    cross = model.by_kind(KIND_CROSS_CUTTING)

    main_right = PAGE_W - 176.0 if cross else PAGE_W - 14.0
    label_w = 108.0
    boxes_left = 14.0 + label_w + 10.0
    boxes_right = main_right
    boxes_w = boxes_right - boxes_left

    rows = _rollup_groups(model, max_per_row=4)
    transition = 26.0  # gap + arrow band between adjacent layer rows
    row_h = min(74.0, max(40.0, (dia_top - dia_bottom - (len(rows) - 1) * transition) / max(1, len(rows))))

    row_meta: list[dict[str, Any]] = []
    cursor = dia_top
    for title, components, extra in rows:
        n = len(components)
        box_w = min(150.0, (boxes_w - 16.0 - (n - 1) * 10.0) / max(1, n))
        total_w = n * box_w + (n - 1) * 10.0
        if extra:
            total_w += min(box_w, 76.0) + 10.0
        start_x = boxes_left + (boxes_w - total_w) / 2
        centers: list[float] = []
        for index, component in enumerate(components):
            x = start_x + index * (box_w + 10.0)
            centers.append(x + box_w / 2.0)
            _component_box(d, x, cursor - row_h, box_w, row_h, component,
                           show_evidence=False, title_size=7.4, fact_lines=1)
        if extra:
            x = start_x + n * (box_w + 10.0)
            _group_box(d, x, cursor - row_h, min(box_w, 76.0), row_h, f"+{extra} more", AMBER, LIGHT_AMBER)
            centers.append(x + min(box_w, 76.0) / 2.0)
        _text(d, 14, cursor - row_h / 2 - 2.5, title, size=6.8, font=FONT_BOLD, color=MUTED)
        row_meta.append({"top": cursor, "bottom": cursor - row_h, "centers": centers, "title": title})
        cursor -= row_h + transition

    for index in range(len(row_meta) - 1):
        upper = row_meta[index]
        lower = row_meta[index + 1]
        cx = boxes_left + boxes_w / 2
        y_from = upper["bottom"]
        y_to = lower["top"]
        mid = (y_from + y_to) / 2
        label = _protocol_between(model, lower["title"], upper["title"])
        _arrow(d, cx, y_from - 6, cx, y_to + 6, color=BLUE, width=1.6, head=7)
        _text(d, cx + 7, mid - 2, label, size=6.5, color=BLUE)

    # Cross-cutting rail on the right
    if cross:
        rail_x = PAGE_W - 176.0
        rail_w = 162.0
        rail_top = dia_top - 16
        rail_bottom = dia_bottom
        _text(d, rail_x + 6, rail_top, "Cross-cutting capabilities", size=7.5, font=FONT_BOLD, color=PURPLE)
        _rrect(d, rail_x, rail_bottom, rail_w, rail_top - rail_bottom - 6, r=3,
               fill=colors.HexColor("#FBF6ED"), stroke=PURPLE, stroke_width=0.8)
        chip_h = 52.0
        cursor = rail_top - 14
        for component in cross:
            if cursor - chip_h < rail_bottom + 8:
                break
            acc, light = _component_fill(component)
            _rrect(d, rail_x + 6, cursor - chip_h, rail_w - 12, chip_h, r=3, fill=light, stroke=acc, stroke_width=0.6)
            _text_center(d, rail_x + 6 + (rail_w - 12) / 2, cursor - chip_h + chip_h - 9,
                         _fit(component.name, 6.6, rail_w - 22, 1, component.name),
                         size=6.6, font=FONT_BOLD, color=PURPLE)
            _text(d, rail_x + 6 + 4, cursor - chip_h + 6, _fit(component.primary_evidence, 5.4, rail_w - 20, 1, ""),
                  size=5.4, color=MUTED)
            cursor -= chip_h + 5
        _line(d, main_right + 2, dia_top - 8, main_right + 2, rail_bottom + 4, color=PURPLE, width=1.0, dashed=(3, 3))

    _legend(d, 14, 40, [
        ("Client", LAYER_COLORS[LAYER_CLIENT][1]),
        ("API layer", LAYER_COLORS[LAYER_GATEWAY][1]),
        ("Services", LAYER_COLORS[LAYER_SERVICES][1]),
        ("Data", LAYER_COLORS[LAYER_DATA][1]),
        ("External", LAYER_COLORS[LAYER_EXTERNAL][1]),
        ("Cross-cutting", colors.HexColor("#FBF6ED")),
    ])
    note = model.notes[0] if model.notes else ""
    _note_line(d, 14, 26, note, PAGE_W - 28)
    _footer(d, 1, model)
    return d


# ── Page 2 · Component & Communication View ---------------------------------


def _columns(model: ArchitectureModel) -> list[tuple[str, list[ArchitectureComponent], int]]:
    def cap(components: list[ArchitectureComponent], max_per: int) -> tuple[list[ArchitectureComponent], int]:
        return components[:max_per], len(components) - len(components[:max_per])

    clients, _ = cap(model.by_kind(KIND_CLIENT), 2)
    controllers, cx = cap(model.by_kind(KIND_CONTROLLER) + model.by_kind(KIND_GATEWAY), 6)
    services, sx = cap(model.by_kind(KIND_SERVICE), 6)
    stores, dx = cap(
        model.by_kind(KIND_DATABASE) + model.by_kind(KIND_CACHE) + model.by_kind(KIND_MESSAGE_BROKER), 6
    )
    externals, ex = cap(model.by_kind(KIND_EXTERNAL), 4)
    columns: list[tuple[str, list[ArchitectureComponent], int]] = [
        ("Client / API Entry", clients + controllers, cx),
        ("Services & Modules", services, sx),
        ("Data & Integration", stores, dx),
        ("External Systems", externals, ex),
    ]
    return [column for column in columns if column[1] or column[2]]


def _page_component_communication(model: ArchitectureModel) -> Drawing:
    d = _new_page(2, "Component & Communication View", model.repo)
    columns = _columns(model)

    col_gap = 36.0
    total_cols = len(columns)
    if total_cols == 0:
        _group_box(d, PAGE_W / 2 - 200, PAGE_H / 2 - 30, 400, 60,
                   "No components detected in the sampled files.", AMBER, LIGHT_AMBER)
        _footer(d, 2, model)
        return d
    col_w = (PAGE_W - 28 - (total_cols - 1) * col_gap) / total_cols

    top = PAGE_H - 56.0
    bottom = 150.0
    row_h = 74.0
    row_gap = 10.0
    positions: dict[str, tuple[float, float, float, float]] = {}
    col_order: dict[str, int] = {}

    for col_index, (title, components, extra) in enumerate(columns):
        col_order.update({c.component_id: col_index for c in components})
        x = 14 + col_index * (col_w + col_gap)
        _text(d, x, top + 3, title, size=7.5, font=FONT_BOLD, color=MUTED)
        cursor = top - 6
        for component in components:
            if cursor - row_h < bottom:
                break
            positions[component.component_id] = (x, cursor - row_h, col_w - 4, row_h)
            cursor -= row_h + row_gap
        if extra:
            _group_box(d, x, cursor - row_h - 6, col_w - 4, 20, f"+{extra} more grouped", AMBER, LIGHT_AMBER)

    for component in model.components:
        pos = positions.get(component.component_id)
        if not pos:
            continue
        _component_box(d, *pos, component, show_evidence=True, title_size=7.0, fact_lines=2)

    primary_ids = _primary_flow_ids(model)
    gap_arrows: dict[int, list[tuple[Any, tuple[float, float, float, float], tuple[float, float, float, float]]]] = {}
    for rel in model.relationships:
        source = positions.get(rel.source)
        target = positions.get(rel.target)
        if not source or not target:
            continue
        src_col = col_order.get(rel.source)
        dst_col = col_order.get(rel.target)
        if src_col is None or dst_col is None or dst_col != src_col + 1:
            continue
        gap_arrows.setdefault(src_col, []).append((rel, source, target))

    for src_col, items in gap_arrows.items():
        col_x = 14 + src_col * (col_w + col_gap)
        gap_start = col_x + col_w - 4 + 2
        gap_end = col_x + col_w + col_gap - 2
        items = sorted(items, key=lambda item: (item[1][1], item[2][1]))
        count = len(items)
        lane_step = min(6.0, max(2.0, (gap_end - gap_start - 8) / max(1, count)))
        center = (gap_start + gap_end) / 2
        for index, (rel, source, target) in enumerate(items):
            lane_x = center + (index - (count - 1) / 2) * lane_step
            sx, sy_top, sw, sh = source
            tx, ty_top, tw, th = target
            sy = sy_top - sh / 2
            ty = ty_top - th / 2
            highlighted = rel.source in primary_ids and rel.target in primary_ids
            color = RED if highlighted else BLUE
            width = 1.5 if highlighted else 1.1
            if ty <= sy:
                points = [(sx + sw, sy), (lane_x, sy), (lane_x, ty + th), (tx, ty + th)]
            else:
                points = [(sx + sw, sy), (lane_x, sy), (lane_x, ty), (tx, ty)]
            _polyline(d, points, color=color, width=width, arrow=True, head=5.5)

    _text(d, 14, bottom - 26, "Primary path is highlighted in red.", size=6.5, font=FONT_BOLD, color=RED)
    _legend(d, 170, bottom - 26, [
        ("Client / API", LAYER_COLORS[LAYER_CLIENT][1]),
        ("Services", LAYER_COLORS[LAYER_SERVICES][1]),
        ("Data", LAYER_COLORS[LAYER_DATA][1]),
        ("External", LAYER_COLORS[LAYER_EXTERNAL][1]),
    ])
    note = "Only relationships between adjacent columns are drawn; skipping columns would cross boxes."
    _note_line(d, 14, bottom - 40, note, PAGE_W - 28)
    _footer(d, 2, model)
    return d


# ── Page 3 · Application Flows ----------------------------------------------


def _flow_chain_h(d: Drawing, x: float, cell_w: float, flow: ArchitectureFlow,
                  accent: colors.Color, light: colors.Color, y_bottom: float) -> None:
    steps = flow.steps[:4]
    n = len(steps)
    box_h = 34.0
    arrow_len = 26.0
    step_w = min(120.0, (cell_w - 44.0 - (n - 1) * arrow_len) / max(1, n))
    total_w = n * step_w + (n - 1) * arrow_len
    start_x = x + (cell_w - total_w) / 2
    for index, step in enumerate(steps):
        bx = start_x + index * (step_w + arrow_len)
        _rrect(d, bx, y_bottom, step_w, box_h, r=3, fill=light, stroke=accent, stroke_width=0.8)
        _text_center(d, bx + step_w / 2, y_bottom + box_h / 2, _fit(step, 6.6, step_w - 6, 1, step),
                     size=6.6, font=FONT_BOLD, color=INK)
        if index < n - 1:
            _arrow(d, bx + step_w + 1, y_bottom + box_h / 2, bx + step_w + arrow_len - 1,
                   y_bottom + box_h / 2, color=accent, width=1.3, head=5)


def _flow_chain_v(d: Drawing, x: float, cell_w: float, flow: ArchitectureFlow,
                  accent: colors.Color, light: colors.Color, y_top: float) -> None:
    steps = flow.steps[:5]
    box_h = 24.0
    arrow_len = 8.0
    box_w = 150.0
    cursor = y_top
    for index, step in enumerate(steps):
        bottom = cursor - box_h
        bx = x + cell_w / 2 - box_w / 2
        _rrect(d, bx, bottom, box_w, box_h, r=3, fill=light, stroke=accent, stroke_width=0.8)
        _text_center(d, bx + box_w / 2, bottom + box_h / 2, _fit(step, 6.4, box_w - 6, 1, step),
                     size=6.4, font=FONT_BOLD, color=INK)
        if index < len(steps) - 1:
            _arrow(d, bx + box_w / 2, bottom - 1, bx + box_w / 2, bottom - arrow_len + 1,
                   color=accent, width=1.2, head=4.5)
        cursor = bottom - arrow_len


def _flow_evidence(model: ArchitectureModel, flow: ArchitectureFlow) -> str:
    by_name = {component.name: component for component in model.components}
    files: list[str] = []
    for step in flow.steps:
        component = by_name.get(step)
        if component and component.primary_evidence:
            files.append(component.primary_evidence)
    return "Observed in: " + ", ".join(files[:5])


def _page_application_flows(model: ArchitectureModel) -> Drawing:
    d = _new_page(3, "Key Application Flows", model.repo)
    flows = model.flows[:4]

    top = PAGE_H - 48.0
    bottom = 66.0
    if not flows:
        _group_box(d, PAGE_W / 2 - 240, (top + bottom) / 2 - 30, 480, 60,
                   "No application flows could be derived from the sampled files.", AMBER, LIGHT_AMBER)
        _footer(d, 3, model)
        return d

    accent_colors = (AMBER, GREEN, BLUE, PURPLE)
    light_colors = (LIGHT_AMBER, LIGHT_GREEN, LIGHT_BLUE, LIGHT_PURPLE)
    n_cols = 2
    n_rows = -(-len(flows) // n_cols)
    cell_gap = 14.0
    cell_w = (PAGE_W - 28 - (n_cols - 1) * cell_gap) / n_cols
    cell_h = (top - bottom - (n_rows - 1) * 10.0) / n_rows

    for index, flow in enumerate(flows):
        row = index // n_cols
        col = index % n_cols
        cx = 14 + col * (cell_w + cell_gap)
        cy = top - (row + 1) * cell_h - row * 10.0
        accent = accent_colors[index % len(accent_colors)]
        light = light_colors[index % len(light_colors)]
        _rrect(d, cx, cy, cell_w, cell_h, r=4, fill=SOFT, stroke=BORDER_LIGHT, stroke_width=0.7)
        _text(d, cx + 12, cy + cell_h - 16, flow.name, size=9, font=FONT_BOLD, color=accent)
        inner_top = cy + cell_h - 22
        if len(flow.steps) <= 4:
            _flow_chain_h(d, cx + 2, cell_w - 4, flow, accent, light, inner_top - 44)
        else:
            _flow_chain_v(d, cx + 2, cell_w - 4, flow, accent, light, inner_top - 6)
        desc = _fit(flow.description, 6.3, cell_w - 24, 1, flow.description)
        _text(d, cx + 12, cy + 20, desc, size=6.3, color=MUTED)
        ev = _flow_evidence(model, flow)
        _text(d, cx + 12, cy + 12, _fit(ev, 5.6, cell_w - 24, 1, ev), size=5.6, font=FONT_BOLD, color=accent)

    _legend(d, 14, 44, [("Flow (request -> response)", RED)])
    _note_line(d, 170, 46, "Each flow is derived from observed source; explanations are one sentence.", PAGE_W - 200)
    _footer(d, 3, model)
    return d


# ── Page 4 · Data + Integration Architecture --------------------------------


def _related_store_arrows(model: ArchitectureModel, consumers: list[ArchitectureComponent],
                          stores: list[ArchitectureComponent]) -> list[tuple[ArchitectureComponent, ArchitectureComponent]]:
    store_ids = {store.component_id for store in stores}
    consumer_ids = {consumer.component_id for consumer in consumers}
    pairs: list[tuple[ArchitectureComponent, ArchitectureComponent]] = []
    for rel in model.relationships:
        if rel.source in consumer_ids and rel.target in store_ids:
            source = model.component(rel.source)
            target = model.component(rel.target)
            if source and target:
                pairs.append((source, target))
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[ArchitectureComponent, ArchitectureComponent]] = []
    for source, target in pairs:
        key = (source.component_id, target.component_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append((source, target))
    return unique


def _page_data_integration(model: ArchitectureModel) -> Drawing:
    d = _new_page(4, "Data & Integration Architecture", model.repo)

    top = PAGE_H - 48.0
    bottom = 70.0
    stores = model.by_kind(KIND_DATABASE) + model.by_kind(KIND_CACHE) + model.by_kind(KIND_MESSAGE_BROKER)
    externals = model.by_kind(KIND_EXTERNAL)

    consumers = [
        component
        for component in model.components
        if component.kind in {KIND_SERVICE, KIND_CONTROLLER, KIND_GATEWAY, KIND_CLIENT}
    ]
    store_pairs = _related_store_arrows(model, consumers, stores)

    # ── Panel A · application data & infrastructure ─────────────────────────
    panel_a_h = 236.0
    panel_a_bottom = top - panel_a_h
    _text(d, 14, top - 4, "Application Data & Infrastructure", size=9.5, font=FONT_BOLD, color=NAVY)

    left_x = 14.0
    left_w = 236.0
    gap_w = 96.0
    right_x = left_x + left_w + gap_w
    right_w = 236.0
    type_x = right_x + right_w + 12.0

    box_h = 40.0
    row_gap = 8.0
    avail_top = top - 18
    avail_bottom = panel_a_bottom + 8
    max_rows = max(1, int((avail_top - avail_bottom + row_gap) / (box_h + row_gap)))

    left_ids = {component.component_id for component, _ in store_pairs}
    consumers_placed: list[ArchitectureComponent] = [c for c in consumers if c.component_id in left_ids]
    if not consumers_placed and consumers:
        consumers_placed = consumers[:1]
    for extra in consumers:
        if len(consumers_placed) >= max_rows:
            break
        if extra.component_id not in left_ids and extra not in consumers_placed:
            consumers_placed.append(extra)
    consumers_placed = consumers_placed[:max_rows]

    store_set = stores[:max_rows]

    cursor = avail_top
    left_pos: dict[str, tuple[float, float]] = {}
    for component in consumers_placed:
        left_pos[component.component_id] = (left_x + left_w / 2, cursor - box_h / 2)
        _component_box(d, left_x, cursor - box_h, left_w, box_h, component, title_size=6.8, compact=True)
        cursor -= box_h + row_gap

    cursor = avail_top
    right_pos: dict[str, tuple[float, float]] = {}
    for store in store_set:
        right_pos[store.component_id] = (right_x + right_w / 2, cursor - box_h / 2)
        acc, light = _component_fill(store)
        _component_box(d, right_x, cursor - box_h, right_w, box_h, store, title_size=6.8, compact=True)
        kind_label = {
            KIND_DATABASE: "database", KIND_CACHE: "cache", KIND_MESSAGE_BROKER: "message broker",
        }.get(store.kind, "storage")
        _rrect(d, type_x, cursor - box_h, 6, box_h, r=1, fill=acc, stroke=acc)
        _text(d, type_x + 12, cursor - box_h + box_h / 2 - 2.5,
              f"{store.name} · {kind_label}", size=6.0, font=FONT_BOLD, color=acc)
        cursor -= box_h + row_gap

    for source, target in store_pairs:
        sp = left_pos.get(source.component_id)
        if not sp:
            continue
        tp = right_pos.get(target.component_id)
        if not tp:
            continue
        acc, _ = _component_fill(target)
        sx, sy = sp
        tx, ty = tp
        lane_x = right_x - gap_w * 0.5
        if ty <= sy:
            points = [(left_x + left_w, sy), (lane_x, sy), (lane_x, ty + box_h), (right_x, ty + box_h)]
        else:
            points = [(left_x + left_w, sy), (lane_x, sy), (lane_x, ty), (right_x, ty)]
        _polyline(d, points, color=acc, width=1.3, arrow=True, head=5.5)

    if not stores:
        _group_box(d, left_x, avail_top - 90, left_w + gap_w + right_w, 44,
                   "No database, cache or message broker was observed in the sampled files.", AMBER, LIGHT_AMBER)
    else:
        _note_line(d, left_x, panel_a_bottom - 4,
                   "Arrows are drawn only where the observed service code references the store.",
                   PAGE_W - 28)

    # ── Panel B · external runtime systems ──────────────────────────────────
    panel_b_top = panel_a_bottom - 14.0
    panel_b_bottom = bottom
    _text(d, 14, panel_b_top - 2, "External Runtime Systems", size=9.5, font=FONT_BOLD, color=NAVY)

    ext_rows = externals[:4]
    if not ext_rows:
        _group_box(d, 14, panel_b_bottom + 30, PAGE_W - 28, 56,
                   "No runtime-external systems observed. Package registries, documentation and "
                   "build-tooling URLs are never modelled as runtime dependencies.", GREY, LIGHT_GREY)
    else:
        _rrect(d, 14, panel_b_bottom + 22, 210, 56, r=4, fill=LIGHT_GREEN, stroke=GREEN, stroke_width=1.0)
        _text_center(d, 14 + 105, panel_b_bottom + 22 + 38, "Application", size=8.5, font=FONT_BOLD, color=GREEN)
        _text_center(d, 14 + 105, panel_b_bottom + 22 + 24,
                     "backend services & modules", size=6.5, color=INK)
        ext_col_x = 300.0
        ext_w = PAGE_W - ext_col_x - 14.0
        box_h = 34.0
        cursor = panel_b_top - 20
        for ext in ext_rows:
            if cursor - box_h < panel_b_bottom + 8:
                break
            acc, light = _component_fill(ext)
            _component_box(d, ext_col_x, cursor - box_h, ext_w, box_h, ext, title_size=6.8, compact=True)
            _arrow(d, 14 + 210, panel_b_bottom + 22 + 28, ext_col_x, cursor - box_h / 2,
                   color=BLUE, width=1.2, head=5.5)
            cursor -= box_h + 8
        _note_line(d, ext_col_x, panel_b_bottom + 4,
                   "External systems are listed only when application code calls them at runtime.",
                   ext_w - 4)

    _legend(d, 14, 42, [
        ("Database", LIGHT_AMBER),
        ("Cache", LIGHT_TEAL),
        ("Message broker", LIGHT_PURPLE),
        ("External", LIGHT_GREY),
    ])
    _footer(d, 4, model)
    return d


# ── Page 5 · Deployment Architecture ----------------------------------------


_DEPLOY_ORDER = {
    "repository": 0, "ci": 1, "image": 2, "orchestration": 3,
    "platform": 4, "iaas": 5, "cloud": 6,
}


def _deployment_chain(model: ArchitectureModel) -> list[tuple[str, str, list[str]]]:
    nodes: list[tuple[str, str, list[str]]] = [("Source Repository (GitHub)", "repository", [model.repo])]
    for component in sorted(model.deployment, key=lambda c: _DEPLOY_ORDER.get(c.kind, 9)):
        nodes.append((component.name, component.kind, component.evidence))
    return nodes


def _page_deployment_architecture(model: ArchitectureModel) -> Drawing:
    d = _new_page(5, "Deployment Architecture", model.repo)

    top = PAGE_H - 48.0
    bottom = 84.0
    nodes = _deployment_chain(model)

    if len(nodes) <= 1:
        cx = PAGE_W / 2
        _rrect(d, cx - 300, (top + bottom) / 2 - 66, 600, 132, r=8, fill=SOFT, stroke=AMBER, stroke_width=1.0)
        _text_center(d, cx, (top + bottom) / 2 + 26, "Deployment architecture is not mapped", size=13,
                     font=FONT_BOLD, color=NAVY)
        _text_center(d, cx, (top + bottom) / 2 + 2,
                     "No Dockerfile, Compose, Kubernetes, Helm, Terraform or CI/CD configuration was found.",
                     size=8, color=INK)
        _text_center(d, cx, (top + bottom) / 2 - 16,
                     "No deployment pipeline is shown rather than inventing one.",
                     size=7.5, color=MUTED)
        _footer(d, 5, model)
        return d

    arrow_len = 30.0
    node_w = min(150.0, (PAGE_W - 28 - (len(nodes) - 1) * arrow_len) / len(nodes))
    total_w = len(nodes) * node_w + (len(nodes) - 1) * arrow_len
    start_x = (PAGE_W - total_w) / 2
    cy = (top + bottom) / 2
    box_h = 132.0
    for index, (name, kind, evidence) in enumerate(nodes):
        nx = start_x + index * (node_w + arrow_len)
        acc, light = {
            "repository": (NAVY, LIGHT_BLUE),
            "ci": (BLUE, LIGHT_BLUE),
            "image": (GREEN, LIGHT_GREEN),
            "orchestration": (AMBER, LIGHT_AMBER),
            "platform": (PURPLE, LIGHT_PURPLE),
            "iaas": (TEAL, LIGHT_TEAL),
            "cloud": (GREY, LIGHT_GREY),
        }.get(kind, (GREY, LIGHT_GREY))
        _rrect(d, nx, cy - box_h / 2, node_w, box_h, r=4, fill=light, stroke=acc, stroke_width=0.9)
        _text_center(d, nx + node_w / 2, cy + box_h / 2 - 22, _fit(name, 7.2, node_w - 10, 2, name),
                     size=7.2, font=FONT_BOLD, color=acc)
        _text_center(d, nx + node_w / 2, cy + box_h / 2 - 42, kind.title(), size=5.5, color=MUTED)
        _line(d, nx + 6, cy + 22, nx + node_w - 6, cy + 22, color=acc, width=0.5)
        evidence_line = _fit(", ".join(evidence), 5.6, node_w - 10, 1, "")
        _text_center(d, nx + node_w / 2, cy + 10, evidence_line, size=5.6, color=MUTED)
        if index < len(nodes) - 1:
            _arrow(d, nx + node_w + 1, cy, nx + node_w + arrow_len - 1, cy,
                   color=BLUE, width=1.4, head=6)

    _note_line(d, 14, 56, "Pipeline steps are shown only for assets actually present in the repository.",
               PAGE_W - 28)
    _note_line(d, 14, 42, "Source Repository reflects the analysed GitHub repository.",
               PAGE_W - 28)
    _footer(d, 5, model)
    return d


# ── Page 6 · Repository Structure & Code Flow -------------------------------


def _page_repository_structure(model: ArchitectureModel) -> Drawing:
    d = _new_page(6, "Repository Structure & Code Flow", model.repo)

    top = PAGE_H - 48.0
    bottom = 44.0

    # ── Left · directory & file map ─────────────────────────────────────────
    left_x = 14.0
    left_w = 396.0
    _text(d, left_x, top - 2, "Directory & file map", size=9.5, font=FONT_BOLD, color=NAVY)

    role_colors = {
        "entry": BLUE, "routes": BLUE, "service": GREEN, "data": AMBER,
        "cache": TEAL, "broker": PURPLE, "external": GREY, "client": NAVY,
        "UI": NAVY, "backend": GREEN, "API": BLUE, "app": GREEN,
        "domain": GREEN, "tests": GREY, "CI": PURPLE, "infra": TEAL,
        "db": AMBER, "config": GREY, "docs": GREY, "shared": GREY,
        "tooling": GREY, "middleware": PURPLE, "static": GREY,
        "services": GREEN,
    }

    rows = list(model.tree)
    row_h = 12.2
    cursor = top - 14
    shown = 0
    for node in rows:
        if cursor - row_h < bottom + 16:
            break
        if shown >= 30:
            break
        indent = 9 + (node.depth - 1) * 11
        glyph = "[-] " if node.is_dir else "    "
        _text(d, left_x + indent, cursor - 4,
              glyph + node.name if node.is_dir else ("    " if node.depth == 1 else "  ") + node.name,
              size=6.4, font=FONT_BOLD if node.is_dir else FONT, color=NAVY if node.is_dir else INK)
        if node.role:
            name_w = (_char_width(6.4) * (len(glyph) + len(node.name) + 1)) + indent
            role_x = left_x + indent + max(name_w, 20.0)
            _text(d, role_x, cursor - 4, node.role, size=5.6, color=role_colors.get(node.role, MUTED))
        cursor -= row_h
        shown += 1

    if shown < len(rows):
        _text(d, left_x, cursor - 2, f"+{len(rows) - shown} more entries", size=6.2,
              font=FONT_BOLD, color=AMBER)

    if not rows:
        _group_box(d, left_x, (top + bottom) / 2 - 30, left_w, 60,
                   "The repository tree could not be derived from the sampled files.", AMBER, LIGHT_AMBER)

    _note_line(d, left_x, bottom + 30,
               "Dirs are shown only for sampled paths; roles mark entry, routes, services, data and externals.",
               left_w)

    # ── Right · observed code flow ─────────────────────────────────────────
    right_x = left_x + left_w + 14.0
    right_w = PAGE_W - right_x - 14.0
    _text(d, right_x, top - 2, "Observed code flow", size=9.5, font=FONT_BOLD, color=NAVY)

    by_name = {component.name: component for component in model.components}
    flow = model.flows[0] if model.flows else None
    flow_accents = (BLUE, GREEN, AMBER, PURPLE, GREY)
    flow_lights = (LIGHT_BLUE, LIGHT_GREEN, LIGHT_AMBER, LIGHT_PURPLE, LIGHT_GREY)
    cursor = top - 14
    if flow:
        steps = flow.steps[:5]
        box_w = min(300.0, right_w - 6)
        box_h = 28.0
        for index, step in enumerate(steps):
            component = by_name.get(step)
            acc = flow_accents[index % len(flow_accents)]
            light = flow_lights[index % len(flow_lights)]
            bx = right_x + (right_w - box_w) / 2
            bottom_y = cursor - box_h
            _rrect(d, bx, bottom_y, box_w, box_h, r=3, fill=light, stroke=acc, stroke_width=0.7)
            _text_center(d, bx + box_w / 2, cursor - 8.5,
                         _fit(step, 6.6, box_w - 10, 1, step), size=6.6, font=FONT_BOLD, color=INK)
            ev = _fit(component.primary_evidence if component else "", 5.6, box_w - 10, 1, "")
            if ev:
                _text_center(d, bx + box_w / 2, cursor - 17.5, ev, size=5.6, color=MUTED)
            if index < len(steps) - 1:
                _arrow(d, bx + box_w / 2, bottom_y - 1, bx + box_w / 2, bottom_y - 9,
                       color=acc, width=1.2, head=4.5)
            cursor = bottom_y - 10
        if shown_flow := flow.description:
            _text(d, right_x, cursor - 4, _fit(shown_flow, 6.2, right_w, 2, shown_flow),
                  size=6.2, color=MUTED)
            cursor -= 18
    else:
        _group_box(d, right_x, cursor - 40, right_w, 34,
                   "No call path could be derived from the sampled files.", AMBER, LIGHT_AMBER)
        cursor -= 50

    _line(d, right_x, cursor - 4, right_x + right_w, cursor - 4, color=BORDER_LIGHT, width=0.6)
    cursor -= 12

    # ── Right · repository areas at a glance ────────────────────────────────
    _text(d, right_x, cursor, "Repository areas at a glance", size=9.5, font=FONT_BOLD, color=NAVY)
    cursor -= 14
    areas = model.structure[:9]
    for area in areas:
        if cursor - 34 < bottom + 4:
            break
        _text(d, right_x, cursor, area.name, size=6.8, font=FONT_BOLD, color=NAVY)
        _text(d, right_x, cursor - 8, _fit(area.purpose, 6.0, right_w, 1, area.purpose), size=6.0, color=INK)
        ev = _fit(", ".join(area.evidence), 5.4, right_w, 1, "")
        _text(d, right_x, cursor - 15, ev, size=5.4, color=MUTED)
        cursor -= 25

    if not areas:
        _text(d, right_x, cursor, "No repository areas could be derived.", size=6.5, color=MUTED)

    _footer(d, 6, model)
    return d


# ── Page 7 · Security Architecture ------------------------------------------


def _page_security_architecture(model: ArchitectureModel) -> Drawing:
    d = _new_page(7, "Security Architecture", model.repo)

    top = PAGE_H - 48.0
    bottom = 44.0
    categories = model.security_categories()

    _text(d, 14, top - 2,
          "Security controls observed in the repository (rule-based; only controls with evidence are shown).",
          size=7.0, color=MUTED)

    total_features = sum(len(features) for _, features in categories)
    if total_features:
        summary = f"{total_features} controls across {len(categories)} areas"
        _text(d, 14, top - 12, summary, size=8.5, font=FONT_BOLD, color=NAVY)

    if not categories:
        _group_box(d, PAGE_W / 2 - 300, (top + bottom) / 2 - 46, 600, 92,
                   "No explicit security controls were observed in the sampled files.",
                   AMBER, LIGHT_AMBER)
        _note_line(d, PAGE_W / 2 - 240, (top + bottom) / 2 - 88,
                   "Absence here only reflects what the sampled files did not reveal - it is not a verdict.",
                   PAGE_W - 200)
        _footer(d, 7, model)
        return d

    n_cols = 3
    col_gap = 14.0
    col_w = (PAGE_W - 28 - (n_cols - 1) * col_gap) / n_cols
    card_h = 118.0
    card_gap = 12.0
    accent_cycle = (BLUE, GREEN, AMBER, PURPLE, TEAL, GREY, RED)

    cursor = top - 22
    for index, (category, features) in enumerate(categories):
        col = index % n_cols
        row = index // n_cols
        x = 14 + col * (col_w + col_gap)
        y = cursor - (row + 1) * card_h - row * card_gap
        acc = accent_cycle[index % len(accent_cycle)]
        _rrect(d, x, y, col_w, card_h, r=4, fill=SOFT, stroke=acc, stroke_width=0.8)
        _rrect(d, x, y + card_h - 16, col_w, 16, r=4, fill=acc, stroke=acc)
        count = len(features)
        _text(d, x + 8, y + card_h - 11.5, category, size=7.2, font=FONT_BOLD, color=WHITE)
        _text(d, x + col_w - 8, y + card_h - 11.5, str(count), size=7.0, font=FONT_BOLD,
              color=WHITE, anchor="end")
        inner = y + card_h - 22
        for feature in features[:3]:
            _text(d, x + 8, inner, _fit(feature.name, 6.2, col_w - 16, 1, feature.name),
                  size=6.2, font=FONT_BOLD, color=acc)
            inner -= 8
            desc_lines = _wrap(feature.description, 5.7, col_w - 16, max_lines=2)
            for desc_line in desc_lines:
                _text(d, x + 8, inner, desc_line, size=5.7, color=INK)
                inner -= 6.6
            ev = _fit(", ".join(feature.evidence), 5.2, col_w - 16, 1, "")
            if ev:
                _text(d, x + 8, inner - 1, ev, size=5.2, color=MUTED)
                inner -= 5.4
            inner -= 3
        if count > 3:
            _text(d, x + 8, inner - 2, f"+{count - 3} more", size=6.0, font=FONT_BOLD, color=AMBER)

    rows_total = -(-len(categories) // n_cols)
    _note_line(d, 14, bottom + 30,
               "Security is derived from code/config evidence: auth flows, validation, transport, secrets, tooling.",
               PAGE_W - 200)
    _note_line(d, 14, bottom + 20,
               f"{rows_total} row{'s' if rows_total != 1 else ''} of cards; each card lists up to three controls with evidence.",
               PAGE_W - 200)
    _footer(d, 7, model)
    return d


# ── Page 8 · Architecture Style & Mapping Tables -----------------------------


def _grid_table(d: Drawing, x: float, top: float, width: float, *,
                title: str, headers: Sequence[str], rows: Sequence[Sequence[str]],
                col_ratios: Sequence[float], accent: colors.Color,
                empty_text: str = "None observed in the sampled repository.",
                header_size: float = 6.4, cell_size: float = 6.0,
                row_h: float = 14.0, max_rows: int = 12) -> float:
    """Draw a titled, zebra-striped mapping table. Returns its bottom Y."""
    title_h = 15.0
    _rrect(d, x, top - title_h, width, title_h, r=3, fill=accent, stroke=accent)
    _text(d, x + 7, top - title_h + 4.5, title, size=7.2, font=FONT_BOLD, color=WHITE)

    # Column geometry from ratios.
    ratio_sum = sum(col_ratios) or 1.0
    inner_w = width - 8.0
    xs: list[float] = [x + 4.0]
    for ratio in col_ratios:
        xs.append(xs[-1] + inner_w * (ratio / ratio_sum))

    # Header row.
    head_y = top - title_h - row_h
    _rrect(d, x, head_y, width, row_h, r=0, fill=LIGHT_GREY, stroke=BORDER_LIGHT, stroke_width=0.5)
    for index, header in enumerate(headers):
        cell_w = xs[index + 1] - xs[index] - 6
        _text(d, xs[index] + 2, head_y + 4.0, _fit(header, header_size, cell_w, 1, header),
              size=header_size, font=FONT_BOLD, color=NAVY)

    body_rows = list(rows)[:max_rows]
    cursor = head_y
    if not body_rows:
        cursor -= row_h
        _rrect(d, x, cursor, width, row_h, r=0, fill=WHITE, stroke=BORDER_LIGHT, stroke_width=0.4)
        _text(d, xs[0] + 2, cursor + 4.0, empty_text, size=cell_size, color=MUTED)
        _rrect(d, x, cursor, width, top - cursor, r=3, fill=None, stroke=accent, stroke_width=0.8)
        return cursor
    for r_index, row in enumerate(body_rows):
        cursor -= row_h
        fill = SOFT if r_index % 2 else WHITE
        _rrect(d, x, cursor, width, row_h, r=0, fill=fill, stroke=BORDER_LIGHT, stroke_width=0.4)
        for c_index, cell in enumerate(row):
            if c_index >= len(xs) - 1:
                break
            cell_w = xs[c_index + 1] - xs[c_index] - 6
            font = FONT_BOLD if c_index == 0 else FONT
            color = INK if c_index == 0 else GREY
            _text(d, xs[c_index] + 2, cursor + 4.0, _fit(str(cell), cell_size, cell_w, 1, str(cell)),
                  size=cell_size, font=font, color=color)
    total = len(rows)
    if total > max_rows:
        cursor -= 9.0
        _text(d, xs[0] + 2, cursor + 2.0, f"+{total - max_rows} more not shown",
              size=5.6, font=FONT_BOLD, color=accent)
    # Outer border around the whole table.
    _rrect(d, x, cursor, width, top - cursor, r=3, fill=None, stroke=accent, stroke_width=0.8)
    return cursor


def _style_signals(model: ArchitectureModel) -> list[tuple[str, str]]:
    """Evidence-backed signals that justify the inferred architectural style."""
    signals: list[tuple[str, str]] = []
    controllers = model.by_kind(KIND_CONTROLLER)
    services = model.by_kind(KIND_SERVICE)
    clients = model.by_kind(KIND_CLIENT)
    externals = model.by_kind(KIND_EXTERNAL)
    dbs = model.by_kind(KIND_DATABASE)
    caches = model.by_kind(KIND_CACHE)
    brokers = model.by_kind(KIND_MESSAGE_BROKER)
    if clients:
        signals.append(("Presentation tier", f"{len(clients)} client/UI component(s)"))
    if controllers:
        signals.append(("API surface", f"{len(controllers)} controller(s) exposing HTTP routes"))
    if services:
        signals.append(("Service layer", f"{len(services)} service/module component(s)"))
    if dbs:
        signals.append(("Persistence", f"{len(dbs)} relational/document store(s)"))
    if caches:
        signals.append(("Caching tier", f"{len(caches)} in-memory store(s)"))
    if brokers:
        signals.append(("Asynchronous messaging", f"{len(brokers)} message broker(s) → event-driven traits"))
    if externals:
        signals.append(("External integrations", f"{len(externals)} third-party service(s)"))
    if model.protocols:
        signals.append(("Communication", ", ".join(model.protocols[:4])))
    return signals


def _page_mapping_tables(model: ArchitectureModel) -> Drawing:
    d = _new_page(8, "Architecture Style & Mapping Tables", model.repo)

    top = PAGE_H - 46.0
    bottom = 34.0

    # ── Style banner ────────────────────────────────────────────────────────
    banner_h = 26.0
    style_label = model.style or "Architecture style not inferred"
    _rrect(d, 14, top - banner_h, PAGE_W - 28, banner_h, r=4, fill=LIGHT_BLUE, stroke=NAVY, stroke_width=0.9)
    _text(d, 22, top - 11, "Architectural style", size=6.8, font=FONT_BOLD, color=MUTED)
    _text(d, 22, top - 21, _fit(style_label, 11.0, PAGE_W - 320, 1, style_label),
          size=11.0, font=FONT_BOLD, color=NAVY)
    signals = _style_signals(model)
    if signals:
        sig_x = PAGE_W - 300
        _text(d, sig_x, top - 8, "Why this style (evidence)", size=6.2, font=FONT_BOLD, color=NAVY)
        chip_y = top - 20
        _text(d, sig_x, chip_y, _fit("  ·  ".join(f"{k}: {v}" for k, v in signals[:3]),
                                     5.6, 286, 1, ""), size=5.6, color=INK)

    grid_top = top - banner_h - 12.0
    col_gap = 16.0
    col_w = (PAGE_W - 28 - col_gap) / 2
    left_x = 14.0
    right_x = 14.0 + col_w + col_gap

    # ── Left column: components → layer, and style signals ──────────────────
    comp_rows = [
        [c.name, _LAYER_SHORT.get(c.layer, c.layer), _KIND_SHORT.get(c.kind, c.kind), c.primary_evidence]
        for c in model.components
    ]
    y_left = _grid_table(
        d, left_x, grid_top, col_w,
        title="Component → Layer → Type mapping",
        headers=["Component", "Layer", "Type", "Evidence"],
        rows=comp_rows, col_ratios=[2.4, 1.8, 1.6, 2.6], accent=NAVY, max_rows=13,
        empty_text="No components were detected.",
    )

    signal_rows = [[k, v] for k, v in signals]
    _grid_table(
        d, left_x, y_left - 12.0, col_w,
        title="Architectural style signals",
        headers=["Signal", "Observation"],
        rows=signal_rows, col_ratios=[1.5, 3.0], accent=TEAL, max_rows=5,
        empty_text="No distinctive style signals were observed.",
    )

    # ── Right column: tech → category, endpoints → controller ───────────────
    tech_rows = [
        [t.name, t.category, ", ".join(t.evidence[:2]) or "-"]
        for t in model.technologies
    ]
    y_right = _grid_table(
        d, right_x, grid_top, col_w,
        title="Technology → Category mapping",
        headers=["Technology", "Category", "Evidence"],
        rows=tech_rows, col_ratios=[1.8, 2.0, 2.4], accent=GREEN, max_rows=9,
        empty_text="No technologies were detected in the sampled files.",
    )

    endpoint_rows: list[list[str]] = []
    for controller in sorted(model.by_kind(KIND_CONTROLLER),
                             key=lambda c: (-(c.detail.get("routes", 0) or 0), c.name)):
        for endpoint in [str(e) for e in (controller.detail.get("endpoints") or [])][:4]:
            endpoint_rows.append([endpoint, controller.name, controller.primary_evidence])
        if not (controller.detail.get("endpoints") or []):
            endpoint_rows.append(["(routes not enumerated)", controller.name, controller.primary_evidence])
    _grid_table(
        d, right_x, y_right - 12.0, col_w,
        title="Endpoint → Controller mapping",
        headers=["Endpoint / route", "Controller", "Evidence"],
        rows=endpoint_rows, col_ratios=[2.4, 2.0, 2.2], accent=BLUE, max_rows=9,
        empty_text="No HTTP endpoints were detected (no API controllers).",
    )

    _note_line(d, 14, bottom,
               "All rows are derived from repository artefacts; anything not observed is omitted rather than assumed.",
               PAGE_W - 28)
    _footer(d, 8, model)
    return d


# ── public entry point --------------------------------------------------------


def architecture_pages(model: ArchitectureModel) -> list[Drawing]:
    """Return the eight graphical pages (in order) for the architecture report."""
    return [
        _page_application_architecture(model),
        _page_component_communication(model),
        _page_application_flows(model),
        _page_data_integration(model),
        _page_deployment_architecture(model),
        _page_repository_structure(model),
        _page_security_architecture(model),
        _page_mapping_tables(model),
    ]


def render_architecture_document(model: ArchitectureModel, *, repo_url: str | None = None) -> bytes:
    """Render the seven architecture pages into a landscape A4 PDF."""
    from io import BytesIO

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
        title=f"Architecture Overview - {model.repo}",
        author="Evidence-driven architecture analysis",
    )
    story: list[Any] = []
    for index, page in enumerate(architecture_pages(model)):
        if index:
            story.append(PageBreak())
        story.append(page)
    doc.build(story)
    return buffer.getvalue()
