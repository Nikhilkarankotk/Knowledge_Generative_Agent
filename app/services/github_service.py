"""Client for the GitHub REST API (GitHubPlugin data provider, Phase 2).

Read-only knowledge integration. Encapsulates everything the plugin needs:

* ``list_allowed_repositories()`` - lists the repositories explicitly configured
  for this deployment (``GITHUB_ALLOWED_REPOSITORIES``); never a global search.
* ``get_repository(repo)`` - repository metadata (default branch, topics, stars).
* ``get_readme(repo)`` - the repository README (base64-decoded plain text).
* ``list_repository_contents(repo, path)`` - directory/root listing of a repository
  so the agent can orient itself before reading specific files.
* ``get_file_content(repo, path)`` - a single file's decoded text content.
* ``search_code(repository, query, limit)`` - code search scoped to a single
  allowlisted repository, returning matched files with attribution.
* ``get_issue(repo, issue_number)`` - issue title/state/labels/body (read-only).

Security boundary: ``GITHUB_ALLOWED_REPOSITORIES`` is enforced here, in the
service, *before* any HTTP request is made. No unscoped global repository or code
search (``/search/repositories``, ``/search/code``) is performed; ``search_code``
is restricted to a repository that passed the allowlist check and is always sent
with a ``repo:owner/name`` qualifier. When no repositories are configured, every
per-repository operation is refused without touching the network and the agent
receives a controlled "No GitHub repositories are configured" message.

All endpoints are read-only (no create/update/delete calls are made). Credentials
are handled here only, as ``Authorization: Bearer <token>``, and are never surfaced
to the LLM. Failures are raised as :class:`~app.core.exceptions.GitHubApiError`
(including :class:`~app.core.exceptions.GitHubRepositoryNotAllowedError` for
allowlist rejections) so the GitHubPlugin can turn them into honest,
non-fabricated markers for the agent.
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import _normalize_repository_id
from app.core.exceptions import GitHubApiError, GitHubRepositoryNotAllowedError

logger = logging.getLogger(__name__)

NO_REPOSITORIES_CONFIGURED = (
    "No GitHub repositories are configured for this Knowledge Generative Agent."
)

# Directories skipped when sampling repository contents (heavy/noise dirs).
_RETRIEVE_SKIP_DIRS = frozenset(
    {
        "node_modules", ".git", "dist", "build", "coverage", ".venv", "venv",
        "__pycache__", "vendor", "target", "bin", "obj", ".idea", ".vscode",
        ".next", ".mypy_cache", ".pytest_cache", "site-packages",
    }
)

# Binary / machine-generated extensions never read as repository contents.
_RETRIEVE_SKIP_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".ico", ".webp",
        ".avif", ".pdf", ".zip", ".gz", ".tgz", ".tar", ".jar", ".war",
        ".class", ".pyc", ".so", ".dll", ".dylib", ".exe", ".woff", ".woff2",
        ".ttf", ".eot", ".bin", ".dat", ".lock", ".map", ".mp4", ".mp3",
        ".wav", ".min.js", ".min.css", ".svg",
    }
)

# Files read first when sampling repository contents (root-level orientation).
_RETRIEVE_PRIORITY_FILES = frozenset(
    {
        "readme.md", "readme", "package.json", "pyproject.toml",
        "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg",
        "pom.xml", "build.gradle", "cargo.toml", "go.mod", "dockerfile",
        "docker-compose.yml", "docker-compose.yaml", "compose.yaml",
        "compose.yml", ".env.example", "application.yaml", "application.yml",
        "application.properties",
    }
)

# Files larger than this are skipped without downloading their body.
_RETRIEVE_SKIP_LARGE_BYTES = 200_000


def _clean_allowed_repositories(repositories: list[str] | None) -> list[str]:
    """Normalize, de-duplicate (case-insensitively) and order the allowlist."""
    result: list[str] = []
    seen: set[str] = set()
    for entry in repositories or []:
        repo = _normalize_repository_id(entry)
        if not repo:
            continue
        if repo.lower() in seen:
            continue
        seen.add(repo.lower())
        result.append(repo)
    return result


class GitHubService:
    """Thin read-only wrapper around the GitHub REST API.

    ``allowed_repositories`` is the repository allowlist (``owner/name`` strings).
    Every repository argument is validated against it before an HTTP request.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        limit: int = 5,
        timeout_seconds: float = 15.0,
        repo_char_limit: int = 10000,
        allowed_repositories: list[str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._limit = max(1, limit)
        self._timeout_seconds = timeout_seconds
        self._repo_char_limit = max(500, repo_char_limit)
        self._allowed_repositories = _clean_allowed_repositories(allowed_repositories)
        self._allowed_ids = {repo.lower() for repo in self._allowed_repositories}
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=timeout_seconds,
            headers={},
        )

    @classmethod
    def from_settings(cls, settings: Any) -> GitHubService | None:
        if not settings.github_enabled or not settings.github_token:
            return None
        return cls(
            base_url=settings.github_base_url,
            api_token=settings.github_token,
            limit=settings.github_limit,
            timeout_seconds=settings.github_timeout_seconds,
            repo_char_limit=settings.github_repo_char_limit,
            allowed_repositories=settings.github_allowed_repository_list,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._api_token)

    @property
    def allowed_repositories(self) -> list[str]:
        """The configured allowlist (canonical ``owner/name`` strings)."""
        return list(self._allowed_repositories)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass

    # -- request helpers --------------------------------------------------------

    def _perform_request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        anonymous: bool = False,
    ) -> httpx.Response:
        """Issue a GET request, attaching ``Authorization`` unless anonymous."""
        headers: dict[str, str] = {}
        if self._api_token and not anonymous:
            headers["Authorization"] = f"Bearer {self._api_token}"
        try:
            return self._client.get(path, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise GitHubApiError(f"GitHub request to {path} timed out.") from exc
        except httpx.HTTPError as exc:
            raise GitHubApiError(f"GitHub request to {path} failed: {exc}") from exc

    def _request_with_fallback(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """GET once; on auth failure retry anonymously (public-repository fallback).

        An invalid/expired ``GITHUB_TOKEN`` currently produces ``HTTP 401`` even
        for public, allowlisted repositories, which blocks all retrieval. Public
        repositories of the configured allowlist are still readable anonymously,
        so a 401/403 that is not a rate limit is retried once without the token.
        Private repositories remain protected: the anonymous attempt yields the
        usual 404, never the content. Rate-limited responses are never retried.
        """
        response = self._perform_request(path, params=params)
        if (
            self._api_token
            and response.status_code in (401, 403)
            and not _is_rate_limit_response(response)
        ):
            logger.info(
                "GitHub request to %s returned HTTP %s; retrying anonymously "
                "(allowlisted public-repository fallback).",
                path,
                response.status_code,
            )
            response = self._perform_request(path, params=params, anonymous=True)
        return response

    def _get_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = self._request_with_fallback(path, params=params)
        if response.status_code == 403 and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in (response.text or "").lower()
        ):
            raise GitHubApiError("GitHub API rate limit exceeded.")
        if response.status_code in (401, 403):
            raise GitHubApiError("GitHub authentication failed (HTTP 401/403).")
        if response.status_code == 404:
            raise GitHubApiError(f"GitHub resource not found (HTTP 404) at {path}.")
        if response.status_code >= 500:
            raise GitHubApiError(
                f"GitHub server error (HTTP {response.status_code}) at {path}."
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubApiError(
                f"GitHub request failed (HTTP {response.status_code})."
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise GitHubApiError("GitHub returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise GitHubApiError("GitHub returned an unexpected payload shape.")
        return data

    def _get_payload(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Like ``_get_json`` but tolerant of list payloads (``contents`` endpoint)."""
        response = self._request_with_fallback(path, params=params)
        if response.status_code == 403 and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in (response.text or "").lower()
        ):
            raise GitHubApiError("GitHub API rate limit exceeded.")
        if response.status_code in (401, 403):
            raise GitHubApiError("GitHub authentication failed (HTTP 401/403).")
        if response.status_code == 404:
            raise GitHubApiError(f"GitHub resource not found (HTTP 404) at {path}.")
        if response.status_code >= 500:
            raise GitHubApiError(
                f"GitHub server error (HTTP {response.status_code}) at {path}."
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubApiError(
                f"GitHub request failed (HTTP {response.status_code})."
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise GitHubApiError("GitHub returned a non-JSON response.") from exc
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            raise GitHubApiError("GitHub returned an unexpected payload shape.")
        return data

    # -- access control ---------------------------------------------------------

    def _require_allowed(self, repo: str, operation: str) -> str:
        """Validate ``repo`` against the allowlist or raise; returns the repo id.

        The check happens before any network request: unconfigured or not-allowed
        repositories are rejected locally and never hit the GitHub API. The token
        is never part of these messages.
        """
        repo = _normalize_repository_id(repo)
        if not repo:
            raise GitHubApiError("Please provide a repository in 'owner/name' form.")
        if not self._allowed_ids:
            logger.info(
                "GitHub request rejected: repository=%r operation=%s "
                "reason=no_repositories_configured",
                repo,
                operation,
            )
            raise GitHubRepositoryNotAllowedError(NO_REPOSITORIES_CONFIGURED)
        if repo.lower() not in self._allowed_ids:
            logger.info(
                "GitHub request rejected: repository=%r operation=%s reason=not_in_allowlist",
                repo,
                operation,
            )
            raise GitHubRepositoryNotAllowedError(
                f"GitHub repository {repo} is not in the configured allowlist "
                "for this Knowledge Generative Agent."
            )
        logger.debug("GitHub request allowed: repository=%r operation=%s", repo, operation)
        return repo

    # -- public API -------------------------------------------------------------

    def list_allowed_repositories(self) -> str:
        """List the repositories configured for this agent (allowlist only).

        No network call is made: this is purely the ``GITHUB_ALLOWED_REPOSITORIES``
        configuration, so an arbitrary public repository can never be returned.
        """
        if not self._allowed_repositories:
            return NO_REPOSITORIES_CONFIGURED
        lines = [
            "[Source: GitHub] Configured repositories for this Knowledge "
            "Generative Agent:"
        ]
        for repo in sorted(self._allowed_repositories):
            lines.append(f"- {repo}")
        return "\n".join(lines)

    def get_repository(self, repo: str) -> str:
        """Repository metadata (default branch, description, topics, stars)."""
        repo = self._require_allowed(repo, "get_repository")
        payload = self._get_json(f"repos/{_quote_repo(repo)}")
        return self._format_repository(payload, include_topics=True)

    def get_readme(self, repo: str) -> str:
        """The repository README as plain text (decoded, truncated)."""
        repo = self._require_allowed(repo, "get_readme")
        payload = self._get_json(f"repos/{_quote_repo(repo)}/readme")
        content = _decode_base64(str(payload.get("content") or ""))
        if not content.strip():
            return f"No README content available for {repo}."
        header = f"[Source: GitHub: {repo}]\nREADME of {repo}\n"
        return header + _cap(content.strip(), self._repo_char_limit)

    def list_repository_contents(self, repo: str, path: str = "") -> str:
        """List the contents of a repository directory (files and subdirectories)."""
        repo = self._require_allowed(repo, "list_repository_contents")
        path = (path or "").strip().strip("/")
        items = self._raw_contents(repo, path)
        if isinstance(items, dict):
            return self._format_single_item(items, repo)
        if not items:
            return f"No files found under '{repo}/{path}'.".replace("//", "/")
        lines = [
            f"[Source: GitHub: {repo}" + (f":{path}" if path else "") + "]/"
        ]
        for item in items:
            name = str(item.get("name") or "?")
            kind = "dir" if item.get("type") == "dir" else item.get("type", "file")
            lines.append(f"{kind}: {name}")
        return "\n".join(lines)

    def get_file_content(self, repo: str, path: str) -> str:
        """Single file text content (decoded, truncated) with attribution."""
        path = str(path or "").strip().strip("/")
        if not path:
            raise GitHubApiError("Please provide a repository and a file path.")
        repo = self._require_allowed(repo, "get_file_content")
        payload = self._raw_contents(repo, path)
        content = _decode_base64(str((payload.get("content") or "") if isinstance(payload, dict) else ""))
        if not content.strip():
            return f"No content available for {repo}:{path}."
        return (
            f"[Source: GitHub: {repo}:{path}]\n"
            + _cap(content.strip(), self._repo_char_limit)
        )

    def get_file_content_text(self, repo: str, path: str, *, char_limit: int = 200_000) -> str:
        """Decoded file text for the export analysis (allowlist-enforced).

        Additive read-only helper used by the export service's repository
        analysis. The repository is validated against the allowlist before any
        request; the returned text is *not* attributed and may be up to
        ``char_limit`` characters (truncated otherwise).
        """
        path = str(path or "").strip().strip("/")
        if not path:
            raise GitHubApiError("Please provide a repository and a file path.")
        repo = self._require_allowed(repo, "get_file_content_text")
        payload = self._raw_contents(repo, path)
        content = _decode_base64(str((payload.get("content") or "") if isinstance(payload, dict) else ""))
        return _cap(content.strip(), max(500, char_limit))

    def retrieve_repository_contents(
        self,
        repo: str,
        *,
        max_items: int = 60,
        max_chars: int = 25_000,
        per_file_chars: int = 8_000,
    ) -> str:
        """Sample and attribute the actual contents of a repository.

        Uses the bounded repository tree, then reads the most relevant readable
        text files (root-level manifest/README files first) up to ``max_chars``
        characters. Heavy directories, binary/lock/minified files and files
        larger than ~200KB are skipped. The repository is validated against the
        allowlist before any request. Returns an attributed, deterministic,
        character-bounded text block the agent can ground an answer on - a
        single-call replacement for guessing paths and reading many files.
        """
        repo = self._require_allowed(repo, "retrieve_repository_contents")
        blobs = self._bounded_blobs(
            repo,
            max_items=max_items,
            skip_dirs=_RETRIEVE_SKIP_DIRS,
        )

        candidates: list[tuple[str, int | None]] = []
        skipped_noise = 0
        for path, size in blobs:
            base = path.rsplit("/", 1)[-1].lower()
            if any(base.endswith(ext) for ext in _RETRIEVE_SKIP_EXTENSIONS):
                skipped_noise += 1
                continue
            if size is not None and size > _RETRIEVE_SKIP_LARGE_BYTES:
                skipped_noise += 1
                continue
            candidates.append((path, size))

        candidates.sort(
            key=lambda item: _retrieve_sort_key(item[0]),
        )

        blocks: list[str] = []
        read_chars = 0
        read_files = 0
        unreadable = 0
        for path, _size in candidates:
            remaining = max_chars - read_chars
            if remaining <= 0:
                break
            take = min(max(500, per_file_chars), remaining)
            try:
                content = self.get_file_content_text(repo, path, char_limit=take)
            except GitHubApiError:
                unreadable += 1
                continue
            if not content.strip():
                unreadable += 1
                continue
            blocks.append(f"### {path}\n{content}")
            read_chars += len(content)
            read_files += 1

        missing = len(candidates) - read_files - unreadable
        header = (
            f"[Source: GitHub: {repo}]\n"
            f"Repository contents of {repo}: {read_files} file(s), "
            f"{read_chars} characters sampled"
        )
        if missing > 0:
            header += f" ({missing} more file(s) left unsampled to stay within limits)"
        if unreadable:
            header += f"; {unreadable} file(s) could not be read"
        if not blocks:
            return header + ".\nNo readable text file contents were retrieved."
        return header + ":\n\n" + "\n\n".join(blocks)

    def walk_repository(
        self,
        repo: str,
        *,
        max_items: int = 200,
        max_depth: int = 4,
        skip_dirs: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Bounded listing of a repository's tree (export analysis).

        Prefers the recursive ``git/trees`` endpoint (a single request); falls back
        to a breadth-first ``contents`` walk when the tree is truncated or the API
        is unavailable. Returns ``{path, type}`` dicts (``type`` in ``file``/``dir``).
        During the fallback walk, directories named in ``skip_dirs`` are recorded but
        never descended into.
        """
        repo = self._require_allowed(repo, "walk_repository")
        skip = {str(name).lower() for name in skip_dirs if name}

        entries = self._tree_recursive(repo)
        if entries is not None:
            results: list[dict[str, Any]] = []
            for entry in entries:
                if len(results) >= max_items:
                    break
                entry_type = "dir" if entry.get("type") == "tree" else "file"
                results.append({"path": str(entry.get("path") or ""), "type": entry_type})
            return results

        tree_results: list[dict[str, Any]] = []
        pending: list[tuple[str, int]] = [("", 0)]
        while pending and len(tree_results) < max_items:
            path, depth = pending.pop(0)
            items = self._raw_contents(repo, path)
            if isinstance(items, dict):
                items = [items]
            for item in items:
                if len(tree_results) >= max_items:
                    break
                entry_path = str(item.get("path") or "")
                entry_type = "dir" if item.get("type") == "dir" else "file"
                tree_results.append({"path": entry_path, "type": entry_type})
                if entry_type == "dir" and depth < max_depth:
                    name = entry_path.rsplit("/", 1)[-1].lower()
                    if name in skip:
                        continue
                    pending.append((entry_path, depth + 1))
        return tree_results

    def _tree_recursive(self, repo: str) -> list[dict[str, Any]] | None:
        """All repository paths via the recursive git-trees endpoint (one request).

        Returns ``None`` (never raises) on API errors or truncated trees so callers
        can fall back to the contents walk.
        """
        try:
            meta = self._get_json(f"repos/{_quote_repo(repo)}")
            branch = str(meta.get("default_branch") or "master")
            payload = self._get_payload(
                f"repos/{_quote_repo(repo)}/git/trees/{quote(branch, safe='')}",
                params={"recursive": "1"},
            )
        except Exception:  # noqa: BLE001 - caller falls back to the contents walk
            return None
        if not isinstance(payload, dict) or payload.get("truncated"):
            return None
        tree = payload.get("tree")
        if not isinstance(tree, list):
            return None
        return [
            entry
            for entry in tree
            if isinstance(entry, dict) and entry.get("type") in ("blob", "tree")
        ]

    def _bounded_blobs(
        self,
        repo: str,
        *,
        max_items: int,
        skip_dirs: frozenset[str],
    ) -> list[tuple[str, int | None]]:
        """Bounded ``(path, size)`` list of repository files (``size`` optional).

        Prefers the recursive ``git/trees`` endpoint (which includes blob sizes
        and needs a single request); falls back to a breadth-first ``contents``
        walk (no sizes) when the tree endpoint fails or is truncated.
        """
        skip = {name.lower() for name in skip_dirs if name}
        blobs: list[tuple[str, int | None]] = []
        entries = self._tree_recursive(repo)
        if entries is not None:
            for entry in entries:
                if len(blobs) >= max_items:
                    break
                if entry.get("type") != "blob":
                    continue
                path = str(entry.get("path") or "")
                if _path_has_skip_dir(path, skip):
                    continue
                blobs.append((path, entry.get("size")))
            return blobs

        pending: list[tuple[str, int]] = [("", 0)]
        while pending and len(blobs) < max_items:
            path, depth = pending.pop(0)
            items = self._raw_contents(repo, path)
            if isinstance(items, dict):
                items = [items]
            for item in items:
                if len(blobs) >= max_items:
                    break
                item_path = str(item.get("path") or "")
                if item.get("type") == "dir":
                    if depth < 5 and not _path_has_skip_dir(item_path, skip):
                        pending.append((item_path, depth + 1))
                    continue
                if _path_has_skip_dir(item_path, skip):
                    continue
                blobs.append((item_path, item.get("size")))
        return blobs

    def search_code(self, repository: str, query: str, limit: int | None = None) -> str:
        """GitHub code search restricted to a single allowlisted repository.

        The repository is validated against the allowlist before any request and
        is also sent as a ``repo:owner/name`` qualifier so the search can never
        return files from repositories outside the allowlist.
        """
        query = (query or "").strip()
        if not query:
            return "Please provide a non-empty search query."
        repository = self._require_allowed(repository, "search_code")
        limit_n = min(max(1, limit or self._limit), 25)
        q = f"repo:{repository} {query}"
        payload = self._get_json(
            "search/code",
            params={"q": q, "per_page": limit_n},
        )
        results = [item for item in payload.get("items", []) if isinstance(item, dict)]
        if not results:
            return f"No matching code found in {repository} for the query."
        blocks: list[str] = []
        for item in results:
            repo_name = str((item.get("repository") or {}).get("full_name") or repository)
            path = str(item.get("path") or "")
            url = str(item.get("html_url") or "")
            header = f"[Source: GitHub: {repo_name}:{path}]"
            lines = [header, f"URL: {url}"]
            snippet = str(item.get("text_matches") or "")
            if snippet and snippet != "[]":
                lines.append(f"Snippet: {_cap(snippet, 500)}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def get_issue(self, repo: str, issue_number: int) -> str:
        """Issue title/state/labels/body (read-only) with attribution."""
        repo = self._require_allowed(repo, "get_issue")
        payload = self._get_json(f"repos/{_quote_repo(repo)}/issues/{int(issue_number)}")
        title = str(payload.get("title") or "Untitled")
        state = str(payload.get("state") or "")
        url = str(payload.get("html_url") or "")
        labels = ", ".join(
            str(item.get("name") or "")
            for item in payload.get("labels", [])
            if isinstance(item, dict)
        )
        body = str(payload.get("body") or "").strip()
        header = (
            f"[Source: GitHub: {repo}#{issue_number}]\n"
            f"Title: {title}\nState: {state}\n"
            + (f"Labels: {labels}\n" if labels else "")
            + f"URL: {url}"
        )
        if body:
            header += "\n" + _cap(body, self._repo_char_limit)
        return header

    # -- internals --------------------------------------------------------------

    def _raw_contents(
        self, repo: str, path: str
    ) -> dict[str, Any] | list[dict[str, Any]]:
        url = f"repos/{_quote_repo(repo)}/contents"
        if path:
            url += "/" + quote(path, safe="/")
        return self._get_payload(url)

    def _format_single_item(self, item: dict[str, Any], repo: str) -> str:
        name = str(item.get("name") or "?")
        if item.get("type") == "dir":
            return f"[Source: GitHub: {repo}]\nDirectory: {name}/"
        return self.get_file_content(repo, name)

    def _format_repository(self, item: dict[str, Any], include_topics: bool = False) -> str:
        full_name = str(item.get("full_name") or item.get("name") or "unknown")
        html_url = str(item.get("html_url") or "")
        lines = [f"[Source: GitHub: {full_name}]", f"URL: {html_url}"]
        branch = (item.get("default_branch") or "") if include_topics else ""
        if branch:
            lines.append(f"Default branch: {branch}")
        language = item.get("language")
        stars = item.get("stargazers_count")
        if language or stars:
            detail = "Language: " + str(language or "n/a")
            if stars is not None:
                detail += f", Stars: {stars}"
            lines.append(detail)
        description = (item.get("description") or "").strip()
        if description:
            lines.append(f"Description: {_cap(description, 300)}")
        if include_topics and isinstance(item.get("topics"), list):
            topics = [str(t) for t in item["topics"] if isinstance(t, str)]
            if topics:
                lines.append("Topics: " + ", ".join(topics))
        return "\n".join(lines)


def _quote_repo(repo: str) -> str:
    """Quote 'owner/name' for use in a URL path, preserving the slash."""
    return quote(repo, safe="/")


def _retrieve_base_name(path: str) -> str:
    return str(path).rsplit("/", 1)[-1].lower()


def _retrieve_sort_key(path: str) -> tuple[int, str]:
    """README first, then root manifest/config files, then the rest (stable)."""
    base = _retrieve_base_name(path)
    if base in ("readme.md", "readme", "readme.rst", "readme.txt"):
        tier = 0
    elif base in _RETRIEVE_PRIORITY_FILES:
        tier = 1
    else:
        tier = 2
    return (tier, path.lower())


def _path_has_skip_dir(path: str, skip: set[str]) -> bool:
    """True when any path segment is in the skip set (case-insensitive)."""
    parts = str(path).lower().split("/")
    return any(part in skip for part in parts)


def _decode_base64(raw: str) -> str:
    try:
        decoded = base64.b64decode(raw)
        return decoded.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - never crash the agent on a bad payload
        return str(raw)


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _is_rate_limit_response(response: httpx.Response) -> bool:
    """True for GitHub's rate-limit 403 (never eligible for the public fallback)."""
    return response.status_code == 403 and (
        response.headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in (response.text or "").lower()
    )
