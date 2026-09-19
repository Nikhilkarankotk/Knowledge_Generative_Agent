"""Deterministic architecture-diagram rendering for exports.

The diagram is built exclusively from what the repository walk observed: sampled
source files are classified into layers (entry point, API/REST, business logic,
data access, data models, configuration) and rendered two ways ::

* as a plain-ASCII box diagram (safe in every text format), and
* as a PNG image embedded into DOCX/PDF exports when Pillow is available.

Nothing here invokes the LLM or guesses beyond the sampled files.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

# ── layer model -----------------------------------------------------------

# Architecture layer titles in top-down flow order.
LAYER_TITLES = (
    "Entry Point",
    "API / REST Layer",
    "Business Logic / Services",
    "Data Access",
    "Data Models",
    "Config & Infrastructure",
)


@dataclass
class ArchitectureLayer:
    """One layer of the diagram: a title plus observed component names."""

    title: str
    items: list[str] = field(default_factory=list)

    @property
    def has_items(self) -> bool:
        return bool(self.items)


# ── text (ASCII) rendering ---------------------------------------------------

def render_architecture_text(layers: list[ArchitectureLayer], *, max_items: int = 6) -> str:
    """Render the layers as a monospace box diagram like the one below.

    ::

                      [ External clients ]
                             |
                             v
        +----------------------------------+
        | ENTRY POINT                      |
        |   MainApplication                |
        +----------------------------------+
                             |
                             v
        [ Data / persistence ]
    """
    lines: list[str] = []
    lines.append("")
    _append_edge(lines, "External clients")
    for layer in layers:
        _append_arrow(lines)
        _append_box(lines, layer.title, layer.items[:max_items] or ["(none observed)"])
    _append_arrow(lines)
    _append_edge(lines, "Data / persistence")
    lines.append("")
    lines.append(
        "Legend: every box is grounded in sampled source files; the External clients and "
        "Data / persistence edges are assumed boundaries of the diagram, not observed code."
    )
    text = "\n".join(lines)
    return text.strip()


def _append_edge(lines: list[str], label: str) -> None:
    lines.append(f"          [ {label} ]")


def _append_arrow(lines: list[str]) -> None:
    lines.append("                |")
    lines.append("                v")


def _append_box(lines: list[str], title: str, items: list[str]) -> None:
    inner_width = max(
        [len(title), *(len(item) for item in items)]
    ) + 4
    border = "+" + "-" * inner_width + "+"
    lines.append("        " + border)
    space = inner_width - len(title)
    lines.append(
        "        | " + title.upper() + " " * (space - 1) + " |"
    )
    for item in items:
        padded = " " * (inner_width - len(item) - 2)
        lines.append("        |   " + item + padded + " |")
    lines.append("        " + border)


# ── PNG rendering ---------------------------------------------------------

_PNG_WIDTH = 760
_BOX_WIDTH = 600
_BOX_PAD = 18
_LINE_H = 30
_TITLE_H = 40
_ARROW_H = 46
_HEADER_H = 40
_EDGE_PILL_H = 52

_TITLE_COLOR = (40, 52, 64)
_TEXT_COLOR = (38, 42, 48)
_BORDER_COLOR = (70, 76, 84)
_ARROW_COLOR = (95, 100, 110)
_BACKGROUND = (255, 255, 255)
_EDGE_FILL = (224, 226, 230)

_LAYER_FILLS = (
    (226, 240, 255),  # entry point - light blue
    (214, 231, 255),  # api layer - blue
    (226, 240, 217),  # business logic - green
    (255, 244, 214),  # data access - amber
    (240, 240, 240),  # data models - grey
    (232, 228, 250),  # config - violet
)

_FONT_TITLE = 22
_FONT_BODY = 18


def render_architecture_png(layers: list[ArchitectureLayer]) -> bytes | None:
    """Render the layer boxes into a PNG image (``None`` when Pillow is missing)."""
    if not layers or not any(layer.has_items for layer in layers):
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:  # noqa: BLE001 - image rendering is best-effort
        return None

    try:
        title_font = ImageFont.load_default(size=_FONT_TITLE)
        body_font = ImageFont.load_default(size=_FONT_BODY)
    except TypeError:  # pragma: no cover - older Pillow without size support
        title_font = ImageFont.load_default()
        body_font = ImageFont.load_default()
        title_line_h = _LINE_H
        body_line_h = _LINE_H - 6
    else:
        title_line_h = _TITLE_H
        body_line_h = _LINE_H

    def box_height(items: list[str]) -> int:
        return _BOX_PAD * 2 + title_line_h + max(1, len(items)) * body_line_h

    total = _HEADER_H + _EDGE_PILL_H + _ARROW_H
    for layer in layers:
        total += box_height(layer.items[:5]) + _ARROW_H
    total += _EDGE_PILL_H

    image = Image.new("RGB", (_PNG_WIDTH, total), _BACKGROUND)
    draw = ImageDraw.Draw(image)

    def draw_pill(y: int, label: str, fill: tuple[int, int, int]) -> int:
        text_w = draw.textlength(label, font=body_font)  # type: ignore[arg-type]
        pill_w = max(int(text_w) + 60, 220)
        left = (_PNG_WIDTH - pill_w) // 2
        top = y
        bottom = top + _EDGE_PILL_H
        draw.rounded_rectangle(
            [left, top, left + pill_w, bottom], radius=18, fill=fill, outline=_BORDER_COLOR, width=2
        )
        draw.text(
            (left + pill_w // 2, (top + bottom) // 2),
            label,
            font=body_font,  # type: ignore[arg-type]
            fill=_TEXT_COLOR,
            anchor="mm",
        )
        return bottom

    def draw_arrow(y: int) -> int:
        cx = _PNG_WIDTH // 2
        top = y
        bottom = top + _ARROW_H
        head = bottom - 8
        draw.line((cx, top, cx, head - 2), fill=_ARROW_COLOR, width=3)
        draw.polygon([(cx - 8, head - 8), (cx + 8, head - 8), (cx, bottom)], fill=_ARROW_COLOR)
        return bottom

    def draw_box(y: int, title: str, items: list[str], fill: tuple[int, int, int]) -> int:
        height = box_height(items[:5])
        left = (_PNG_WIDTH - _BOX_WIDTH) // 2
        top = y
        bottom = top + height
        draw.rounded_rectangle(
            [left, top, left + _BOX_WIDTH, bottom], radius=10, fill=fill, outline=_BORDER_COLOR, width=2
        )
        title_y = top + _BOX_PAD + title_line_h // 2
        draw.text(
            (left + _BOX_PAD + 6, title_y),
            title,
            font=title_font,  # type: ignore[arg-type]
            fill=_TITLE_COLOR,
            anchor="lm",
        )
        row_y = title_y + title_line_h // 2 + 6
        for item in items[:5]:
            draw.text(
                (left + _BOX_PAD + 6, row_y),
                item,
                font=body_font,  # type: ignore[arg-type]
                fill=_TEXT_COLOR,
                anchor="lm",
            )
            row_y += body_line_h
        return bottom

    y = _HEADER_H
    y = draw_pill(y, "External clients", _EDGE_FILL)
    y = draw_arrow(y)
    for index, layer in enumerate(layers[: len(_LAYER_FILLS)]):
        y = draw_box(y, layer.title.upper(), layer.items[:5], _LAYER_FILLS[index])
        y = draw_arrow(y)
    draw_pill(y, "Data / persistence", _EDGE_FILL)

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
