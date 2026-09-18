"""Client for the Confluence REST API (ConfluencePlugin data provider, Phase 1).

Encapsulates everything the plugin needs:

* ``search(query, limit, space_key)`` - CQL text search over Confluence pages,
  optionally scoped to one space. On a low-recall phrase match it automatically
  broadens to per-keyword OR terms so large wikis (hundreds of pages) still surface
  relevant pages. Returns a plain-text snapshot including the page id/title/URL/space
  so the agent can answer with ``[Source: Confluence: <title>]`` attribution and
  optionally drill into a page.
* ``list_spaces()`` - the spaces the agent may search, so the agent can discover and
  scope to the authoritative space instead of searching the whole wiki blindly.
* ``get_page(page_id)`` - full plain-text content of one page (with attribution).

Credentials are handled here only (Bearer token, or HTTP Basic when a username
is configured) and are never surfaced to the LLM. Failures are raised as
:class:`~app.core.exceptions.ConfluenceApiError` so the ConfluencePlugin can turn
them into honest, non-fabricated markers for the agent.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any

import httpx

from app.core.exceptions import ConfluenceApiError

logger = logging.getLogger(__name__)


class _TextFromHtml(HTMLParser):
    """Extract *structured* text from Confluence's HTML ``body.view`` content.

    Instead of flattening the page to one blob of prose, this emits a lightweight
    markdown representation so downstream exporters (DOCX/PDF) can rebuild the
    document structure professionally:

    * ``<h1>``..``<h6>``          -> ``#``..``######`` headings
    * ``<table>``                -> pipe tables (``| a | b |`` + separator row)
    * ``<pre>`` / block ``<code>`` -> fenced ```` ``` ```` blocks with the original
      whitespace preserved (critical for ASCII architecture diagrams)
    * ``<ul>`` / ``<ol>``         -> ``-`` / ``1.`` list items (nested lists indented)
    * ``<b>``/``<strong>``        -> ``**bold**``
    * ``<p>``/``<div>``/``<br>``   -> paragraph breaks
    """

    _IGNORE_TAGS = {"script", "style"}
    _HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    _INLINE = {"a", "span", "em", "i", "u", "small", "sub", "sup", "mark", "font"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: list[str] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._table_rows: list[list[str]] = []
        self._list_stack: list[str] = []
        self._ordinals: list[int] = []
        self._bold_depth = 0

    def _emit(self, text: str) -> None:
        if self._cell is not None:
            self._cell.append(text)
        else:
            self._pieces.append(text)

    # -- tags -----------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._IGNORE_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "pre" or (tag == "code" and self._pre_depth == 0 and self._table_depth == 0
                            and self._cell is None and not self._list_stack):
            if self._pre_depth == 0:
                self._pieces.append("\n```\n")
            self._pre_depth += 1
            return
        if self._pre_depth:
            return  # everything inside <pre> is verbatim
        if tag == "table":
            self._table_depth += 1
            self._table_rows = []
            return
        if self._table_depth:
            if tag == "tr":
                self._row = []
            elif tag in {"td", "th"}:
                self._cell = []
            elif tag == "br":
                self._emit(" ")
            return
        if tag in self._HEADINGS:
            self._pieces.append("\n\n" + "#" * self._HEADINGS[tag] + " ")
            return
        if tag == "ul":
            self._list_stack.append("ul")
            return
        if tag == "ol":
            self._list_stack.append("ol")
            self._ordinals.append(0)
            return
        if tag == "li":
            indent = "  " * max(len(self._list_stack) - 1, 0)
            if self._list_stack and self._list_stack[-1] == "ol":
                self._ordinals[-1] += 1
                self._pieces.append(f"\n{indent}{self._ordinals[-1]}. ")
            else:
                self._pieces.append(f"\n{indent}- ")
            return
        if tag in {"b", "strong"}:
            if self._bold_depth == 0:
                self._emit("**")
            self._bold_depth += 1
            return
        if tag in {"p", "div", "br", "section", "article", "blockquote", "hr"}:
            self._pieces.append("\n\n" if tag != "br" else "\n")
            return
        if tag in self._INLINE:
            self._emit(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORE_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in {"pre", "code"} and self._pre_depth:
            self._pre_depth -= 1
            if self._pre_depth == 0:
                self._pieces.append("\n```\n")
            return
        if self._pre_depth:
            return
        if self._table_depth:
            if tag in {"td", "th"} and self._cell is not None:
                text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
                if self._row is not None:
                    self._row.append(text)
                self._cell = None
            elif tag == "tr" and self._row is not None:
                self._table_rows.append(self._row)
                self._row = None
            elif tag == "table":
                self._table_depth -= 1
                self._flush_table()
            return
        if tag in self._HEADINGS:
            self._pieces.append("\n")
            return
        if tag in {"ul", "ol"} and self._list_stack:
            popped = self._list_stack.pop()
            if popped == "ol" and self._ordinals:
                self._ordinals.pop()
            if not self._list_stack:
                self._pieces.append("\n")
            return
        if tag in {"b", "strong"} and self._bold_depth:
            self._bold_depth -= 1
            if self._bold_depth == 0:
                self._emit("**")
            return
        if tag in {"p", "div", "section", "article", "blockquote"}:
            self._pieces.append("\n")
        elif tag in self._INLINE:
            self._emit(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._pre_depth:
            self._pieces.append(data)  # verbatim (diagrams / code)
            return
        self._emit(data)

    # -- helpers --------------------------------------------------------------
    def _flush_table(self) -> None:
        rows = [row for row in self._table_rows if any(cell for cell in row)]
        self._table_rows = []
        if not rows:
            return
        width = max(len(row) for row in rows)
        lines: list[str] = []
        for index, row in enumerate(rows):
            padded = [cell.replace("|", "\\|") for cell in (row + [""] * width)[:width]]
            lines.append("| " + " | ".join(padded) + " |")
            if index == 0:
                lines.append("| " + " | ".join(["---"] * width) + " |")
        self._pieces.append("\n\n" + "\n".join(lines) + "\n\n")

    def text(self) -> str:
        raw = "".join(self._pieces).replace("\xa0", " ")
        out: list[str] = []
        in_code = False
        for line in raw.splitlines():
            if line.strip() == "```":
                in_code = not in_code
                out.append("```")
                continue
            if in_code:
                out.append(line.rstrip())
                continue
            line = re.sub(r"[ \t]+", " ", line).rstrip()
            # "** text **" -> "**text**": trim whitespace INSIDE the bold markers
            # only, then drop markers that ended up empty.
            line = re.sub(r"\*\*\s+([^*]*?)\s*\*\*", r"**\1**", line)
            line = re.sub(r"\*\*([^*]*?)\s+\*\*", r"**\1**", line)
            line = line.replace("****", "")
            line = _space_bold_markers(line)
            # Inline element boundaries insert a guard space; remove it before
            # punctuation so "control plane ." reads "control plane.".
            line = re.sub(r"\s+([.,;:!?)\]])", r"\1", line)
            line = re.sub(r"([(\[])\s+", r"\1", line)
            stripped = line.strip()
            # Decorative bullet glyphs -> markdown bullets.
            line = re.sub(r"^(\s*)[\u25a0\u25aa\u25cf\u2022\u2023\u2043\u2219]\s*", r"\1- ", line)
            # Drop empty structural shells (<h3></h3>, empty <li>).
            if re.fullmatch(r"#{1,6}", stripped) or re.fullmatch(r"[-*+]|\d+[.)]", stripped):
                continue
            out.append(line)
        text = "\n".join(out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _space_bold_markers(line: str) -> str:
    """Ensure a space separates words from ``**`` markers on the outside.

    Inline HTML like ``Connect to <b>VPN</b>.`` loses the whitespace around the
    tag during parsing and would become ``Connect to**VPN**.``. Walking the
    markers in order (open, close, open, ...) restores ``Connect to **VPN**.``
    while leaving punctuation right after a closing marker untouched.
    """
    parts = line.split("**")
    if len(parts) < 3:
        return line
    out = parts[0]
    for index in range(1, len(parts)):
        opening = index % 2 == 1  # odd -> this ** opens a bold span
        prev, curr = out, parts[index]
        if opening:
            if prev and not prev[-1].isspace() and prev[-1].isalnum():
                out += " "
            out += "**" + curr
        else:
            out += "**"
            if curr and curr[0].isalnum():
                out += " "
            out += curr
    return out


def html_to_text(markup: str) -> str:
    """Convert Confluence ``body.view`` HTML into structured markdown-ish text.

    Headings, lists, tables, bold text and preformatted/ASCII-diagram blocks are
    preserved so the exported DOCX/PDF can lay the page out professionally.
    """
    parser = _TextFromHtml()
    parser.feed(markup or "")
    parser.close()
    return parser.text()


def _person_name(user: Any) -> str:
    """Best-effort display name for a Confluence user object (never fabricated)."""
    if not isinstance(user, dict):
        return ""
    for key in (
        "displayName",
        "publicName",
        "name",
        "username",
        "email",
        "accountId",
        "userKey",
    ):
        value = str(user.get(key) or "").strip()
        if value:
            return value
    return ""


def _person_account(user: Any) -> str:
    """Stable account identifier for a Confluence user (used only for dedupe)."""
    if not isinstance(user, dict):
        return ""
    for key in ("accountId", "userKey", "username", "email"):
        value = str(user.get(key) or "").strip()
        if value:
            return value
    return ""


def _editor_entries(values: Any) -> list[dict[str, str]]:
    """Normalize version-history editor metadata into ordered, unique entries.

    Accepts raw ``/version`` results (dicts with ``by``/``when``/``number``) or
    already-normalized ``{name, when, version}`` dicts, or plain name strings.
    Preserves the input order (newest version first) and deduplicates by account
    id when present, falling back to the display name. Never fabricates a name.
    """
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values or []:
        if not isinstance(value, dict):
            name = str(value or "").strip()
            when = ""
            number = ""
            account = ""
        elif "name" in value:
            name = str(value.get("name") or "").strip()
            when = str(value.get("when") or "").strip()
            number = str(value.get("version") or value.get("number") or "").strip()
            account = str(value.get("account") or "").strip()
        else:
            user = value.get("by")
            name = _person_name(user)
            when = str(value.get("when") or "").strip()
            raw_number = value.get("number")
            number = str(raw_number).strip() if raw_number is not None else ""
            account = _person_account(user)
        if not name:
            continue
        key = (account or name).casefold()
        if key in seen:
            continue
        seen.add(key)
        entries.append({"name": name, "when": when, "version": number})
        if len(entries) >= _RECENT_EDITOR_LIMIT:
            break
    return entries


def _editor_line(entry: dict[str, str]) -> str:
    """One deterministic, parseable line per recent editor."""
    parts = [entry["name"], entry.get("when") or "", entry.get("version") or ""]
    return "Recent editor: " + " | ".join(parts).rstrip(" |")


def _confluence_people_lines(
    version: Any,
    history: Any,
    recent_editors: Any = None,
) -> list[str]:
    """Deterministic person metadata lines for capture (owner/editors, when known)."""
    version = version if isinstance(version, dict) else {}
    history = history if isinstance(history, dict) else {}
    last_updated = history.get("lastUpdated")
    last_updated = last_updated if isinstance(last_updated, dict) else {}
    owner = _person_name(history.get("createdBy"))
    editor = _person_name(last_updated.get("by")) or _person_name(version.get("by"))
    when = str(last_updated.get("when") or version.get("when") or "").strip()
    entries = _editor_entries(recent_editors)
    if not entries and editor:
        raw_number = version.get("number")
        entries = [
            {
                "name": editor,
                "when": when,
                "version": str(raw_number) if raw_number is not None else "",
            }
        ]
    lines: list[str] = []
    if owner:
        lines.append(f"Owner: {owner}")
    if editor:
        lines.append(f"Last modified by: {editor}")
    lines.extend(_editor_line(entry) for entry in entries)
    if when:
        lines.append(f"Last modified: {when}")
    return lines


# How many page versions to inspect (newest first) and how many distinct recent
# editors to keep. Version history is best-effort: a failure never fails the page.
_VERSION_HISTORY_LIMIT = 10
_RECENT_EDITOR_LIMIT = 5


class ConfluenceService:
    """Thin wrapper around the Confluence ``/rest/api`` endpoints."""

    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        username: str = "",
        limit: int = 5,
        timeout_seconds: float = 15.0,
        page_char_limit: int = 15000,
        include_drafts: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._username = username
        self._limit = max(1, limit)
        self._timeout_seconds = timeout_seconds
        self._page_char_limit = max(500, page_char_limit)
        self._include_drafts = include_drafts
        if client is not None:
            self._client = client
        else:
            kwargs: dict[str, Any] = {
                "base_url": self._base_url,
                "timeout": timeout_seconds,
                "headers": {},
            }
            self._client = httpx.Client(**kwargs)
        if api_token:
            if username:
                # Atlassian Cloud: email + API token via HTTP Basic.
                self._client.headers["Authorization"] = "Basic " + _basic_auth_header(username, api_token)
            else:
                # Personal Access Token
                self._client.headers["Authorization"] = f"Bearer {api_token}"

    @classmethod
    def from_settings(cls, settings: Any) -> ConfluenceService | None:
        if not settings.confluence_enabled or not settings.confluence_base_url:
            return None
        return cls(
            base_url=settings.confluence_base_url,
            api_token=settings.confluence_api_token,
            username=settings.confluence_username,
            limit=settings.confluence_limit,
            timeout_seconds=settings.confluence_timeout_seconds,
            page_char_limit=settings.confluence_page_char_limit,
            include_drafts=getattr(settings, "confluence_include_drafts", False),
        )

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass

    # -- request helpers --------------------------------------------------------

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        debug_params = params or {}
        logger.debug("Confluence HTTP request: GET %s params=%r", path, debug_params)
        try:
            response = self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            logger.debug("Confluence HTTP response: GET %s status=TIMEOUT", path)
            raise ConfluenceApiError(f"Confluence request to {path} timed out.") from exc
        except httpx.HTTPError as exc:
            logger.debug("Confluence HTTP response: GET %s status=ERROR %s", path, exc)
            raise ConfluenceApiError(f"Confluence request to {path} failed: {exc}") from exc
        logger.debug("Confluence HTTP response: GET %s status=%d", path, response.status_code)
        if response.status_code in (401, 403):
            raise ConfluenceApiError("Confluence authentication failed (HTTP 401/403).")
        if response.status_code == 404:
            raise ConfluenceApiError(f"Confluence resource not found (HTTP 404) at {path}.")
        if response.status_code >= 500:
            raise ConfluenceApiError(
                f"Confluence server error (HTTP {response.status_code}) at {path}."
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ConfluenceApiError(f"Confluence request failed (HTTP {response.status_code}).") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ConfluenceApiError("Confluence returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise ConfluenceApiError("Confluence returned an unexpected payload shape.")
        return data

    def _run_search(self, cql: str, limit: int) -> list[dict[str, Any]]:
        payload = self._get_json(
            "rest/api/content/search",
            params={
                "cql": cql,
                "limit": max(1, limit),
                "expand": "version,space,history,excerpt,ancestors",
            },
        )
        results = [item for item in payload.get("results", []) if isinstance(item, dict)]
        titles = [item.get("title") for item in results]
        ids = [item.get("id") for item in results]
        parents = [
            (item.get("ancestors") or [])[-1].get("title")
            if item.get("ancestors")
            else None
            for item in results
        ]
        logger.debug(
            "Confluence search results: cql=%r count=%d titles=%r ids=%r parent_titles=%r",
            cql, len(results), titles, ids, parents,
        )
        return results

    # -- public API -------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int | None = None,
        space_key: str | None = None,
    ) -> str:
        """CQL text search over Confluence pages; returns an attributed text block.

        The search is progressive and hierarchy-aware:

        1. Title phrase match (highest precision).
        2. Body text phrase match.
        3. Per-keyword OR broaden (titles + body).
        4. Anchor/parent resolution: detects an application anchor (e.g.
           "Payments application") from the query, resolves it by title, and
           searches its descendant pages via ``ancestor`` -- the key mechanism
           that finds nested pages like "API Documentation" under "Payments
           Application".

        Results are deduplicated by page id, capped at ``limit``, and ranked so
        title-matches appear before keyword-or matches. When ``space_key`` is
        given the search is scoped to that single space.
        """
        query = (query or "").strip()
        if not query:
            return "Please provide a non-empty search query."
        limit_n = min(max(1, limit or self._limit), 25)

        scope = ""
        if space_key:
            scope = f' AND space = "{_escape_cql(space_key)}"'

        if self._include_drafts:
            type_cql = "(type = page OR type = draft)"
        else:
            type_cql = "type = page"
        base_cql = f"{type_cql}{scope}"
        keywords = _extract_keywords(query)

        logger.debug(
            "Confluence search: user_query=%r normalized_query=%r keywords=%r "
            "limit=%d space_key=%r include_drafts=%s",
            query, re.sub(r"\s+", " ", query).strip(), keywords, limit_n,
            space_key, self._include_drafts,
        )

        ranked: list[dict[str, Any]] = []
        seen: set[str] = set()

        def absorb(pages: list[dict[str, Any]]) -> None:
            for item in pages:
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                ranked.append(item)

        # 1) Title phrase (highest precision).
        absorb(self._run_search(f'{base_cql} AND title ~ "{_escape_cql(query)}"', limit_n * 2))
        if len(ranked) >= limit_n:
            return self._render_search_results(ranked[:limit_n], query)

        # 2) Body text phrase -- only when multi-keyword, or when title got nothing.
        if len(keywords) >= 2 or not ranked:
            absorb(self._run_search(f'{base_cql} AND text ~ "{_escape_cql(query)}"', limit_n * 2))
            if len(ranked) >= limit_n:
                return self._render_search_results(ranked[:limit_n], query)

        # 3) Hierarchy-aware: resolve an anchor page (e.g. "Payments application")
        #    by title, and search its descendants -- the fix for nested pages that
        #    flat keyword-or truncates out of the top N.
        if len(ranked) < limit_n:
            anchor, topic = _split_anchor_and_topic(query, keywords)
            if not anchor and len(keywords) >= 2:
                anchor, topic = keywords[0], " ".join(keywords[1:])
            if anchor:
                parent_pages = self._run_search(
                    f'{base_cql} AND title ~ "{_escape_cql(anchor)}"',
                    min(5, limit_n + 2),
                )
                for parent in parent_pages[:2]:
                    pid = str(parent.get("id") or "")
                    if not pid:
                        continue
                    absorb([parent])
                    if len(ranked) >= limit_n:
                        break
                    if topic:
                        child_cql = (
                            f'{base_cql} AND ancestor = "{pid}" AND '
                            f'(title ~ "{_escape_cql(topic)}" OR text ~ "{_escape_cql(topic)}")'
                        )
                    else:
                        child_cql = f'{base_cql} AND ancestor = "{pid}"'
                    absorb(self._run_search(child_cql, limit_n * 2))
                    if len(ranked) >= limit_n:
                        break

        # 4) Per-keyword OR broaden (last-resort broad).
        if len(ranked) < limit_n and len(keywords) >= 2:
            term_cql = base_cql + " AND (" + " OR ".join(
                f'text ~ "{_escape_cql(word)}" OR title ~ "{_escape_cql(word)}"'
                for word in keywords
            ) + ")"
            absorb(self._run_search(term_cql, limit_n * 3))

        ranked = ranked[:limit_n]
        if not ranked:
            return (
                "No Confluence pages matched this query. This is not proof that "
                "Confluence has no relevant documentation; retry with broader terms or "
                "without a space scope."
            )
        return self._render_search_results(ranked, query)

    def list_spaces(self, limit: int = 50) -> str:
        """List the Confluence spaces accessible to the agent (key + name)."""
        payload = self._get_json(
            "rest/api/space",
            params={"limit": max(1, min(limit, 100))},
        )
        results = [item for item in payload.get("results", []) if isinstance(item, dict)]
        if not results:
            return "No Confluence spaces are accessible."
        lines = [
            f"[Source: Confluence] key={item.get('key', '?')}, name={item.get('name') or 'Unnamed'}"
            for item in results
        ]
        return "Available Confluence spaces:\n" + "\n".join(lines)

    def recent_editor_versions(self, page_id: str) -> list[dict[str, str]]:
        """Editors of the page's recent versions, newest first (best-effort).

        Reads the real Confluence version history
        (``GET /rest/api/content/{id}/version``) and returns one
        ``{"name", "when", "version"}`` entry per distinct editor, preserving the
        newest-first order. A failure (permissions, missing endpoint, network)
        degrades to an empty list so it can never break page retrieval or
        fabricate an identity.
        """
        page_id = (page_id or "").strip()
        if not page_id:
            return []
        try:
            payload = self._get_json(
                f"rest/api/content/{page_id}/version",
                params={"limit": _VERSION_HISTORY_LIMIT},
            )
        except ConfluenceApiError as exc:
            logger.debug(
                "Confluence version history unavailable for page %s: %s", page_id, exc
            )
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        entries = _editor_entries(results if isinstance(results, list) else [])
        logger.debug(
            "Confluence recent editors: page_id=%s editors=%r", page_id, entries
        )
        return entries

    def recent_editor_names(self, page_id: str) -> list[str]:
        """Display names of the page's recent version editors (newest first)."""
        return [entry["name"] for entry in self.recent_editor_versions(page_id)]

    def get_page(self, page_id: str) -> str:
        """Full plain-text content of a Confluence page (with attribution)."""
        payload = self._get_json(
            f"rest/api/content/{page_id}",
            params={"expand": "body.view,version,space,history"},
        )
        title = str(payload.get("title") or "Unknown")
        url = self._page_url(payload)
        body = payload.get("body", {}) or {}
        content = html_to_text(str((body.get("view", {}) or {}).get("value") or ""))
        if not content:
            return f"No content available for Confluence page '{title}'."
        truncated = content[: self._page_char_limit]
        recent_editors = self.recent_editor_versions(page_id)
        people = _confluence_people_lines(
            payload.get("version"), payload.get("history"), recent_editors
        )
        prefix = "\n".join([f"[Source: Confluence: {title}]", *people, url])
        return f"{prefix}\n{truncated}"

    # -- formatting -------------------------------------------------------------

    def _page_url(self, item: dict[str, Any]) -> str:
        links = item.get("_links", {}) or {}
        webui = str(links.get("webui") or "")
        return self._base_url + (webui if webui.startswith("/") else f"/{webui}")

    def _format_search_result(self, item: dict[str, Any]) -> str:
        title = str(item.get("title") or "Untitled")
        page_id = str(item.get("id") or "")
        url = self._page_url(item)
        space = (item.get("space") or {}).get("name") or (item.get("space") or {}).get("key") or ""
        excerpt = str(item.get("excerpt") or "").strip() or str(item.get("summary") or "").strip()
        header = f"[Source: Confluence: {title}]" + (f" (space: {space})" if space else "")
        lines = [header, f"Page id: {page_id}", f"URL: {url}"]
        lines.extend(_confluence_people_lines(item.get("version"), item.get("history")))
        ancestors = item.get("ancestors") or []
        if isinstance(ancestors, list) and ancestors:
            ancestor_titles = [
                str(entry.get("title") or "").strip()
                for entry in ancestors
                if isinstance(entry, dict) and str(entry.get("title") or "").strip()
            ]
            if ancestor_titles:
                lines.append(f"Parent: {ancestor_titles[-1]}")
                lines.append("Ancestors: " + " > ".join(ancestor_titles))
        if excerpt:
            lines.append(f"Excerpt: {_cap(excerpt, 500)}")
        return "\n".join(lines)

    def _render_search_results(self, ranked: list[dict[str, Any]], query: str) -> str:
        logger.debug(
            "Confluence search done: query=%r returning=%d titles=%r ids=%r",
            query,
            len(ranked),
            [item.get("title") for item in ranked],
            [item.get("id") for item in ranked],
        )
        return "\n\n".join(self._format_search_result(item) for item in ranked)


def _basic_auth_header(username: str, password: str) -> str:
    import base64

    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return token


def _escape_cql(query: str) -> str:
    return query.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")


_STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "be", "by", "can", "do", "does",
        "for", "from", "has", "have", "how", "in", "is", "it", "its", "of", "on",
        "or", "that", "the", "their", "there", "this", "to", "was", "were", "what",
        "when", "where", "which", "who", "with", "you", "your",
    }
)


def _extract_keywords(query: str, max_keywords: int = 4) -> list[str]:
    """Lower-cased, deduplicated meaningful tokens from a query (max 4)."""
    tokens = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", query.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for token in tokens:
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= max_keywords:
            break
    return keywords


def _dedupe_results(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate pages by id, preserving first-seen (phrase-first) order."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        deduped.append(item)
    return deduped


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


_APP_NOUN_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9]*(?:[\s'-][A-Za-z0-9]+)*)\s+"
    r"(?P<noun>application|app|service|services|platform|system|portal)s?\b",
    re.IGNORECASE,
)


def _split_anchor_and_topic(query: str, keywords: list[str]) -> tuple[str | None, str | None]:
    """Detect an anchor page concept (e.g. "Payments application") in the query.

    Returns ``(anchor_title, topic)`` or ``(None, None)`` when no application-like
    noun phrase is detected.
    """
    match = _APP_NOUN_RE.search(query)
    if match:
        anchor = f"{match.group('name').strip()} {match.group('noun').strip()}"
        noun_kw = match.group("noun").lower()
        anchor_tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9]+", match.group("name"))}
        anchor_tokens.add(noun_kw)
        topic_words = [kw for kw in keywords if kw not in anchor_tokens]
        return anchor, (" ".join(topic_words) if topic_words else None)
    return None, None
