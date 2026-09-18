"""Shared per-turn retrieval evidence ledger for every knowledge source.

The ledger gives every knowledge-source plugin the same guarantees, so adding a
new source (Teams, OneDrive, Outlook, ...) means writing a small adapter instead
of re-implementing plumbing:

- evidence found earlier in a turn is never erased by a later empty search;
- a machine-readable retrieval-state block is appended to tool results so the
  final LLM always sees the true, complete retrieval state for the turn;
- failures, empty results and content-unavailable cases stay distinct.

The ledger is source-agnostic: a plugin feeds it normalized items and a source
name, and reuses the same rendering/stripping/scope-matching helpers.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from typing import Any

# Per-turn bounds. The state block must stay small enough to fit in the model
# context yet carry every item found across the turn's searches.
MAX_EVIDENCE_ITEMS = 20
STATE_CONTENT_LIMIT = 600

# A state block looks like:
# === CONFLUENCE RETRIEVAL STATE (authoritative evidence summary; machine-readable) ===
# ...
# === END CONFLUENCE RETRIEVAL STATE ===
_STATE_START_RE = re.compile(r"^=== ([A-Z0-9_]+) RETRIEVAL STATE\b")
_STATE_END_RE = re.compile(r"^=== END [A-Z0-9_]+ RETRIEVAL STATE ===\s*$")

RelevanceFn = Callable[[str, dict[str, Any]], str]


def state_start(source: str) -> str:
    """Opening marker for ``source``'s retrieval-state block."""
    return f"=== {source.upper()} RETRIEVAL STATE"


def state_end(source: str) -> str:
    """Closing marker for ``source``'s retrieval-state block."""
    return f"=== END {source.upper()} RETRIEVAL STATE ==="


def append_state(result: str, state: str) -> str:
    """Append the retrieval-state block to a tool result, if any state exists."""
    if not state:
        return result
    return f"{result}\n\n{state}"


def strip_retrieval_state(text: str) -> str:
    """Remove every knowledge-source retrieval-state block from tool output."""
    if not text:
        return text
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        if not skipping and _STATE_START_RE.match(line):
            skipping = True
            continue
        if skipping:
            if _STATE_END_RE.match(line):
                skipping = False
            continue
        kept.append(line)
    return "\n".join(kept)


def match_known_scope(value: str | None, known: Mapping[str, str]) -> str | None:
    """Case-insensitively resolve a requested scope to its exact known value.

    Returns ``None`` when there is no requested value (meaning "search every
    scope") or when the value is unknown, so a guessed scope identifier can never
    narrow a search to zero results and masquerade as missing information.
    """
    key = (value or "").strip()
    if not key or not known:
        return None
    return known.get(key.casefold())


class RetrievalLedger:
    """Thread-safe, per-turn evidence ledger for one knowledge source.

    Items are normalized dicts with the generic keys ``id``, ``title``, ``url``,
    ``parent``, ``excerpt`` and an optional source-specific ``metadata`` mapping.
    The first non-empty value for an item wins, so a later sparse hit can never
    overwrite richer metadata captured earlier in the turn.
    """

    def __init__(self, source: str, *, item_label: str = "item") -> None:
        self._source = source
        self._item_label = item_label
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        self._search_count = 0
        self._last_result_count = 0
        self._last_query = ""
        self._errors: list[str] = []

    @property
    def source(self) -> str:
        return self._source

    @staticmethod
    def _generic_item(item: Mapping[str, Any]) -> dict[str, Any]:
        metadata = item.get("metadata")
        return {
            "id": str(item.get("id") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "url": str(item.get("url") or "").strip(),
            "parent": str(item.get("parent") or "").strip(),
            "excerpt": str(item.get("excerpt") or "").strip(),
            "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
        }

    def _new_item_locked(self, item_id: str) -> dict[str, Any]:
        return {
            "id": item_id,
            "title": "",
            "url": "",
            "parent": "",
            "excerpt": "",
            "relevance": "",
            "metadata": {},
            "content": "",
            "content_length": 0,
            "retrieved": False,
            "content_unavailable": False,
            "queries": [],
        }

    @staticmethod
    def _apply_fields_locked(record: dict[str, Any], generic: Mapping[str, Any]) -> None:
        for key in ("title", "url", "parent", "excerpt"):
            value = generic.get(key) or ""
            if value and not record.get(key):
                record[key] = value
        generic_metadata = generic.get("metadata") or {}
        if generic_metadata:
            merged = dict(record.get("metadata") or {})
            for key, value in generic_metadata.items():
                if value not in (None, "") and not merged.get(key):
                    merged[key] = value
            record["metadata"] = merged

    def _absorb_locked(
        self,
        item: Mapping[str, Any],
        query: str,
        relevance: RelevanceFn | None,
    ) -> None:
        generic = self._generic_item(item)
        item_id = generic["id"]
        if not item_id:
            return
        record = self._items.get(item_id)
        if record is None:
            if len(self._items) >= MAX_EVIDENCE_ITEMS:
                return
            record = self._new_item_locked(item_id)
            self._items[item_id] = record
        label = relevance(query, generic) if relevance is not None else "search"
        if label == "title" or not record.get("relevance"):
            record["relevance"] = label
        self._apply_fields_locked(record, generic)
        if query and query not in record["queries"]:
            record["queries"].append(query)

    def record_search(
        self,
        query: str,
        items: list[Mapping[str, Any]],
        *,
        relevance: RelevanceFn | None = None,
    ) -> None:
        """Record one search and absorb every item it returned."""
        with self._lock:
            self._search_count += 1
            self._last_result_count = len(items)
            self._last_query = query
            for item in items:
                self._absorb_locked(item, query, relevance)

    def record_search_error(self, query: str, message: str) -> None:
        """Record a failed search without discarding earlier evidence."""
        with self._lock:
            self._search_count += 1
            self._last_result_count = 0
            self._last_query = query
            self._errors.append(f"search failed: {message}")

    def record_retrieval(
        self,
        item_id: str,
        item: Mapping[str, Any],
        content: str,
    ) -> None:
        """Record that an item's content was successfully retrieved."""
        clean_id = str(item_id or "").strip()
        if not clean_id:
            return
        generic = self._generic_item(item)
        generic["id"] = clean_id
        with self._lock:
            record = self._items.get(clean_id)
            if record is None:
                if len(self._items) >= MAX_EVIDENCE_ITEMS:
                    return
                record = self._new_item_locked(clean_id)
                self._items[clean_id] = record
            record["retrieved"] = True
            record["content_unavailable"] = False
            if not record.get("relevance"):
                record["relevance"] = "retrieved"
            self._apply_fields_locked(record, generic)
            text = str(content or "")
            record["content"] = text[:STATE_CONTENT_LIMIT]
            record["content_length"] = len(text)

    def record_content_unavailable(
        self,
        item_id: str,
        *,
        message: str | None = None,
    ) -> None:
        """Record that an item was found but its content could not be retrieved."""
        clean_id = str(item_id or "").strip()
        if not clean_id:
            return
        with self._lock:
            record = self._items.get(clean_id)
            if record is None:
                if len(self._items) >= MAX_EVIDENCE_ITEMS:
                    return
                record = self._new_item_locked(clean_id)
                self._items[clean_id] = record
            record["content_unavailable"] = True
            if not record.get("relevance"):
                record["relevance"] = "retrieved"
            self._errors.append(
                message or f"{self._item_label} {clean_id} content unavailable"
            )

    def record_error(self, message: str) -> None:
        """Record a retrieval failure that is not tied to a specific item."""
        with self._lock:
            self._errors.append(message)

    def render(self) -> str:
        """Render the machine-readable state block, or '' when nothing happened."""
        with self._lock:
            if not (self._search_count or self._items or self._errors):
                return ""
            records = [dict(record) for record in self._items.values()]
            search_count = self._search_count
            last_result_count = self._last_result_count
            last_query = self._last_query
            errors = list(self._errors)
        items = [
            {
                "id": record["id"],
                "title": record["title"],
                "url": record["url"],
                "parent": record["parent"],
                "relevance": record["relevance"],
                "retrieved": record["retrieved"],
                "content_available": not record["content_unavailable"],
                "content_length": record["content_length"],
                "content": record["content"] or record["excerpt"],
                "metadata": record["metadata"],
            }
            for record in records
        ]
        payload = {
            "source": self._source,
            "searches": search_count,
            "items_found": len(records),
            "items_retrieved": sum(1 for record in records if record["retrieved"]),
            "evidence_found": bool(records),
            "last_search_results": last_result_count,
            "last_search_query": last_query,
            "errors": errors,
            "items": items,
        }
        label = self._source.title()
        item_label = self._item_label
        if records:
            guidance = (
                f"{label} retrieval this turn found {len(records)} relevant "
                f"{item_label}(s). A later search returning 0 results does not erase "
                "them. Use this evidence; do not claim this source has no relevant "
                "information."
            )
        elif search_count:
            guidance = (
                f"{label} searches this turn returned no {item_label}s yet. This "
                f"means no matching {item_label}s were found for the attempted "
                "queries; it is not yet proof that this source has no relevant "
                "information. Try broader terms before concluding."
            )
        else:
            guidance = (
                f"{label} retrieval failed; the content could not be retrieved. Do "
                "not claim that information does or does not exist."
            )
        lines = [
            f"{state_start(self._source)} (authoritative evidence summary; machine-readable) ===",
            guidance,
            json.dumps(payload, ensure_ascii=False, default=str),
            state_end(self._source),
        ]
        return "\n".join(lines)
