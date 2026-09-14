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
    """Extract readable text from Confluence's HTML ``body.view`` content."""

    _IGNORE_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: list[str] = []
        self._skip_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in self._IGNORE_TAGS:
            self._skip_depth += 1
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}:
            self._pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORE_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._pieces.append(data)

    def text(self) -> str:
        raw = " ".join(self._pieces)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n", raw)
        return raw.strip()


def html_to_text(markup: str) -> str:
    """Strip HTML tags and collapse whitespace into readable text."""
    parser = _TextFromHtml()
    parser.feed(markup or "")
    parser.close()
    return parser.text()


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
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._username = username
        self._limit = max(1, limit)
        self._timeout_seconds = timeout_seconds
        self._page_char_limit = max(500, page_char_limit)
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
        try:
            response = self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise ConfluenceApiError(f"Confluence request to {path} timed out.") from exc
        except httpx.HTTPError as exc:
            raise ConfluenceApiError(f"Confluence request to {path} failed: {exc}") from exc
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
                "expand": "version,space,excerpt",
            },
        )
        return [item for item in payload.get("results", []) if isinstance(item, dict)]

    # -- public API -------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int | None = None,
        space_key: str | None = None,
    ) -> str:
        """CQL text search over Confluence pages; returns an attributed text block.

        The primary CQL matches the query as a phrase. If that returns fewer pages
        than requested, the search automatically broadens to per-keyword OR terms
        (stop words dropped, up to 4 keywords) matching both body text and page
        titles, and merges/dedupes the results so phrase matches rank first --
        important when a wiki holds hundreds of pages. When ``space_key`` is given
        the search is scoped to that single space.
        """
        query = (query or "").strip()
        if not query:
            return "Please provide a non-empty search query."
        limit_n = min(max(1, limit or self._limit), 25)

        scope = ""
        if space_key:
            scope = f' AND space = "{_escape_cql(space_key)}"'

        base_cql = f"type = page{scope}"
        phrase_cql = f'{base_cql} AND text ~ "{_escape_cql(query)}"'
        results = self._run_search(phrase_cql, limit_n * 3)

        keywords = _extract_keywords(query)
        if len(results) < limit_n and len(keywords) >= 2:
            term_cql = base_cql + " AND (" + " OR ".join(
                f'text ~ "{_escape_cql(word)}" OR title ~ "{_escape_cql(word)}"'
                for word in keywords
            ) + ")"
            broad = self._run_search(term_cql, limit_n * 2)
            results = _dedupe_results(results + broad)

        results = results[:limit_n]
        if not results:
            return "No Confluence pages found matching the query."
        return "\n\n".join(self._format_search_result(item) for item in results)

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

    def get_page(self, page_id: str) -> str:
        """Full plain-text content of a Confluence page (with attribution)."""
        payload = self._get_json(
            f"rest/api/content/{page_id}",
            params={"expand": "body.view,version,space"},
        )
        title = str(payload.get("title") or "Unknown")
        url = self._page_url(payload)
        body = payload.get("body", {}) or {}
        content = html_to_text(str((body.get("view", {}) or {}).get("value") or ""))
        if not content:
            return f"No content available for Confluence page '{title}'."
        truncated = content[: self._page_char_limit]
        return f"[Source: Confluence: {title}]\n{url}\n{truncated}"

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
        if excerpt:
            lines.append(f"Excerpt: {_cap(excerpt, 500)}")
        return "\n".join(lines)


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
