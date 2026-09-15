"""Exporter registry.

The exporter registry decouples export orchestration from concrete generators:
adding a new format (e.g. ``odt``) is a matter of implementing
:class:`Exporter` and registering it here - the agent/ExportService orchestration
stays unchanged. Exporters appear in the registry only when their underlying
library is available at runtime, which is exactly the "available exporters" fact
the intent validation is based on.

:func:`available_formats` returns the current set of format names that a generated
export may target. ``native`` is handled specially (passthrough of stored bytes)
and is always available.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.export.formats import ExportFormat


@dataclass
class ExportPayload:
    """Everything an exporter needs to render one generated artifact."""

    content: str = ""
    title: str = ""
    subtitle: str = ""
    sections: list[ExportSection] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    source_url: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    # Optional rendered architecture diagram (PNG bytes) for image-capable exporters.
    diagram_png: bytes | None = None
    # Structured rows for tabular exports (csv/xlsx); each row is a list of cells.
    rows: list[list[str]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)


@dataclass
class ExportSection:
    """A heading + body triple used to structure generated documents/reports.

    ``rows``/``headers`` (optional) carry the same content as a tabular grid so
    table-capable exporters (DOCX/PDF/HTML/MD/TXT) can render it aligned instead
    of as free-form prose.
    """

    heading: str
    body: str
    evidence: str | None = None  # e.g. "Observed from source" / URL
    rows: list[list[str]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)


class Exporter:
    """Base class for generated-format exporters (content -> bytes)."""

    format_name: str

    def render(self, payload: ExportPayload) -> bytes:
        raise NotImplementedError


class NativeExporter(Exporter):
    """Passthrough for stored original bytes (no rendering)."""

    format_name = "native"

    def render(self, payload: ExportPayload) -> bytes:
        data = payload.metadata.get("native_bytes")
        if not isinstance(data, bytes):
            raise ValueError("Native exporter requires metadata['native_bytes'].")
        return data


class _Registry:
    def __init__(self) -> None:
        self._exporters: dict[str, Exporter] = {}

    def register(self, exporter: Exporter) -> None:
        self._exporters[exporter.format_name] = exporter

    def get(self, format_name: str) -> Exporter | None:
        return self._exporters.get(format_name.lower())

    @property
    def names(self) -> set[str]:
        return set(self._exporters)


def build_registry() -> _Registry:
    """Construct the default registry, registering exporters whose dependencies
    are importable. ``native`` is always present."""
    registry = _Registry()
    registry.register(NativeExporter())

    from app.export.exporters.document_exporters import (
        CsvExporter,
        HtmlExporter,
        JsonExporter,
        MarkdownExporter,
        TextExporter,
    )

    registry.register(TextExporter())
    registry.register(MarkdownExporter())
    registry.register(HtmlExporter())
    registry.register(JsonExporter())
    registry.register(CsvExporter())

    import importlib.util

    if importlib.util.find_spec("docx"):
        from app.export.exporters.document_exporters import DocxExporter

        registry.register(DocxExporter())

    if importlib.util.find_spec("openpyxl"):
        from app.export.exporters.spreadsheet_exporters import XlsxExporter

        registry.register(XlsxExporter())

    if importlib.util.find_spec("reportlab"):
        from app.export.exporters.document_exporters import PdfExporter

        registry.register(PdfExporter())

    return registry


_REGISTRY: _Registry | None = None


def registry() -> _Registry:
    global _REGISTRY  # noqa: PLW0603 - process-wide singleton registry
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return _REGISTRY


def available_formats() -> frozenset[str]:
    """Formats a generated export may currently target."""
    names = {name for name in registry().names if name != "native"}
    return frozenset(names)


def exportable_enum_formats() -> frozenset[str]:
    """The allowlisted format enum values with a live exporter (plus native)."""
    names = set(registry().names)
    names.add(ExportFormat.NATIVE.value)
    return frozenset(names)
