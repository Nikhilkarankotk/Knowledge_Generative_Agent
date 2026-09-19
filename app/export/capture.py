"""Retrieval capture - records the *actual retrieval context* per chat turn.

The commit-to-context phase happens exactly once per assistant answer: at the end
of a chat turn the plugins' retrieval results are flattened into
:class:`ExportItemDraft` rows and persisted against the assistant
``chat_message.id``. Because Semantic Kernel only hands the plugins formatted
strings (the services stay process-shared singletons and keep their exact
contracts), the capture layer parses those deterministic, test-covered service
formats back into structured artifact metadata.

The parser functions below only interpret the well-known ``[Source: ...]``
formats produced by the four services (RAG, Confluence, GitHub, SharePoint);
unknown/no-result markers are ignored and can never fabricate a source.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

# Guard so a pathological retrieval result can never be persisted in full.
_MAX_CONTENT_REFERENCE_CHARS = 200_000
_MAX_ITEMS = 200


@dataclass
class ExportItemDraft:
    """A single retrieval artifact ready to be persisted as ExportContextItem."""

    source_type: str
    source_id: str
    source_name: str = ""
    filename: str | None = None
    mime_type: str | None = None
    source_url: str | None = None
    source_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    retrieval_rank: int | None = None
    retrieval_score: float | None = None
    content_reference: str | None = None
    export_strategy: str | None = None
    native_format: str | None = None
    size_bytes: int | None = None
    exportable: bool = True

    def merge_content(self, content: str) -> None:
        """Append captured content into the JSON ``content_reference`` blob."""
        text = (content or "").strip()
        if not text:
            return
        payload: dict[str, Any] = {}
        if self.content_reference:
            try:
                payload = json.loads(self.content_reference)
            except ValueError:
                payload = {"content": str(self.content_reference)}
        existing = str(payload.get("content") or "")
        if existing and text not in existing:
            payload["content"] = existing + "\n" + text
        elif not existing:
            payload["content"] = text
        payload["content"] = payload["content"][:_MAX_CONTENT_REFERENCE_CHARS]
        self.content_reference = json.dumps(payload)


class RetrievalCapture:
    """Thread-safe accumulator of retrieval artifacts for one chat turn.

    Deduplicated by ``(source_type, source_id)``: the agent may retrieve the same
    artifact several times in one turn; it is recorded once with the richest
    metadata.
    """

    def __init__(self) -> None:
        self._items: list[ExportItemDraft] = []
        self._ranks: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    @property
    def items(self) -> list[ExportItemDraft]:
        with self._lock:
            return list(self._items)

    def add(self, draft: ExportItemDraft) -> None:
        with self._lock:
            key = (draft.source_type, draft.source_id)
            if key in self._ranks:
                existing = self._existing_for(draft.source_type, draft.source_id)
                if existing is not None:
                    self._merge(existing, draft)
                    return
            if not self._items:
                rank = 0
            else:
                rank = max(item.retrieval_rank or 0 for item in self._items) + 1
            draft.retrieval_rank = rank
            self._ranks[key] = rank
            if len(self._items) < _MAX_ITEMS:
                self._items.append(draft)
            else:
                self._ranks.pop(key, None)

    def _existing_for(self, source_type: str, source_id: str) -> ExportItemDraft | None:
        for item in self._items:
            if item.source_type == source_type and item.source_id == source_id:
                return item
        return None

    def _merge(self, existing: ExportItemDraft, incoming: ExportItemDraft) -> None:
        if not existing.source_name and incoming.source_name:
            existing.source_name = incoming.source_name
        if not existing.filename and incoming.filename:
            existing.filename = incoming.filename
        if not existing.mime_type and incoming.mime_type:
            existing.mime_type = incoming.mime_type
        if not existing.source_url and incoming.source_url:
            existing.source_url = incoming.source_url
        if not existing.source_path and incoming.source_path:
            existing.source_path = incoming.source_path
        if not existing.export_strategy and incoming.export_strategy:
            existing.export_strategy = incoming.export_strategy
        if not existing.native_format and incoming.native_format:
            existing.native_format = incoming.native_format
        if incoming.size_bytes is not None:
            existing.size_bytes = max(existing.size_bytes or 0, incoming.size_bytes)
        existing.metadata.update(incoming.metadata or {})
        incoming_rank = incoming.retrieval_rank
        existing_rank = existing.retrieval_rank
        if incoming_rank is not None:
            existing.retrieval_rank = min(existing_rank or 0, incoming_rank) if existing_rank is not None else incoming_rank
        existing.merge_content(
            _extract_content_payload(incoming.content_reference) or ""
        )


def _extract_content_payload(content_reference: str | None) -> str | None:
    if not content_reference:
        return None
    try:
        payload = json.loads(content_reference)
    except ValueError:
        return str(content_reference)
    return str(payload.get("content") or "")


# --- Parser helpers for the deterministic service formats ---------------------


def parse_rag_context(text: str) -> list[tuple[str, str]]:
    """Split a RAG context string into (filename, chunk_text) pairs in order."""
    blocks: list[tuple[str, str | None]] = []
    current_file: str | None = None
    current_text: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip("\r")
        match = re.match(r"^\[Source: ([^\]]+)\]$", line)
        if match:
            if current_file:
                blocks.append((current_file, "\n".join(current_text).strip()))
            current_file = match.group(1).strip()
            current_text = []
        else:
            if current_file is not None:
                current_text.append(line)
    if current_file:
        blocks.append((current_file, "\n".join(current_text).strip()))
    return [(name, body) for name, body in blocks if name and body]


def parse_confluence_search(text: str) -> list[dict[str, Any]]:
    """Parse ``search_pages`` output blocks into page metadata dicts."""
    pages: list[dict[str, Any]] = []
    for block in re.split(r"\n{2,}", (text or "")):
        page: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("[Source: Confluence:"):
                raw = line[len("[Source: Confluence: "):].rstrip("]")
                page["title"] = raw.split(" (space: ")[0].strip()
                if " (space: " in raw:
                    page["space"] = raw.split(" (space: ", 1)[1].rstrip(")").strip()
            elif line.startswith("Page id: "):
                page["page_id"] = line[len("Page id: "):].strip()
            elif line.startswith("URL: "):
                page["url"] = line[len("URL: "):].strip()
            elif line.startswith("Parent: "):
                page["parent"] = line[len("Parent: "):].strip()
            elif line.startswith("Excerpt: "):
                page["excerpt"] = line[len("Excerpt: "):].strip()
        if page.get("page_id"):
            pages.append(page)
    return pages


def parse_confluence_page(text: str) -> dict[str, Any] | None:
    """Parse ``get_page`` output into {title, url, content}."""
    lines = (text or "").splitlines()
    title: str | None = None
    url: str | None = None
    for _index, line in enumerate(lines):
        if line.startswith("[Source: Confluence:"):
            raw = line[len("[Source: Confluence: "):].rstrip("]")
            title = raw.split(" (space: ")[0].strip()
        elif line.startswith("["):
            continue
        elif title is not None and url is None and ("http://" in line or "https://" in line):
            url = line.strip()
    if title is None:
        return None
    content_start = 0
    for index, line in enumerate(lines):
        if line.startswith("[Source:"):
            content_start = index + 1
    content = "\n".join(lines[content_start:]).strip()
    if not content or content.startswith("No content available"):
        return None
    return {"title": title, "url": url or "", "content": content}


def parse_github_target(text: str) -> dict[str, Any] | None:
    """Parse ``[Source: GitHub: owner/repo[:path]]`` into metadata."""
    for line in (text or "").splitlines():
        if line.startswith("[Source: GitHub: "):
            raw = line[len("[Source: GitHub: "):].split("]", 1)[0].strip()
            if "#" in raw:
                repo, issue = raw.split("#", 1)
                return {"repo": repo.strip(), "issue": issue.strip()}
            if ":" in raw:
                repo, path = raw.split(":", 1)
                return {"repo": repo.strip(), "path": path.strip()}
            return {"repo": raw.strip()}
    return None


def parse_sharepoint_items(text: str) -> list[dict[str, Any]]:
    """Parse SharePoint blocks (list/search/content) into item metadata dicts."""
    items: list[dict[str, Any]] = []
    for block in re.split(r"\n{2,}", (text or "")):
        item: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("[Source: SharePoint:"):
                item["name"] = line[len("[Source: SharePoint: "):].rstrip("]").strip()
            elif line.startswith("[Source: SharePoint]"):
                continue
            elif line.startswith("URL: "):
                item["url"] = line[len("URL: "):].strip()
            elif line.startswith("Drive id: "):
                item["drive_id"] = line[len("Drive id: "):].strip()
            elif line.startswith("Document id: "):
                item["document_id"] = line[len("Document id: "):].strip()
            elif line.startswith("Site id: "):
                item["site_id"] = line[len("Site id: "):].strip()
            elif line.startswith("Size: "):
                size = line[len("Size: "):].split(" ", 1)[0]
                try:
                    item["size"] = int(size)
                except ValueError:
                    pass
            elif line.startswith("Modified: "):
                item["modified"] = line[len("Modified: "):].strip()
            elif line.startswith("MimeType: "):
                item["mime_type"] = line[len("MimeType: "):].strip()
            elif line.startswith("Parent: "):
                item["parent"] = line[len("Parent: "):].strip()
            elif line.startswith("Created: "):
                item["created"] = line[len("Created: "):].strip()
            elif line.startswith("Type: "):
                item["type"] = line[len("Type: "):].strip()
            else:
                if item.get("name") and "content" not in item:
                    item["content"] = []
                if "content" in item and line.strip():
                    (item["content"] if isinstance(item["content"], list) else []).append(line)
        if item.get("document_id") or item.get("name"):
            if isinstance(item.get("content"), list):
                item["content"] = "\n".join(item["content"]).strip()
            items.append(item)
    return items


def parse_github_code_search(text: str) -> list[dict[str, Any]]:
    """Parse ``search_code`` output into repo/path metadata pairs."""
    results: list[dict[str, Any]] = []
    for block in re.split(r"\n{2,}", (text or "")):
        target = None
        url = ""
        for line in block.splitlines():
            if line.startswith("[Source: GitHub: "):
                raw = line[len("[Source: GitHub: "):].split("]", 1)[0].strip()
                if ":" in raw and "#" not in raw:
                    repo, path = raw.split(":", 1)
                    target = {"repo": repo.strip(), "path": path.strip()}
            elif line.startswith("URL: "):
                url = line[len("URL: "):].strip()
        if target:
            target["url"] = url
            results.append(target)
    return results
