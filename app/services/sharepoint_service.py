"""Client for Microsoft Graph / SharePoint Online (SharePointPlugin data provider, Phase 3).

Read-only knowledge integration via Microsoft Graph OAuth 2.0 client-credentials
flow. Encapsulates everything the plugin needs:

``tenant_wide`` mode (``SHAREPOINT_TENANT_WIDE=true``) uses the Microsoft Graph
Search API (``POST /search/query`` with ``entityTypes: driveItem``) so the agent
can search and read every SharePoint site and document the application's
Microsoft Graph permission can access, without a site/folder allowlist. This
requires the Entra application permission reading all sites/files (e.g.
``Sites.Read.All`` or ``Files.Read.All``), granted with admin consent; with only
``Sites.Selected`` the search JSON API is not authorized. Reads by drive/item id
still work for any drive the token can read.

Scoped (default) mode is the classic Phase 3 boundary:

* ``list_allowed_sites()`` - lists the SharePoint site + knowledge-base folders
  explicitly configured for this deployment (``SHAREPOINT_ALLOWED_SITES`` /
  ``SHAREPOINT_ALLOWED_FOLDERS``); never a global search.
* ``get_site()`` - resolves the configured site (``SHAREPOINT_SITE_HOSTNAME`` +
  ``SHAREPOINT_SITE_RELATIVE_PATH``) and returns site metadata.
* ``list_drives()`` - document libraries available in the resolved site.
* ``list_files(folder, limit)`` - files/folders inside a configured knowledge-base
  folder with metadata (name, size, created, modified, mime type, webUrl, drive
  and item ids, parent path).
* ``search(query, folder, limit)`` - searches only within a configured
  knowledge-base folder via scoped listing + filename matching; metadata only (no
  content download).
* ``get_document_content(document_id, drive_id, folder)`` - retrieve a file's
  metadata, download its content, extract readable text where supported, and
  return text with ``[Source: SharePoint: <filename>]`` attribution. The document
  must live inside a configured knowledge-base folder.
* ``search_file_content(query, folder, limit)`` - scoped search then download the
  matching documents' content in one call.

Security boundary (this is the application-level restriction, applied in the
service, *before* any Graph request):

* The allowed-site allowlist ``SHAREPOINT_ALLOWED_SITES`` is validated against the
  *resolved* site. No tenant-wide site discovery or search is performed; no site id
  is ever accepted from the plugin/LLM.
* The allowed-folder allowlist ``SHAREPOINT_ALLOWED_FOLDERS`` restricts every list,
  search and read to the configured knowledge-base folder(s) inside the site's
  primary document library. Retrieval never leaves those folders, and each
  document read is verified to live under an allowed folder.

Token acquisition uses the OAuth 2.0 client-credentials endpoint at
``https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`` with an
in-process token cache (until expiry minus a safety skew). Credentials are never
logged or exposed to the LLM. Failures are raised as
:class:`~app.core.exceptions.SharePointApiError` (including
:class:`~app.core.exceptions.SharePointSiteNotAllowedError` for site allowlist
rejections and :class:`~app.core.exceptions.SharePointFolderNotAllowedError` for
folder allowlist rejections) so the SharePointPlugin can turn them into honest,
non-fabricated markers for the agent.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Literal, overload
from urllib.parse import quote, unquote

import httpx

from app.core.config import Settings
from app.core.exceptions import (
    SharePointApiError,
    SharePointFolderNotAllowedError,
    SharePointSiteNotAllowedError,
)
from app.rag.document_parser import DocumentParser

logger = logging.getLogger(__name__)

_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_TOKEN_HOST = "https://login.microsoftonline.com"
_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MB guard
_MAX_CONTENT_READ_BYTES = 5 * 1024 * 1024  # content-token matching read cap (5 MB)
_MAX_SEARCH_ITEMS = 200  # folder walk bound for a single search
_LIBRARY_CHILDREN_TOP = 999

NO_SITES_CONFIGURED = (
    "No SharePoint sites are configured for this Knowledge Generative Agent."
)
NO_FOLDERS_CONFIGURED = (
    "No SharePoint knowledge-base folders are configured for this Knowledge "
    "Generative Agent."
)


# Words that carry no search signal in a natural-language question. Graph search
# ANDs every token, so leaving these in makes an otherwise good query miss.
_SEARCH_STOPWORDS = frozenset(
    """
    a an the and or of for to in on at by with from into about as is are was were be
    been being this that these those it its their there here what which who whom whose
    when where why how do does did can could should would will shall may might must
    explain describe show tell give provide list summarize summarise detail details
    overview please me us our your you i we they them any all some
    """.split()
)


def _search_keywords(query: str) -> list[str]:
    """Distinctive, order-preserving keywords of a question (``CI/CD`` -> ``cicd``)."""
    text = (query or "").casefold()
    # Keep compound tokens like "ci/cd" and "job_portal" searchable as one word.
    text = text.replace("/", "").replace("_", " ").replace("-", " ")
    keywords: list[str] = []
    for token in re.findall(r"[a-z0-9]+", text):
        if len(token) <= 1 or token in _SEARCH_STOPWORDS:
            continue
        if token not in keywords:
            keywords.append(token)
    return keywords


def _query_variants(query: str) -> list[str]:
    """Search strings to try in order: verbatim, keywords, then shorter subsets."""
    variants = [(query or "").strip()]
    keywords = _search_keywords(query)
    if keywords:
        variants.append(" ".join(keywords))
        # Drop generic trailing/leading words progressively (keep >= 2 keywords).
        generic = {"application", "app", "web", "system", "design", "architecture", "flow", "document"}
        distinctive = [k for k in keywords if k not in generic]
        if distinctive and distinctive != keywords:
            variants.append(" ".join(distinctive))
        # Pairs of the most distinctive words (proper nouns / product names tend to
        # come first in a question: "Job Portal ...").
        if len(distinctive) > 2:
            variants.append(" ".join(distinctive[:2]))
    return [v for v in dict.fromkeys(variants) if v]


def _graph_person_name(identity: Any) -> str:
    """Best-effort display name for a Graph identity set (never fabricated)."""
    if not isinstance(identity, dict):
        return ""
    user = identity.get("user")
    candidates = [user if isinstance(user, dict) else {}, identity]
    for candidate in candidates:
        for key in ("displayName", "email"):
            value = str(candidate.get(key) or "").strip()
            if value:
                return value
    return ""


def _clean_allowed_sites(sites: list[str] | None) -> list[str]:
    """Normalize, de-duplicate (case-insensitively) and order the site allowlist."""
    result: list[str] = []
    seen: set[str] = set()
    for entry in sites or []:
        site = entry.strip().strip("/")
        if not site:
            continue
        if site.lower() in seen:
            continue
        seen.add(site.lower())
        result.append(site)
    return result


def _clean_allowed_folders(folders: list[str] | None) -> list[str]:
    """Normalize, de-duplicate (case-insensitively) the folder allowlist."""
    result: list[str] = []
    seen: set[str] = set()
    for entry in folders or []:
        folder = (entry or "").strip().strip("/")
        if not folder:
            continue
        if folder.lower() in seen:
            continue
        seen.add(folder.lower())
        result.append(folder)
    return result


class SharePointService:
    """Thin read-only wrapper around the Microsoft Graph SharePoint endpoints.

    ``allowed_sites`` is the site allowlist (Graph site-id strings) and
    ``allowed_folders`` is the knowledge-base folder allowlist (drive-relative
    paths). The configured site is always resolved from
    ``site_hostname``/``site_relative_path``; nothing is ever taken from the LLM.
    """

    def __init__(
        self,
        *,
        tenant_id: str = "",
        client_id: str = "",
        client_secret: str = "",
        graph_base_url: str = "https://graph.microsoft.com/v1.0",
        site_hostname: str = "",
        site_relative_path: str = "",
        allowed_sites: list[str] | None = None,
        allowed_folders: list[str] | None = None,
        limit: int = 10,
        timeout_seconds: float = 30.0,
        content_char_limit: int = 20000,
        retry_backoff_seconds: float = 0.25,
        tenant_wide: bool = False,
        search_region: str = "",
        token_client: httpx.Client | None = None,
        graph_client: httpx.Client | None = None,
        document_parser: DocumentParser | None = None,
    ) -> None:
        self._tenant_id = (tenant_id or "").strip()
        self._client_id = (client_id or "").strip()
        self._client_secret = client_secret or ""
        self._graph_base_url = graph_base_url.rstrip("/")
        self._site_hostname = (site_hostname or "").strip()
        self._site_relative_path = (site_relative_path or "").strip()
        self._tenant_wide = bool(tenant_wide)
        self._search_region = (search_region or "").strip()
        self._limit = max(1, limit)
        self._timeout_seconds = timeout_seconds
        self._content_char_limit = max(500, content_char_limit)
        self._retry_backoff_seconds = retry_backoff_seconds
        self._allowed_sites = _clean_allowed_sites(allowed_sites)
        self._allowed_ids = {s.lower() for s in self._allowed_sites}
        self._allowed_folders = _clean_allowed_folders(allowed_folders)
        # Cache
        self._access_token: str = ""
        self._access_token_expires_at: float = 0.0
        self._token_lock = threading.Lock()
        self._resolved_site: dict[str, Any] | None = None
        # Clients
        if token_client is not None:
            self._token_client = token_client
        else:
            self._token_client = httpx.Client(
                base_url=_TOKEN_HOST,
                timeout=timeout_seconds,
            )
        if graph_client is not None:
            self._graph_client = graph_client
        else:
            # Microsoft Graph's drive "/content" endpoints return a 302 redirect to
            # SharePoint's download URL (signed with a short-lived tempauth token in
            # the Location header value); follow it to retrieve the bytes. httpx
            # strips the Authorization header on cross-host redirects.
            self._graph_client = httpx.Client(
                base_url=self._graph_base_url,
                timeout=timeout_seconds,
                follow_redirects=True,
            )
        self._document_parser = document_parser or DocumentParser()

    @classmethod
    def from_settings(cls, settings: Settings) -> SharePointService | None:
        if not settings.sharepoint_enabled:
            return None
        return cls(
            tenant_id=settings.sharepoint_tenant_id,
            client_id=settings.sharepoint_client_id,
            client_secret=settings.sharepoint_client_secret,
            graph_base_url=settings.sharepoint_graph_base_url,
            site_hostname=settings.sharepoint_site_hostname,
            site_relative_path=settings.sharepoint_site_relative_path,
            allowed_sites=settings.sharepoint_allowed_sites_list,
            allowed_folders=settings.sharepoint_allowed_folders_list,
            limit=settings.sharepoint_limit,
            timeout_seconds=settings.sharepoint_timeout_seconds,
            content_char_limit=settings.sharepoint_content_char_limit,
            tenant_wide=settings.sharepoint_tenant_wide,
            search_region=settings.sharepoint_search_region,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._tenant_id and self._client_id and self._client_secret)

    @property
    def tenant_wide(self) -> bool:
        return self._tenant_wide

    @property
    def allowed_sites(self) -> list[str]:
        """The configured site allowlist (canonical site-id strings)."""
        return list(self._allowed_sites)

    @property
    def allowed_folders(self) -> list[str]:
        """The configured knowledge-base folder allowlist."""
        return list(self._allowed_folders)

    def close(self) -> None:
        try:
            self._token_client.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass
        try:
            self._graph_client.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass

    # -- token management -------------------------------------------------------

    def _get_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token
        with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < self._access_token_expires_at:
                return self._access_token
            return self._fetch_access_token()

    def _fetch_access_token(self) -> str:
        if not self.enabled:
            raise SharePointApiError(
                "SharePoint authentication is not configured (credentials missing)."
            )
        token_path = f"/{self._tenant_id}/oauth2/v2.0/token"
        for attempt in range(3):
            try:
                response = self._token_client.post(
                    token_path,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": _GRAPH_SCOPE,
                    },
                )
            except httpx.TimeoutException as exc:
                raise SharePointApiError(
                    "SharePoint token request timed out."
                ) from exc
            except httpx.HTTPError as exc:
                raise SharePointApiError(
                    f"SharePoint token request failed: {exc}"
                ) from exc
            if response.status_code in (401, 403):
                raise SharePointApiError(
                    "SharePoint authentication failed (invalid client credentials or tenant)."
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2 and self._retry_backoff_seconds > 0:
                    time.sleep(self._retry_backoff_seconds * (attempt + 1))
                    continue
                if response.status_code == 429:
                    raise SharePointApiError(
                        "SharePoint token endpoint rate limited (HTTP 429)."
                    )
                raise SharePointApiError(
                    f"SharePoint token endpoint server error (HTTP {response.status_code})."
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SharePointApiError(
                    f"SharePoint token request failed (HTTP {response.status_code})."
                ) from exc
            try:
                data = response.json()
            except ValueError as exc:
                raise SharePointApiError(
                    "SharePoint token endpoint returned a non-JSON response."
                ) from exc
            access_token = str(data.get("access_token") or "")
            if not access_token:
                raise SharePointApiError(
                    "SharePoint token endpoint returned no access token."
                )
            expires_in = float(data.get("expires_in") or 3600)
            self._access_token = access_token
            self._access_token_expires_at = time.monotonic() + expires_in - 60
            logger.info(
                "Obtained Microsoft Graph access token (expires_in=%ss).",
                int(expires_in),
            )
            return self._access_token
        # The loop only breaks via raise/return; this satisfies type checkers.
        raise SharePointApiError("Unexpected SharePoint token request failure.")

    # -- graph request helpers --------------------------------------------------

    @overload
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        raw: Literal[True],
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response: ...

    @overload
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        raw: Literal[False] = False,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        raw: bool = False,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | httpx.Response:
        token = self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        body: Any = json_body if json_body is not None else None
        for attempt in range(3):
            try:
                response = self._graph_client.request(
                    method, path, params=params, headers=headers, json=body
                )
            except httpx.TimeoutException as exc:
                raise SharePointApiError(
                    f"SharePoint/Graph request to {path} timed out."
                ) from exc
            except httpx.HTTPError as exc:
                raise SharePointApiError(
                    f"SharePoint/Graph request to {path} failed: {exc}"
                ) from exc
            logger.debug(
                "Graph request: %s %s -> HTTP %s", method, path, response.status_code
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2 and self._retry_backoff_seconds > 0:
                    time.sleep(self._retry_backoff_seconds * (attempt + 1))
                    continue
                if response.status_code == 429:
                    raise SharePointApiError(
                        "SharePoint rate limit reached (HTTP 429). Retry later."
                    )
                raise SharePointApiError(
                    f"Microsoft Graph server error (HTTP {response.status_code}) at {path}."
                )
            if response.status_code == 401:
                raise SharePointApiError(
                    "SharePoint authentication failed (HTTP 401). "
                    "The access token may be expired or invalid."
                )
            if response.status_code == 403:
                raise SharePointApiError(
                    "The application does not have permission to access this SharePoint "
                    "site (HTTP 403)."
                )
            if response.status_code == 404:
                raise SharePointApiError(
                    f"The requested SharePoint resource could not be found (HTTP 404) at {path}."
                )
            if response.status_code >= 400:
                raise SharePointApiError(
                    f"SharePoint/Graph request failed (HTTP {response.status_code}) at {path}."
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SharePointApiError(
                    f"SharePoint/Graph request failed (HTTP {response.status_code})."
                ) from exc
            if raw:
                return response
            try:
                data = response.json()
            except ValueError as exc:
                raise SharePointApiError(
                    "Microsoft Graph returned a non-JSON response."
                ) from exc
            if not isinstance(data, dict):
                raise SharePointApiError(
                    "Microsoft Graph returned an unexpected payload shape."
                )
            return data
        raise SharePointApiError("Unexpected SharePoint request failure.")

    # -- site resolution ---------------------------------------------------------

    def _site_path(self) -> str:
        relative = self._site_relative_path.strip().strip("/")
        return f"sites/{_quote(f'{self._site_hostname}:/{relative}', safe=':/')}"

    def _resolve_site(self) -> dict[str, Any]:
        """Resolve + validate the configured site once.

        The site is resolved via ``GET /sites/{hostname}:/{relative-path}`` (the
        documented Graph form). If the resolved site id is not in the allowlist the
        request is rejected before any further Graph call. The resolved site's name
        / webUrl is also validated against the intended relative path so the
        deployment can never silently point at the wrong site.
        """
        if self._resolved_site is not None:
            return self._resolved_site
        if not self._allowed_ids:
            raise SharePointSiteNotAllowedError(NO_SITES_CONFIGURED)
        if not self._site_hostname or not self._site_relative_path:
            raise SharePointApiError(
                "SharePoint site hostname/relative path are not configured "
                "(SHAREPOINT_SITE_HOSTNAME / SHAREPOINT_SITE_RELATIVE_PATH)."
            )
        payload = self._request("GET", self._site_path())
        resolved_id = str(payload.get("id") or "").strip()
        if not resolved_id:
            raise SharePointApiError(
                "Microsoft Graph did not return a site id for the configured SharePoint site."
            )
        if resolved_id.lower() not in self._allowed_ids:
            name = str(
                payload.get("displayName")
                or payload.get("name")
                or resolved_id
            )
            logger.info(
                "SharePoint request rejected: resolved_site=%r name=%r "
                "reason=not_in_allowlist",
                resolved_id,
                name,
            )
            raise SharePointSiteNotAllowedError(
                f"SharePoint site {resolved_id} is not in the configured allowlist "
                f"(SHAREPOINT_ALLOWED_SITES) for this Knowledge Generative Agent. "
                f"Resolved site: {name}."
            )
        name = str(payload.get("name") or "")
        web_url = str(payload.get("webUrl") or "").lower().rstrip("/")
        expected = self._site_relative_path.strip().rstrip("/").lower()
        last_segment = expected.rsplit("/", 1)[-1]
        if expected and not web_url.endswith(expected) and name.lower() != last_segment:
            raise SharePointApiError(
                "Resolved SharePoint site does not match the intended site "
                f"({self._site_relative_path}). Refusing to continue."
            )
        self._resolved_site = payload
        return payload

    def _resolved_site_id(self) -> str:
        return str(self._resolve_site().get("id") or "")

    # -- folder access control ----------------------------------------------------

    def _require_folder_allowed(self, folder: str | None, operation: str) -> str:
        """Return the effective folder, rejecting anything outside the allowlist."""
        folder = (folder or "").strip().strip("/")
        if not self._allowed_folders:
            logger.info(
                "SharePoint request rejected: folder=%r operation=%s "
                "reason=no_folders_configured",
                folder,
                operation,
            )
            raise SharePointFolderNotAllowedError(NO_FOLDERS_CONFIGURED)
        if not folder:
            folder = self._default_folder()
        if folder.lower() not in {f.lower() for f in self._allowed_folders}:
            logger.info(
                "SharePoint request rejected: folder=%r operation=%s "
                "reason=not_in_folder_allowlist",
                folder,
                operation,
            )
            raise SharePointFolderNotAllowedError(
                f"SharePoint folder {folder} is not in the configured allowlist "
                "(SHAREPOINT_ALLOWED_FOLDERS) for this Knowledge Generative Agent."
            )
        return folder

    def _default_folder(self) -> str:
        return self._allowed_folders[0] if self._allowed_folders else ""

    # -- drive resolution -------------------------------------------------------

    def _resolve_drive_id(self) -> str:
        """Resolve the primary document library drive for the allowed site."""
        site_id = self._resolved_site_id()
        payload = self._request("GET", f"sites/{_quote(site_id)}/drives")
        drives = [d for d in payload.get("value", []) if isinstance(d, dict)]
        if not drives:
            raise SharePointApiError(
                f"No document libraries found in SharePoint site {site_id}."
            )
        # Prefer the "Documents" document library (SharePoint default).
        for drive in drives:
            name = str(drive.get("name") or "").lower()
            drive_type = str(drive.get("driveType") or "").lower()
            if drive_type == "documentlibrary" and name == "documents":
                return str(drive.get("id") or "")
        for drive in drives:
            drive_type = str(drive.get("driveType") or "").lower()
            if drive_type == "documentlibrary":
                return str(drive.get("id") or "")
        # Last resort: first drive (driveType may be "business" for a Documents lib).
        return str(drives[0].get("id") or "")

    # -- folder listing / scoped search -------------------------------------------

    def _list_folder_children(self, drive_id: str, folder: str) -> list[dict[str, Any]]:
        folder = self._graph_folder(folder)
        if folder:
            endpoint = (
                f"drives/{_quote(drive_id, safe='')}/root:/{_quote(folder, safe='/')}:/children"
            )
        else:
            endpoint = f"drives/{_quote(drive_id, safe='')}/root/children"
        params: dict[str, Any] = {
            "$top": _LIBRARY_CHILDREN_TOP,
            "$select": (
                "id,name,size,webUrl,createdDateTime,lastModifiedDateTime,"
                "createdBy,lastModifiedBy,folder,file,mimeType,parentReference"
            ),
        }
        payload = self._request("GET", endpoint, params=params)
        items = [i for i in payload.get("value", []) if isinstance(i, dict)]
        parent = self._display_folder(folder)
        for item in items:
            item["_drive_id"] = drive_id
            item["_parent_path"] = parent
        return items

    def _walk_folder_items(self, drive_id: str, folder: str, max_items: int) -> list[dict[str, Any]]:
        """List all items under an allowed folder (bounded; stays inside the folder)."""
        folder = self._graph_folder(folder)
        stack = [folder]
        results: list[dict[str, Any]] = []
        while stack and len(results) < max_items:
            current = stack.pop()
            children = self._list_folder_children(drive_id, current)
            for item in children:
                if len(results) >= max_items:
                    break
                results.append(item)
                name = str(item.get("name") or "")
                if item.get("folder") and name:
                    stack.append(f"{current}/{name}" if current else name)
        return results

    def _query_tokens(self, query: str) -> list[str]:
        """Distinctive search keywords (stop-words such as "explain", "the",
        "for" removed) so a natural-language question does not match every file."""
        return [tok for tok in _search_keywords(query) if len(tok) >= 2]

    @staticmethod
    def _name_contains(item: dict[str, Any], tokens: list[str]) -> bool:
        name = str(item.get("name") or "").lower().replace("_", " ").replace("-", " ")
        return any(token in name for token in tokens)

    def _try_content_text(self, item: dict[str, Any]) -> str:
        size = int(item.get("size") or 0)
        drive_id = str(item.get("_drive_id") or "")
        item_id = str(item.get("id") or "")
        if item.get("folder") or size <= 0 or size > _MAX_CONTENT_READ_BYTES:
            return ""
        if not drive_id or not item_id:
            return ""
        try:
            content_bytes = self._fetch_content_bytes(drive_id, item_id)
            text = self._document_parser.parse_document(
                content_bytes, str(item.get("name") or "")
            )
            return (text or "").strip()
        except Exception:  # noqa: BLE001 - unreadable/corrupt files are just non-matches
            return ""

    def _search_items_in_folder(
        self,
        folder: str,
        query: str,
        limit: int,
        *,
        include_content: bool,
    ) -> list[dict[str, Any]]:
        drive_id = self._resolve_drive_id()
        tokens = self._query_tokens(query)
        if not tokens:
            return []
        items = self._walk_folder_items(drive_id, folder, max_items=_MAX_SEARCH_ITEMS)
        results: list[dict[str, Any]] = []
        for item in items:
            if item.get("folder"):
                continue
            name_hit = self._name_contains(item, tokens)
            content_hit = False
            text = ""
            if include_content:
                text = self._try_content_text(item)
                content_hit = bool(text) and any(
                    token in text.lower() for token in tokens
                )
            if name_hit or content_hit:
                item["_matched_by"] = "name" if name_hit else "content"
                if include_content:
                    item["_content_text"] = text
                results.append(item)
            if len(results) >= limit:
                break
        return results

    # -- content fetch ----------------------------------------------------------

    def _fetch_content_bytes(self, drive_id: str, document_id: str) -> bytes:
        response = self._request(
            "GET",
            f"drives/{_quote(drive_id, safe='')}/items/{_quote(document_id, safe='')}/content",
            raw=True,
        )
        return response.content

    def _item_folder(self, item: dict[str, Any]) -> str:
        """Return the drive-relative folder path a drive item lives in.

        Parsed from ``parentReference.path`` (e.g. ``/drives/{id}/root:/kb-folder``).
        Items directly in the library root have an empty folder path.
        """
        parent = item.get("parentReference") or {}
        path = str(parent.get("path") or "")
        marker = "/root:"
        index = path.find(marker)
        if index < 0:
            return ""
        return unquote(path[index + len(marker):].strip("/"))

    @staticmethod
    def _folder_prefix(item_folder: str, allowed_folder: str) -> bool:
        item_folder = item_folder.strip("/").lower()
        allowed = allowed_folder.strip("/").lower()
        if allowed in {".", ""}:
            return True  # "." = the whole Documents library (root + subfolders)
        return item_folder == allowed or item_folder.startswith(allowed + "/")

    @staticmethod
    def _graph_folder(folder: str) -> str:
        """Drive-relative path for Graph calls (``.`` -> library root)."""
        folder = (folder or "").strip().strip("/")
        return "" if folder == "." else folder

    @staticmethod
    def _display_folder(folder: str) -> str:
        """Human-readable library path (``.`` -> ``Shared Documents``)."""
        folder = (folder or "").strip().strip("/")
        return "Shared Documents" if folder in {".", ""} else f"Shared Documents/{folder}"

    def _require_item_in_allowed_folder(
        self, item: dict[str, Any], folder_gate: str | None
    ) -> str:
        item_folder = self._item_folder(item)
        if folder_gate is not None:
            if not self._folder_prefix(item_folder, folder_gate):
                raise SharePointFolderNotAllowedError(
                    f"SharePoint document is not inside the allowed knowledge-base "
                    f"folder '{folder_gate}' (parent folder: '{item_folder or 'root'}')."
                )
            return folder_gate
        for allowed in self._allowed_folders:
            if self._folder_prefix(item_folder, allowed):
                return allowed
        raise SharePointFolderNotAllowedError(
            f"SharePoint document is not inside any configured knowledge-base "
            f"folder (SHAREPOINT_ALLOWED_FOLDERS). Parent folder: "
            f"'{item_folder or 'root'}'."
        )

    # -- tenant-wide mode (Microsoft Graph Search API) --------------------------

    def _graph_search_items(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Tenant-wide drive-item search via ``POST /search/query``.

        Searches every SharePoint site and document the application's Microsoft
        Graph permission can access (independent of any site/folder allowlist).
        Returns synthetic drive-item dicts (``_drive_id`` filled from the hit's
        ``parentReference.driveId``).
        """
        limit_n = max(1, min(limit, 25))
        request: dict[str, Any] = {
            "entityTypes": ["driveItem"],
            "query": {"queryString": query},
            "from": 0,
            "size": limit_n,
            "fields": [
                "id",
                "name",
                "webUrl",
                "size",
                "createdDateTime",
                "lastModifiedDateTime",
                "createdBy",
                "lastModifiedBy",
                "mimeType",
                "parentReference",
            ],
        }
        if self._search_region:
            # The Microsoft Graph Search API requires "region" when called with
            # application permissions (HTTP 400 "Region is required when request
            # with application permission" otherwise).
            request["region"] = self._search_region
        payload = self._request(
            "POST",
            "search/query",
            json_body={"requests": [request]},
        )
        hits: list[dict[str, Any]] = []
        for entry in payload.get("value", []) if isinstance(payload, dict) else []:
            for container in entry.get("hitsContainers", []):
                for hit in container.get("hits", []):
                    if not isinstance(hit, dict):
                        continue
                    resource = hit.get("resource")
                    if not isinstance(resource, dict):
                        continue
                    hits.append(self._graph_hit_to_item(resource))
        return hits

    @staticmethod
    def _graph_hit_to_item(resource: dict[str, Any]) -> dict[str, Any]:
        parent = resource.get("parentReference") or {}
        drive_id = str(parent.get("driveId") or "")
        item_id = str(resource.get("id") or "")
        name = str(resource.get("name") or "")
        parent_path = str(parent.get("path") or "").replace("/root:", "/").strip("/")
        return {
            "id": item_id,
            "name": name,
            "webUrl": str(resource.get("webUrl") or ""),
            "size": resource.get("size"),
            "createdDateTime": str(resource.get("createdDateTime") or ""),
            "lastModifiedDateTime": str(resource.get("lastModifiedDateTime") or ""),
            "createdBy": resource.get("createdBy") if isinstance(resource.get("createdBy"), dict) else {},
            "lastModifiedBy": resource.get("lastModifiedBy") if isinstance(resource.get("lastModifiedBy"), dict) else {},
            "mimeType": str(resource.get("mimeType") or ""),
            "_drive_id": drive_id,
            "_parent_path": parent_path,
        }

    def _search_items_relaxed(self, query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
        """Graph search with progressive query relaxation.

        Microsoft Graph search ANDs every token, so a natural-language question
        ("Explain the CI/CD pipeline flow for the Job Portal web application")
        matches nothing even though "Job Portal pipeline" would. Try the verbatim
        query first, then the distinctive keywords, then progressively shorter
        keyword sets, and finally the individual keywords (union, de-duplicated).
        Returns the hits and the query variant that produced them.
        """
        tried: list[str] = []
        for variant in _query_variants(query):
            if variant in tried:
                continue
            tried.append(variant)
            items = self._graph_search_items(variant, limit)
            if items:
                if variant != query:
                    logger.info(
                        "SharePoint search relaxed %r -> %r (%d hit(s))",
                        query,
                        variant,
                        len(items),
                    )
                return items, variant
        # Last resort: union of single-keyword searches (ranked by hit frequency).
        keywords = _search_keywords(query)
        if len(keywords) > 1:
            merged: dict[str, dict[str, Any]] = {}
            score: dict[str, int] = {}
            for keyword in keywords:
                for item in self._graph_search_items(keyword, limit):
                    key = str(item.get("id") or item.get("name"))
                    merged.setdefault(key, item)
                    score[key] = score.get(key, 0) + 1
            if merged:
                ranked = sorted(merged.values(), key=lambda it: -score[str(it.get("id") or it.get("name"))])
                logger.info(
                    "SharePoint search relaxed %r -> keyword union %r (%d hit(s))",
                    query,
                    keywords,
                    len(ranked),
                )
                return ranked[:limit], " OR ".join(keywords)
        return [], query

    def _tenant_wide_search(
        self,
        query: str,
        limit: int,
        *,
        include_content: bool,
    ) -> str:
        items, matched_query = self._search_items_relaxed(query, limit)
        if not items:
            return "No matching SharePoint documents found."

        blocks: list[str] = []
        for item in items:
            if include_content:
                text = self._try_content_text(item)
                item["_content_text"] = text
                blocks.append(self._format_item_with_content(item))
            else:
                blocks.append(self._format_drive_item(item))
        header = (
            f"[Source: SharePoint] Search results for \"{query}\" across all "
            "SharePoint content accessible to this agent"
            + (f" (matched on \"{matched_query}\")" if matched_query != query else "")
            + (f" (content of {len(items)} match(es))" if include_content else "")
            + ":"
        )
        return header + "\n\n" + "\n\n".join(blocks)

    def _tenant_wide_sites(self, limit: int) -> str:
        """List SharePoint sites the application's Graph permission can access."""
        limit_n = max(1, min(limit, 50))
        payload = self._request(
            "GET",
            "sites",
            params={"$select": "id,displayName,name,webUrl", "$top": limit_n},
        )
        sites = (
            [s for s in payload.get("value", []) if isinstance(s, dict)]
            if isinstance(payload, dict)
            else []
        )
        if not sites:
            return "No SharePoint sites are accessible to this agent."
        blocks = [
            "[Source: SharePoint] SharePoint sites accessible to this agent:"
        ]
        for site in sites:
            name = str(site.get("displayName") or site.get("name") or "Unnamed site")
            site_id = str(site.get("id") or "")
            web_url = str(site.get("webUrl") or "")
            blocks.append(f"- {name}\n  Site id: {site_id}\n  URL: {web_url}")
        return "\n".join(blocks)

    # -- formatting ---------------------------------------------------------------

    def _format_drive_item(self, item: dict[str, Any]) -> str:
        name = str(item.get("name") or "Untitled")
        web_url = str(item.get("webUrl") or "")
        item_id = str(item.get("id") or "")
        drive_id = str(item.get("_drive_id") or "")
        size = item.get("size")
        created = str(item.get("createdDateTime") or "")
        modified = str(item.get("lastModifiedDateTime") or "")
        created_by = _graph_person_name(item.get("createdBy"))
        modified_by = _graph_person_name(item.get("lastModifiedBy"))
        mime = str(item.get("mimeType") or "")
        parent = str(item.get("_parent_path") or "")
        is_folder = bool(item.get("folder"))
        lines = [
            f"[Source: SharePoint: {name}]",
            f"URL: {web_url}",
        ]
        if drive_id:
            lines.append(f"Drive id: {drive_id}")
        if item_id:
            lines.append(f"Document id: {item_id}")
        if size is not None:
            lines.append(f"Size: {size} bytes")
        if created_by:
            lines.append(f"Created by: {created_by}")
        if created:
            lines.append(f"Created: {created}")
        if modified_by:
            lines.append(f"Modified by: {modified_by}")
        if modified:
            lines.append(f"Modified: {modified}")
        if mime:
            lines.append(f"MimeType: {mime}")
        if parent:
            lines.append(f"Parent: {parent}")
        lines.append(f"Type: {'folder' if is_folder else 'file'}")
        return "\n".join(lines)

    def _format_item_with_content(self, item: dict[str, Any]) -> str:
        name = str(item.get("name") or "Untitled")
        web_url = str(item.get("webUrl") or "")
        item_id = str(item.get("id") or "")
        drive_id = str(item.get("_drive_id") or "")
        is_folder = bool(item.get("folder"))
        size = int(item.get("size") or 0)
        created_by = _graph_person_name(item.get("createdBy"))
        modified_by = _graph_person_name(item.get("lastModifiedBy"))
        header = (
            f"[Source: SharePoint: {name}]\n"
            f"URL: {web_url}\n"
            f"Drive id: {drive_id}\n"
            f"Document id: {item_id}"
        )
        if created_by:
            header += f"\nCreated by: {created_by}"
        if modified_by:
            header += f"\nModified by: {modified_by}"
        if is_folder:
            return header + "\nType: folder"
        if size > _MAX_DOWNLOAD_BYTES:
            return header + f"\nSize: {size} bytes - exceeds download limit"
        text = str(item.get("_content_text") or "").strip()
        if text:
            return header + f"\nSize: {size} bytes\n" + _cap(text, self._content_char_limit)
        return header + "\nNo readable text could be extracted from this document."

    # -- public API -------------------------------------------------------------

    def list_allowed_sites(self) -> str:
        """List the configured site + knowledge-base folder scope (no network)."""
        if self._tenant_wide:
            return (
                "[Source: SharePoint] SharePoint access is configured in tenant-wide "
                "mode (SHAREPOINT_TENANT_WIDE=true): this agent can search and read "
                "every SharePoint site and document the application's Microsoft "
                "Graph permission can access. Use list_sharepoint_documents to "
                "enumerate the accessible sites."
            )
        if not self._allowed_sites:
            return NO_SITES_CONFIGURED
        lines = [
            "[Source: SharePoint] Configured SharePoint sites for this Knowledge "
            "Generative Agent:"
        ]
        for site in sorted(self._allowed_sites):
            lines.append(f"- {site}")
        if self._allowed_folders:
            lines.append("Allowed knowledge-base folder(s):")
            for folder in sorted(self._allowed_folders):
                lines.append(f"- {self._display_folder(folder)}")
        return "\n".join(lines)

    def get_site(self) -> str:
        """Resolved site metadata (display name, URL) with attribution."""
        if self._tenant_wide:
            return self._tenant_wide_sites(self._limit)
        site = self._resolve_site()
        name = str(
            site.get("displayName")
            or site.get("name")
            or "Unnamed SharePoint site"
        )
        web_url = str(site.get("webUrl") or "")
        resolved_id = str(site.get("id") or "")
        return (
            f"[Source: SharePoint: {name}]\n"
            f"Site id: {resolved_id}\n"
            f"URL: {web_url}"
        )

    def list_drives(self) -> str:
        """Document libraries available in the resolved site (drive id + name)."""
        if self._tenant_wide:
            return self._tenant_wide_sites(self._limit)
        site_id = self._resolved_site_id()
        payload = self._request("GET", f"sites/{_quote(site_id)}/drives")
        drives = [d for d in payload.get("value", []) if isinstance(d, dict)]
        if not drives:
            return f"No document libraries found in SharePoint site {site_id}."
        lines = [f"[Source: SharePoint: site {site_id}]", "Document libraries:"]
        for drive in drives:
            name = str(drive.get("name") or "Unnamed library")
            drive_id = str(drive.get("id") or "?")
            drive_type = str(drive.get("driveType") or "")
            lines.append(f"- {name} (id: {drive_id}, type: {drive_type})")
        return "\n".join(lines)

    def list_files(
        self,
        folder: str | None = None,
        limit: int | None = None,
    ) -> str:
        """List files/folders inside a scoped folder, or sites in tenant-wide mode."""
        if self._tenant_wide:
            return self._tenant_wide_sites(limit or self._limit)
        folder = self._require_folder_allowed(folder, "list_files")
        site_id = self._resolved_site_id()
        drive_id = self._resolve_drive_id()
        limit_n = min(max(1, limit or 200), _LIBRARY_CHILDREN_TOP)
        items = self._walk_folder_items(drive_id, folder, max_items=limit_n)
        if not items:
            return (
                f"No files found in SharePoint site {site_id} "
                f"(folder: {self._display_folder(folder)})."
            )
        header = (
            f"[Source: SharePoint: site {site_id}]\n"
            f"Folder: {self._display_folder(folder)}\n"
            f"Document library drive id: {drive_id}"
        )
        lines = [header]
        for item in items:
            lines.append(self._format_drive_item(item))
        return "\n\n".join(lines)

    def search(
        self,
        query: str,
        folder: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Search across every accessible site (tenant-wide) or a scoped folder."""
        query = (query or "").strip()
        if not query:
            return "Please provide a non-empty search query."
        if self._tenant_wide:
            return self._tenant_wide_search(
                query, min(max(1, limit or self._limit), 25), include_content=False
            )
        folder = self._require_folder_allowed(folder, "search")
        site_id = self._resolved_site_id()
        limit_n = min(max(1, limit or self._limit), 25)
        items = self._search_items_in_folder(folder, query, limit_n, include_content=False)
        if not items:
            return (
                f"No matching documents found in the configured SharePoint "
                f"knowledge base folder '{folder}' for the query."
            )
        header = (
            f"[Source: SharePoint: site {site_id}] - "
            f"Search results for \"{query}\" (folder: {self._display_folder(folder)})"
        )
        blocks = [header] + [self._format_drive_item(item) for item in items]
        return "\n\n".join(blocks)

    def get_document_content(
        self,
        document_id: str,
        drive_id: str,
        folder: str | None = None,
    ) -> str:
        """Retrieve a file's metadata, download content and extract text.

        In scoped mode the document must live inside a configured knowledge-base
        folder (or inside ``folder`` when explicitly provided); otherwise the read
        is rejected before any content download. In tenant-wide mode the read is
        allowed by drive id + document id with no folder gating.
        """
        drive_id = (drive_id or "").strip().strip("/")
        document_id = (document_id or "").strip().strip("/")
        if not drive_id or not document_id:
            raise SharePointApiError(
                "Please provide both a drive id and a document id."
            )
        item_path = (
            f"drives/{_quote(drive_id, safe='')}"
            f"/items/{_quote(document_id, safe='')}"
        )
        if self._tenant_wide:
            meta = self._request(
                "GET",
                item_path,
                params={
                    "$select": (
                        "id,name,webUrl,size,lastModifiedDateTime,createdBy,lastModifiedBy,file,mimeType,parentReference"
                    ),
                },
            )
            return self._format_document_content(meta, drive_id, document_id)
        folder_gate = (
            self._require_folder_allowed(folder, "get_document")
            if folder
            else None
        )
        self._resolved_site_id()
        meta = self._request(
            "GET",
            item_path,
            params={
                "$select": (
                    "id,name,webUrl,size,lastModifiedDateTime,createdBy,lastModifiedBy,file,mimeType,parentReference"
                ),
            },
        )
        self._require_item_in_allowed_folder(meta, folder_gate)
        return self._format_document_content(meta, drive_id, document_id)

    def _format_document_content(
        self, meta: dict[str, Any], drive_id: str, document_id: str
    ) -> str:
        name = str(meta.get("name") or "document")
        web_url = str(meta.get("webUrl") or "")
        size = int(meta.get("size") or 0)
        header = (
            f"[Source: SharePoint: {name}]\n"
            f"URL: {web_url}\n"
            f"Drive id: {drive_id}\n"
            f"Document id: {document_id}"
        )
        if size > _MAX_DOWNLOAD_BYTES:
            return (
                header
                + f"\nDocument size ({size} bytes) exceeds the "
                f"{_MAX_DOWNLOAD_BYTES:,} byte download limit; content not retrieved."
            )
        try:
            content_bytes = self._fetch_content_bytes(drive_id, document_id)
        except SharePointApiError as exc:
            raise SharePointApiError(
                f"Could not download content of SharePoint document {name}: {exc}"
            ) from exc
        if not content_bytes:
            return header + "\nNo content could be downloaded from this document."
        # Reuse the existing DocumentParser for supported enterprise document types.
        try:
            text = self._document_parser.parse_document(content_bytes, name)
        except Exception:  # noqa: BLE001 - unsupported or corrupt formats
            return (
                header
                + "\nCould not extract readable text from this file type "
                f"({name})."
            )
        text = (text or "").strip()
        if not text:
            return header + "\nNo readable text extracted from this document."
        return header + "\n" + _cap(text, self._content_char_limit)

    def search_file_content(
        self,
        query: str,
        folder: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Search (scoped or tenant-wide), then return matching document content."""
        query = (query or "").strip()
        if not query:
            return "Please provide a non-empty search query."
        if self._tenant_wide:
            return self._tenant_wide_search(
                query, min(max(1, limit or self._limit), 25), include_content=True
            )
        folder = self._require_folder_allowed(folder, "search_file_content")
        site_id = self._resolved_site_id()
        limit_n = min(max(1, limit or self._limit), 25)
        items = self._search_items_in_folder(folder, query, limit_n, include_content=True)
        if not items:
            return (
                f"No matching documents found in the configured SharePoint "
                f"knowledge base folder '{folder}' for the query."
            )
        blocks = [self._format_item_with_content(item) for item in items]
        summary = (
            f"[Source: SharePoint: site {site_id}] - "
            f"Search results for \"{query}\" (folder: {self._display_folder(folder)}, "
            f"content of {len(items)} match(es))"
        )
        return summary + "\n\n" + "\n\n".join(blocks)

    def download_document_bytes(
        self,
        document_id: str,
        drive_id: str,
        folder: str | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        """Download the *original bytes* of a SharePoint document (Phase 4 export).

        Returns ``(content_bytes, metadata)`` where ``metadata`` carries
        ``name``, ``web_url``, ``size``, ``last_modified`` and ``mime_type``. The
        exact same access control as :meth:`get_document_content` is applied: in
        scoped mode the document must resolve inside a configured knowledge-base
        folder (or the explicit ``folder`` gate); in tenant-wide mode the read is
        allowed by drive/item id. Any folder/allowlist rejection raises before a
        byte is downloaded. Downloads larger than ``_MAX_DOWNLOAD_BYTES`` are
        refused so an export can never force the tenant-wide service to fetch an
        arbitrarily large blob.
        """
        drive_id = (drive_id or "").strip().strip("/")
        document_id = (document_id or "").strip().strip("/")
        if not drive_id or not document_id:
            raise SharePointApiError(
                "Please provide both a drive id and a document id."
            )
        item_path = (
            f"drives/{_quote(drive_id, safe='')}"
            f"/items/{_quote(document_id, safe='')}"
        )
        folder_gate: str | None = None
        if not self._tenant_wide:
            folder_gate = (
                self._require_folder_allowed(folder, "download_document_bytes")
                if folder
                else None
            )
            self._resolved_site_id()
            meta = self._request(
                "GET",
                item_path,
                params={
                    "$select": (
                        "id,name,webUrl,size,lastModifiedDateTime,createdBy,lastModifiedBy,file,mimeType,parentReference"
                    ),
                },
            )
            self._require_item_in_allowed_folder(meta, folder_gate)
        else:
            meta = self._request(
                "GET",
                item_path,
                params={
                    "$select": (
                        "id,name,webUrl,size,lastModifiedDateTime,createdBy,lastModifiedBy,file,mimeType,parentReference"
                    ),
                },
            )
        size = int(meta.get("size") or 0)
        if size > _MAX_DOWNLOAD_BYTES:
            raise SharePointApiError(
                f"SharePoint document '{str(meta.get('name') or document_id)}' "
                f"({size} bytes) exceeds the {_MAX_DOWNLOAD_BYTES:,} byte export "
                "download limit."
            )
        content_bytes = self._fetch_content_bytes(drive_id, document_id)
        if not content_bytes:
            raise SharePointApiError("No content could be downloaded from this document.")
        return content_bytes, {
            "name": str(meta.get("name") or "document"),
            "web_url": str(meta.get("webUrl") or ""),
            "size": size,
            "last_modified": str(meta.get("lastModifiedDateTime") or ""),
            "mime_type": str(meta.get("mimeType") or ""),
        }


def _quote(value: str, safe: str = "/") -> str:
    return quote(value, safe=safe)


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
