"""Safe ZIP writer for multi-artifact exports.

Enforces the export specification's archive rules before anything is written:

* every entry name is sanitized (no traversal, no absolute/drive paths, no control
  characters) and collisions are de-duplicated deterministically;
* per-file, per-total and per-entry-count limits are enforced while streaming, so
  an oversized or hostile artifact set is refused instead of ballooning;
* ``manifest.json`` is written first with full provenance (source types, names,
  URLs, mime types, retrieval order) for every entry.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any

from app.export.errors import ExportLimitError
from app.export.formats import safe_filename

_MANIFEST_NAME = "manifest.json"

# Dangerous URL/path separators and control characters inside any single segment.
_UNSAFE_SEGMENT = re.compile(r"[\\\x00-\x1f\x7f]|[.][.][^.]|^[.]+$")


def safe_relpath(name: str, *, max_depth: int = 8) -> str:
    """Sanitize an archive-relative path, keeping directory structure.

    Splits on ``/``, sanitizes each component, drops empties and traversal
    segments, and bounds the depth so archive entries can never escape the
    archive root or encode absolute/drive-hostile paths.
    """
    raw = (name or "").strip().replace("\\", "/")
    parts: list[str] = []
    for segment in raw.split("/"):
        segment = segment.strip()
        if not segment or segment in {".", ".."}:
            continue
        segment = safe_filename(segment, default="file")
        if _UNSAFE_SEGMENT.search(segment):
            segment = "file"
        if segment:
            parts.append(segment)
        if len(parts) >= max_depth:
            break
    if not parts:
        return "file.txt"
    return "/".join(parts)


@dataclass
class ZipEntry:
    name: str
    data: bytes
    metadata: dict[str, Any] | None = None


@dataclass
class ZipResult:
    path: str
    file_count: int
    total_bytes: int


def _unique_names(desired: list[str]) -> list[str]:
    """De-duplicate the sanitized entry names deterministically."""
    used: set[str] = set()
    result: list[str] = []
    for name in desired:
        safe = safe_relpath(name)
        candidate = safe
        counter = 1
        while candidate in used:
            stem, dot, ext = safe.rpartition(".")
            if dot and len(ext) <= 10:
                candidate = f"{stem}-{counter}{dot}{ext}"
            else:
                candidate = f"{safe}-{counter}"
            counter += 1
        used.add(candidate)
        result.append(candidate)
    return result


def build_manifest(entries: list[ZipEntry]) -> dict[str, Any]:
    return {
        "export_version": "1.0",
        "generated_by": "knowledge-generative-agent",
        "file_count": len(entries),
        "entries": [
            {
                "path": entry.name,
                "source_type": (entry.metadata or {}).get("source_type"),
                "source_name": (entry.metadata or {}).get("source_name"),
                "source_url": (entry.metadata or {}).get("source_url"),
                "mime_type": (entry.metadata or {}).get("mime_type"),
                "native_format": (entry.metadata or {}).get("native_format"),
                "retrieval_rank": (entry.metadata or {}).get("retrieval_rank"),
                "export_strategy": (entry.metadata or {}).get("export_strategy"),
            }
            for entry in entries
        ],
    }


def write_zip_to_path(
    path: str,
    entries: list[ZipEntry],
    *,
    max_files: int,
    max_total_bytes: int,
    max_single_file_bytes: int,
    manifest_extra: dict[str, Any] | None = None,
) -> ZipResult:
    """Write ``entries`` (+ manifest.json) to ``path`` under the given limits."""
    if len(entries) > max_files:
        raise ExportLimitError(
            f"Export refused: {len(entries)} artifacts exceeds the maximum of "
            f"{max_files} files in a single export."
        )
    total = 0
    for entry in entries:
        total += len(entry.data)
        if len(entry.data) > max_single_file_bytes:
            raise ExportLimitError(
                f"Export refused: '{entry.name}' exceeds the maximum single-file "
                f"size of {max_single_file_bytes} bytes."
            )
    if total > max_total_bytes:
        raise ExportLimitError(
            f"Export refused: total artifact size {total} bytes exceeds the "
            f"maximum of {max_total_bytes} bytes."
        )

    names = _unique_names([entry.name for entry in entries])
    named: list[ZipEntry] = []
    for entry, name in zip(entries, names, strict=True):
        entry = ZipEntry(name=name, data=entry.data, metadata=entry.metadata)
        named.append(entry)

    manifest = build_manifest(named)
    if manifest_extra:
        manifest["details"] = manifest_extra

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2, ensure_ascii=False))
        for entry in named:
            with archive.open(entry.name, "w") as dest:
                dest.write(entry.data)

    return ZipResult(path=path, file_count=len(named), total_bytes=total + len(_MANIFEST_NAME))


def zip_bytes(
    entries: list[ZipEntry],
    *,
    max_files: int,
    max_total_bytes: int,
    max_single_file_bytes: int,
    manifest_extra: dict[str, Any] | None = None,
) -> io.BytesIO:
    """In-memory variant of :func:`write_zip_to_path` (tests/small exports)."""
    if len(entries) > max_files:
        raise ExportLimitError(
            f"Export refused: {len(entries)} artifacts exceeds the maximum of "
            f"{max_files} files in a single export."
        )
    total = sum(len(entry.data) for entry in entries)
    for entry in entries:
        if len(entry.data) > max_single_file_bytes:
            raise ExportLimitError(
                f"Export refused: '{entry.name}' exceeds the maximum single-file "
                f"size of {max_single_file_bytes} bytes."
            )
    if total > max_total_bytes:
        raise ExportLimitError(
            f"Export refused: total artifact size {total} bytes exceeds the "
            f"maximum of {max_total_bytes} bytes."
        )

    names = _unique_names([entry.name for entry in entries])
    named = [
        ZipEntry(name=name, data=entry.data, metadata=entry.metadata)
        for entry, name in zip(entries, names, strict=True)
    ]
    manifest = build_manifest(named)
    if manifest_extra:
        manifest["details"] = manifest_extra
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))
        for entry in named:
            archive.writestr(entry.name, entry.data)
    stream.seek(0)
    return stream
