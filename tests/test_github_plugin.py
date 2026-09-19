"""Tests for :mod:`app.plugins.github_plugin` (GitHub-backed tools)."""

from __future__ import annotations

import logging

from app.core.exceptions import GitHubApiError
from app.plugins.github_plugin import (
    GET_FILE_CONTENT_DESCRIPTION,
    GET_ISSUE_DESCRIPTION,
    GET_README_DESCRIPTION,
    GET_REPOSITORY_DESCRIPTION,
    LIST_ALLOWED_REPOSITORIES_DESCRIPTION,
    LIST_REPOSITORY_CONTENTS_DESCRIPTION,
    RETRIEVE_REPOSITORY_CONTENTS_DESCRIPTION,
    SEARCH_CODE_DESCRIPTION,
    GitHubPlugin,
)


class StubGitHub:
    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[tuple[str, tuple]] = []
        self.repo_result = "[Source: GitHub: acme/payments]\nURL: https://github.com/acme/payments"
        self.single_issue_result = "[Source: GitHub: a/b#1]\nTitle: Checkout fails"

    def list_allowed_repositories(self) -> str:
        self.calls.append(("list_allowed_repositories", ()))
        return self.repo_result

    def get_repository(self, repo: str) -> str:
        self.calls.append(("get_repository", (repo,)))
        return self.repo_result

    def get_readme(self, repo: str) -> str:
        self.calls.append(("get_readme", (repo,)))
        return "[Source: GitHub: a/b]\nREADME of a/b"

    def list_repository_contents(self, repo: str, path: str = "") -> str:
        self.calls.append(("list_repository_contents", (repo, path)))
        return "[Source: GitHub: a/b]/"

    def get_file_content(self, repo: str, path: str) -> str:
        self.calls.append(("get_file_content", (repo, path)))
        return "[Source: GitHub: a/b:pay.py]"

    def retrieve_repository_contents(
        self, repo: str, *, max_items: int = 60, max_chars: int = 25_000
    ) -> str:
        self.calls.append(("retrieve_repository_contents", (repo, max_items, max_chars)))
        return f"[Source: GitHub: {repo}]\n### README.md\n..."

    def search_code(self, repository: str, query: str, limit: int | None = None) -> str:
        self.calls.append(("search_code", (repository, query, limit)))
        return self.repo_result

    def get_issue(self, repo: str, issue_number: int) -> str:
        self.calls.append(("get_issue", (repo, issue_number)))
        return self.single_issue_result


class FailGitHub(StubGitHub):
    def list_allowed_repositories(self) -> str:  # type: ignore[override]
        raise RuntimeError("boom")

    def get_readme(self, repo: str) -> str:  # type: ignore[override]
        raise RuntimeError("boom")


class NotFoundGitHub(StubGitHub):
    def get_repository(self, repo: str) -> str:  # type: ignore[override]
        raise GitHubApiError(f"GitHub resource not found (HTTP 404) at repos/{repo}")


def test_list_allowed_repositories_forwards_to_service() -> None:
    stub = StubGitHub()
    plugin = GitHubPlugin(stub)  # type: ignore[arg-type]

    result = plugin.list_allowed_repositories()

    assert result == stub.repo_result
    assert stub.calls == [("list_allowed_repositories", ())]


def test_get_repository_forwards_repo() -> None:
    stub = StubGitHub()
    plugin = GitHubPlugin(stub)  # type: ignore[arg-type]

    plugin.get_repository("acme/payments")

    assert stub.calls == [("get_repository", ("acme/payments",))]


def test_get_readme_forwards_repo() -> None:
    stub = StubGitHub()
    plugin = GitHubPlugin(stub)  # type: ignore[arg-type]

    result = plugin.get_readme("acme/payments")

    assert result == "[Source: GitHub: a/b]\nREADME of a/b"
    assert stub.calls == [("get_readme", ("acme/payments",))]


def test_list_repository_contents_forwards_path() -> None:
    stub = StubGitHub()
    plugin = GitHubPlugin(stub)  # type: ignore[arg-type]

    plugin.list_repository_contents("acme/payments", path="src")

    assert stub.calls == [("list_repository_contents", ("acme/payments", "src"))]


def test_get_file_content_forwards_path() -> None:
    stub = StubGitHub()
    plugin = GitHubPlugin(stub)  # type: ignore[arg-type]

    plugin.get_file_content("acme/payments", "src/pay.py")

    assert stub.calls == [("get_file_content", ("acme/payments", "src/pay.py"))]


def test_retrieve_repository_contents_forwards_with_defaults() -> None:
    stub = StubGitHub()
    plugin = GitHubPlugin(stub)  # type: ignore[arg-type]

    result = plugin.retrieve_repository_contents("acme/payments")

    assert "### README.md" in result
    assert stub.calls == [("retrieve_repository_contents", ("acme/payments", 60, 25_000))]


def test_retrieve_repository_contents_honours_explicit_caps() -> None:
    stub = StubGitHub()
    plugin = GitHubPlugin(stub)  # type: ignore[arg-type]

    plugin.retrieve_repository_contents("a/b", max_items=10, max_chars=5_000)

    assert stub.calls == [("retrieve_repository_contents", ("a/b", 10, 5_000))]


def test_search_code_forwards_repository_and_query() -> None:
    stub = StubGitHub()
    plugin = GitHubPlugin(stub)  # type: ignore[arg-type]

    plugin.search_code("acme/payments", "def charge", limit=3)

    assert stub.calls == [("search_code", ("acme/payments", "def charge", 3))]


def test_get_issue_forwards_number() -> None:
    stub = StubGitHub()
    plugin = GitHubPlugin(stub)  # type: ignore[arg-type]

    result = plugin.get_issue("a/b", 12)

    assert result == stub.single_issue_result
    assert stub.calls == [("get_issue", ("a/b", 12))]


def test_disabled_github_returns_marker() -> None:
    class DisabledStub(StubGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.enabled = False

    plugin = GitHubPlugin(DisabledStub())  # type: ignore[arg-type]
    assert "not configured" in plugin.list_allowed_repositories()
    assert "not configured" in plugin.retrieve_repository_contents("acme/payments")
    assert "not configured" in plugin.get_readme("a/b")
    assert "not configured" in plugin.search_code("a/b", "x")
    assert "not configured" in plugin.get_issue("a/b", 1)


def test_unavailable_github_returns_marker_not_exception() -> None:
    plugin = GitHubPlugin(FailGitHub())  # type: ignore[arg-type]
    assert "Could not list GitHub repositories" in plugin.list_allowed_repositories()
    assert "Could not retrieve the README" in plugin.get_readme("a/b")


def test_list_allowed_repositories_description_promotes_allowlist() -> None:
    description = LIST_ALLOWED_REPOSITORIES_DESCRIPTION.lower()
    for term in (
        "configured",
        "owner/name",
        "only these repositories are readable",
        "do not ask the user",
        "search_code",
    ):
        assert term in description, f"missing concept: {term}"
    assert "Payments" not in LIST_ALLOWED_REPOSITORIES_DESCRIPTION


def test_search_code_description_requires_repository_scope() -> None:
    description = SEARCH_CODE_DESCRIPTION.lower()
    assert "single github repository" in description
    assert "different from confluenceplugin" in description
    assert "search both" in description
    assert "configured for this knowledge generative agent" in description


def test_other_descriptions_mention_allowlist_and_followup_flow() -> None:
    for description in (
        GET_REPOSITORY_DESCRIPTION,
        GET_README_DESCRIPTION,
        LIST_REPOSITORY_CONTENTS_DESCRIPTION,
        GET_FILE_CONTENT_DESCRIPTION,
        RETRIEVE_REPOSITORY_CONTENTS_DESCRIPTION,
        GET_ISSUE_DESCRIPTION,
    ):
        assert "configured for this knowledge generative agent" in description.lower()
    assert "owner/name" in GET_README_DESCRIPTION.lower()
    assert "get_file_content" in LIST_REPOSITORY_CONTENTS_DESCRIPTION.lower()
    assert "path" in GET_FILE_CONTENT_DESCRIPTION.lower()
    assert "read-only" in GET_ISSUE_DESCRIPTION.lower()
    assert "repository" in GET_REPOSITORY_DESCRIPTION.lower()
    assert "file contents" in RETRIEVE_REPOSITORY_CONTENTS_DESCRIPTION.lower()
    assert "retrieve_repository_contents" in LIST_ALLOWED_REPOSITORIES_DESCRIPTION.lower()


def test_search_logs_invocation_and_result_count(caplog) -> None:
    plugin = GitHubPlugin(StubGitHub())  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO, logger="app.plugins.github_plugin"):
        plugin.search_code("acme/payments", "charge_payment", limit=3)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "search_code invoked: repository='acme/payments' query='charge_payment' limit=3" in msg
        for msg in messages
    )
    assert any("completed: repository='acme/payments' 1 results returned" in msg for msg in messages)


def test_repo_404_becomes_controlled_marker_not_exception() -> None:
    plugin = GitHubPlugin(NotFoundGitHub())  # type: ignore[arg-type]
    marker = plugin.get_repository("acme/payments")
    assert "Could not retrieve GitHub repository acme/payments" in marker
    assert "HTTP 404" in marker


def test_function_surface_is_fixed_and_parameterized() -> None:
    plugin = GitHubPlugin(StubGitHub())  # type: ignore[arg-type]
    names = {
        "list_allowed_repositories",
        "get_repository",
        "get_readme",
        "list_repository_contents",
        "get_file_content",
        "retrieve_repository_contents",
        "search_code",
        "get_issue",
    }
    exposed = {
        name
        for name in dir(plugin)
        if not name.startswith("_") and callable(getattr(plugin, name))
    }
    # No parameterless "search repositories" tool exists that could bypass the
    # allowlist; conversation history cannot add or enable additional tools either.
    assert exposed == names


# ---- capture integration tests ----


def test_get_readme_records_repository_in_capture() -> None:
    from app.export.capture import RetrievalCapture

    stub = StubGitHub()
    capture = RetrievalCapture()
    plugin = GitHubPlugin(stub, capture=capture)  # type: ignore[arg-type]

    plugin.get_readme("acme/payments")

    assert len(capture.items) == 1
    item = capture.items[0]
    assert item.source_type == "GITHUB"
    assert item.source_id == "acme/payments"


def test_list_repository_contents_records_path_in_capture() -> None:
    from app.export.capture import RetrievalCapture

    stub = StubGitHub()
    capture = RetrievalCapture()
    plugin = GitHubPlugin(stub, capture=capture)  # type: ignore[arg-type]

    plugin.list_repository_contents("acme/payments", path="src")

    assert len(capture.items) == 1
    item = capture.items[0]
    assert item.source_type == "GITHUB"
    assert item.source_id == "acme/payments"
    assert item.metadata.get("path") == "src"


def test_list_repository_contents_root_captures_no_path_metadata() -> None:
    from app.export.capture import RetrievalCapture

    stub = StubGitHub()
    capture = RetrievalCapture()
    plugin = GitHubPlugin(stub, capture=capture)  # type: ignore[arg-type]

    plugin.list_repository_contents("acme/payments")

    assert len(capture.items) == 1
    assert "path" not in capture.items[0].metadata
