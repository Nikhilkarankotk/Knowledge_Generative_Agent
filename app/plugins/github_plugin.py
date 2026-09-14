"""GitHubPlugin - exposes read-only GitHub repository knowledge to the agent.

Delegates to :class:`~app.services.github_service.GitHubService`, which owns
authentication, request building, the ``GITHUB_ALLOWED_REPOSITORIES`` allowlist and
result formatting. The plugin only ever reads repositories configured for this
deployment; it contains no unrestricted global repository/code search. The plugin
never raises an unexpected error into the auto-invocation loop: GitHub failures
(including allowlist rejections) are translated into honest, non-fabricated
markers so the agent can answer from the sources that are still available.
"""

from __future__ import annotations

import logging

from semantic_kernel.functions import kernel_function

from app.core.exceptions import GitHubApiError
from app.services.github_service import GitHubService

# The @kernel_function decorator below runs signature introspection at class-definition
# time, which crashes on Python 3.14 unless the compatibility patch is applied first.
from app.sk.compat import apply_py314_compatibility_patch

apply_py314_compatibility_patch()

logger = logging.getLogger(__name__)

LIST_ALLOWED_REPOSITORIES_DESCRIPTION = (
    "List the GitHub repositories that are configured for this Knowledge "
    "Generative Agent (owner/name). Call this first when you need GitHub and are "
    "unsure which repositories are available. Only these repositories are readable; "
    "do not ask the user for other GitHub repositories. Follow up with "
    "list_repository_contents or get_readme to explore a listed repository, and "
    "search_code to find where a specific symbol is implemented inside it."
)

GET_REPOSITORY_DESCRIPTION = (
    "Retrieve metadata for a single GitHub repository (owner/name): default branch, "
    "description, topics, language and stars. Call this when you already know the "
    "repository and need its details, or to confirm a repository exists. Only "
    "repositories configured for this Knowledge Generative Agent are accessible."
)

GET_README_DESCRIPTION = (
    "Retrieve the README of a GitHub repository (owner/name) as plain text. The README "
    "describes the project, its modules and how to use them. Call this when you need an "
    "overview of what a repository implements or how its components fit together. Only "
    "repositories configured for this Knowledge Generative Agent are accessible."
)

LIST_REPOSITORY_CONTENTS_DESCRIPTION = (
    "List the files and folders at the root (or a given path) of a GitHub repository "
    "(owner/name). Call this to orient yourself in an unfamiliar repository before "
    "reading specific files with get_file_content, e.g. to find where a service or "
    "module lives. Only repositories configured for this Knowledge Generative Agent "
    "are accessible."
)

GET_FILE_CONTENT_DESCRIPTION = (
    "Retrieve the full text content of a single file in a GitHub repository "
    "by repository (owner/name) and path, e.g. 'src/services/payments.py'. Call this "
    "when you need the actual implementation of a class, function, configuration or "
    "dependency declaration. Only repositories configured for this Knowledge "
    "Generative Agent are accessible."
)

SEARCH_CODE_DESCRIPTION = (
    "Search the source code of a single GitHub repository (repository in owner/name "
    "form) for a query (e.g. a function name, class name or identifier) and return the "
    "matching files with their paths. Use this when you need to locate where a specific "
    "symbol or feature is implemented in code. The repository must be one of the "
    "repositories configured for this Knowledge Generative Agent. This is different "
    "from ConfluencePlugin, which searches documentation; if the question compares "
    "documentation with code, search both."
)

GET_ISSUE_DESCRIPTION = (
    "Retrieve a GitHub issue (read-only) by repository (owner/name) and issue number: "
    "title, state, labels and body. Use when the question refers to a specific GitHub "
    "issue, bug report or feature request. Only repositories configured for this "
    "Knowledge Generative Agent are accessible."
)


class GitHubPlugin:
    """Read-only GitHub repository/code access limited to the configured allowlist."""

    def __init__(self, github_service: GitHubService | None) -> None:
        self._service = github_service

    @kernel_function(
        description=LIST_ALLOWED_REPOSITORIES_DESCRIPTION,
        name="list_allowed_repositories",
    )
    def list_allowed_repositories(self) -> str:
        if not self._enabled:
            logger.info("GitHub list_allowed_repositories not invoked: GitHub not configured")
            return "GitHub access is not configured for this deployment."
        logger.info("GitHub list_allowed_repositories invoked")
        try:
            result = self._service.list_allowed_repositories()  # type: ignore[union-attr]
            logger.info("GitHub list_allowed_repositories completed")
            return result
        except GitHubApiError as exc:
            logger.warning("GitHub list_allowed_repositories failed: %s", exc)
            return f"Could not list GitHub repositories: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected GitHub list_allowed_repositories failure")
            return "Could not list GitHub repositories."

    @kernel_function(description=GET_REPOSITORY_DESCRIPTION, name="get_repository")
    def get_repository(self, repo: str) -> str:
        if not self._enabled:
            logger.info("GitHub get_repository not invoked: GitHub not configured")
            return "GitHub access is not configured for this deployment."
        logger.info("GitHub get_repository invoked: repo=%r", repo)
        try:
            result = self._service.get_repository(repo)  # type: ignore[union-attr]
            logger.info("GitHub get_repository completed: repo=%r", repo)
            return result
        except GitHubApiError as exc:
            logger.warning("GitHub get_repository(%s) failed: %s", repo, exc)
            return f"Could not retrieve GitHub repository {repo}: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected GitHub get_repository failure")
            return f"Could not retrieve GitHub repository {repo}."

    @kernel_function(description=GET_README_DESCRIPTION, name="get_readme")
    def get_readme(self, repo: str) -> str:
        if not self._enabled:
            logger.info("GitHub get_readme not invoked: GitHub not configured")
            return "GitHub access is not configured for this deployment."
        logger.info("GitHub get_readme invoked: repo=%r", repo)
        try:
            result = self._service.get_readme(repo)  # type: ignore[union-attr]
            logger.info("GitHub get_readme completed: repo=%r", repo)
            return result
        except GitHubApiError as exc:
            logger.warning("GitHub get_readme(%s) failed: %s", repo, exc)
            return f"Could not retrieve the README of {repo}: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected GitHub get_readme failure")
            return f"Could not retrieve the README of {repo}."

    @kernel_function(
        description=LIST_REPOSITORY_CONTENTS_DESCRIPTION,
        name="list_repository_contents",
    )
    def list_repository_contents(self, repo: str, path: str = "") -> str:
        if not self._enabled:
            logger.info("GitHub list_repository_contents not invoked: GitHub not configured")
            return "GitHub access is not configured for this deployment."
        logger.info("GitHub list_repository_contents invoked: repo=%r path=%r", repo, path)
        try:
            result = self._service.list_repository_contents(repo, path)  # type: ignore[union-attr]
            logger.info(
                "GitHub list_repository_contents completed: repo=%r path=%r",
                repo,
                path,
            )
            return result
        except GitHubApiError as exc:
            logger.warning("GitHub list_repository_contents(%s) failed: %s", repo, exc)
            return f"Could not list the contents of {repo}: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected GitHub list_repository_contents failure")
            return f"Could not list the contents of {repo}."

    @kernel_function(description=GET_FILE_CONTENT_DESCRIPTION, name="get_file_content")
    def get_file_content(self, repo: str, path: str) -> str:
        if not self._enabled:
            logger.info("GitHub get_file_content not invoked: GitHub not configured")
            return "GitHub access is not configured for this deployment."
        logger.info("GitHub get_file_content invoked: repo=%r path=%r", repo, path)
        try:
            result = self._service.get_file_content(repo, path)  # type: ignore[union-attr]
            logger.info("GitHub get_file_content completed: repo=%r path=%r", repo, path)
            return result
        except GitHubApiError as exc:
            logger.warning("GitHub get_file_content(%s:%s) failed: %s", repo, path, exc)
            return f"Could not retrieve the file {path} from {repo}: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected GitHub get_file_content failure")
            return f"Could not retrieve the file {path} from {repo}."

    @kernel_function(description=SEARCH_CODE_DESCRIPTION, name="search_code")
    def search_code(self, repository: str, query: str, limit: int | None = None) -> str:
        if not self._enabled:
            logger.info("GitHub search_code not invoked: GitHub not configured")
            return "GitHub access is not configured for this deployment."
        logger.info(
            "GitHub search_code invoked: repository=%r query=%r limit=%r",
            repository,
            query,
            limit,
        )
        try:
            result = self._service.search_code(repository, query, limit=limit)  # type: ignore[union-attr]
            logger.info(
                "GitHub search_code completed: repository=%r %d results returned",
                repository,
                _count_sources(result),
            )
            return result
        except GitHubApiError as exc:
            logger.warning("GitHub search_code(%s) failed: %s", repository, exc)
            return f"GitHub code search is currently unavailable: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected GitHub search_code failure")
            return "GitHub code search is currently unavailable."

    @kernel_function(description=GET_ISSUE_DESCRIPTION, name="get_issue")
    def get_issue(self, repo: str, issue_number: int) -> str:
        if not self._enabled:
            logger.info("GitHub get_issue not invoked: GitHub not configured")
            return "GitHub access is not configured for this deployment."
        logger.info("GitHub get_issue invoked: repo=%r issue=%r", repo, issue_number)
        try:
            result = self._service.get_issue(repo, issue_number)  # type: ignore[union-attr]
            logger.info("GitHub get_issue completed: repo=%r issue=%r", repo, issue_number)
            return result
        except GitHubApiError as exc:
            logger.warning("GitHub get_issue(%s#%s) failed: %s", repo, issue_number, exc)
            return f"Could not retrieve GitHub issue {repo}#{issue_number}: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected GitHub get_issue failure")
            return f"Could not retrieve GitHub issue {repo}#{issue_number}."

    @property
    def _enabled(self) -> bool:
        return self._service is not None and self._service.enabled


def _count_sources(text: str) -> int:
    return text.count("[Source")
