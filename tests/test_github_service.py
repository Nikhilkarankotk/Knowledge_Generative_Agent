"""Tests for :mod:`app.services.github_service` (GitHubPlugin data provider).

Covers the repository allowlist security boundary: only ``GITHUB_ALLOWED_REPOSITORIES``
repositories are ever read, rejections happen before any API call, and no global
repository/code search is performed.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.exceptions import GitHubApiError, GitHubRepositoryNotAllowedError
from app.services.github_service import (
    NO_REPOSITORIES_CONFIGURED,
    GitHubService,
    _decode_base64,
)

BASE_URL = "https://api.github.com"

ALLOWED = ["acme/payments", "a/b", "eng/store"]


def make_service(handler, **kwargs) -> GitHubService:
    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    kwargs.setdefault("allowed_repositories", ALLOWED)
    kwargs.setdefault("api_token", "secret-token")
    return GitHubService(
        base_url=BASE_URL,
        client=client,
        **kwargs,
    )


def _repo_item(repo: str, *, language: str = "Python", stars: int = 42) -> dict:
    return {
        "full_name": repo,
        "name": repo.split("/")[-1],
        "html_url": f"https://github.com/{repo}",
        "description": f"Implementation of {repo}",
        "language": language,
        "stargazers_count": stars,
        "default_branch": "main",
        "topics": ["payments", "backend"],
    }


def _code_item(repo: str, path: str, snippet: str = "") -> dict:
    return {
        "name": path.split("/")[-1],
        "path": path,
        "html_url": f"https://github.com/{repo}/blob/main/{path}",
        "repository": {"full_name": repo},
        "text_matches": [{"fragment": snippet}] if snippet else "",
    }


# -- allowlist enforcement -------------------------------------------------------


def test_list_allowed_repositories_returns_configured_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("list_allowed_repositories must not call the GitHub API")

    service = make_service(handler, allowed_repositories=["acme/payments", "eng/store"])
    output = service.list_allowed_repositories()

    assert output.startswith("[Source: GitHub]")
    assert "acme/payments" in output
    assert "eng/store" in output
    assert "ShakibulAkash" not in output
    assert "github.com" not in output


def test_list_allowed_repositories_empty_allowlist_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty allowlist must never touch the GitHub API")

    service = make_service(handler, allowed_repositories=[])
    assert service.list_allowed_repositories() == NO_REPOSITORIES_CONFIGURED


def test_unconfigured_repository_rejected_before_api_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"API must not be called for disallowed repo: {request.url}")

    service = make_service(handler)
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.get_repository("ShakibulAkash/ecommerce-web-application")


def test_shakibulakash_unconfigured_repo_never_returns_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("banned repository must never reach an API call")

    service = make_service(handler)
    repo = "ShakibulAkash/ecommerce-web-application"
    for operation in (
        lambda: service.get_repository(repo),
        lambda: service.get_readme(repo),
        lambda: service.list_repository_contents(repo),
        lambda: service.get_file_content(repo, "README.md"),
        lambda: service.search_code(repo, "ecommerce"),
        lambda: service.get_issue(repo, 1),
    ):
        with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
            operation()


def test_empty_allowlist_refuses_every_repo_operation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"empty allowlist must never touch the GitHub API: {request.url}")

    service = make_service(handler, allowed_repositories=[])
    with pytest.raises(GitHubRepositoryNotAllowedError, match="No GitHub repositories"):
        service.get_repository("a/b")
    with pytest.raises(GitHubRepositoryNotAllowedError, match="No GitHub repositories"):
        service.search_code("a/b", "billing")


def test_allowed_repository_accepted_with_normalized_input() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/repos/acme/payments/readme")
        return httpx.Response(200, json={"content": base64.b64encode(b"# ok").decode()})

    service = make_service(handler)
    output = service.get_readme("  https://github.com/acme/payments.git  ")
    assert "[Source: GitHub: acme/payments]" in output


def test_allowlist_is_case_insensitive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_repo_item("ACME/PAYMENTS"))

    service = make_service(handler)
    output = service.get_repository("ACME/PAYMENTS")
    assert "[Source: GitHub: ACME/PAYMENTS]" in output


def test_multiple_allowed_repositories_all_readable_others_rejected() -> None:
    handled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        handled.append(request.url.path)
        return httpx.Response(200, json=_repo_item("acme/payments"))

    service = make_service(handler, allowed_repositories=["acme/payments", "eng/store"])
    service.get_repository("acme/payments")
    service.get_repository("eng/store")
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.get_repository("other/thing")

    assert len(handled) == 2
    assert {path.split("/")[2] for path in handled} == {"acme", "eng"}


def test_allowed_repositories_property_exposes_configured() -> None:
    service = make_service(lambda request: httpx.Response(200, json={}))
    assert service.allowed_repositories == ALLOWED


# -- no global search / scoped code search --------------------------------------


def test_no_unrestricted_global_search_functions_exist() -> None:
    service = make_service(lambda request: httpx.Response(200, json={}))
    assert not hasattr(service, "search_repositories")


def test_search_code_scoped_to_allowlisted_repository() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/search/code")
        params = parse_qs(request.url.query.decode())
        q = params["q"][0]
        assert q.startswith("repo:acme/payments ")
        assert "charge_payment" in q
        assert params["per_page"][0] == "5"
        return httpx.Response(
            200,
            json={
                "items": [
                    _code_item("acme/payments", "src/services/payments.py", snippet="def charge_payment")
                ]
            },
        )

    service = make_service(handler)
    output = service.search_code("acme/payments", "charge_payment")

    assert "[Source: GitHub: acme/payments:src/services/payments.py]" in output
    assert "def charge_payment" in output


def test_search_code_disallowed_repo_rejected_before_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unqualified/disallowed code search must not reach the API")

    service = make_service(handler)
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.search_code("ShakibulAkash/ecommerce-web-application", "checkout")
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.search_code("other/thing", "checkout")


def test_search_code_empty_results_message() -> None:
    service = make_service(lambda request: httpx.Response(200, json={"items": []}))
    assert "No matching code found in acme/payments" in service.search_code("acme/payments", "zzz")


def test_search_code_empty_query_message() -> None:
    service = make_service(lambda request: httpx.Response(200, json={"items": []}))
    assert "non-empty" in service.search_code("acme/payments", "   ")


# -- per-repository formatting ---------------------------------------------------


def test_get_repository_formats_metadata_with_topics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/repos/acme/payments")
        return httpx.Response(200, json=_repo_item("acme/payments"))

    service = make_service(handler)
    output = service.get_repository("acme/payments")

    assert "[Source: GitHub: acme/payments]" in output
    assert "Default branch: main" in output
    assert "Topics: payments, backend" in output


def test_get_readme_decodes_and_attributes() -> None:
    content = base64.b64encode(b"# Payments\nImplements the checkout.").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/repos/acme/payments/readme")
        return httpx.Response(200, json={"content": content})

    service = make_service(handler)
    output = service.get_readme("acme/payments")

    assert output.startswith("[Source: GitHub: acme/payments]")
    assert "Implements the checkout." in output


def test_get_readme_truncates_long_content() -> None:
    content = base64.b64encode(b"x" * 20000).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": content})

    service = make_service(handler, repo_char_limit=500)
    output = service.get_readme("a/b")
    assert len(output) < 600
    assert output.endswith("...")


def test_get_readme_no_content_message() -> None:
    service = make_service(lambda request: httpx.Response(200, json={"content": ""}))
    assert "No README content" in service.get_readme("a/b")


def test_list_repository_contents_directory() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/repos/acme/payments/contents")
        return httpx.Response(
            200,
            json=[
                {"name": "src", "type": "dir", "path": "src"},
                {"name": "README.md", "type": "file", "path": "README.md"},
            ],
        )

    service = make_service(handler)
    output = service.list_repository_contents("acme/payments")

    assert output.startswith("[Source: GitHub: acme/payments]/")
    assert "dir: src" in output
    assert "file: README.md" in output


def test_list_repository_contents_with_path() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        return httpx.Response(
            200,
            json=[{"name": "payments", "type": "dir", "path": "src/services/payments"}],
        )

    service = make_service(handler)
    output = service.list_repository_contents("a/b", path="src/services")
    assert output.startswith("[Source: GitHub: a/b:src/services]/")
    assert "dir: payments" in output
    assert captured[0].endswith("/repos/a/b/contents/src/services")


def test_walk_repository_via_single_recursive_tree() -> None:
    """The recursive git-trees endpoint replaces the per-directory contents walk."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        if request.url.path == "/repos/a/b":
            return httpx.Response(200, json={"default_branch": "main"})
        assert request.url.path == "/repos/a/b/git/trees/main"
        assert parse_qs(request.url.query.decode() or "").get("recursive") == ["1"]
        return httpx.Response(
            200,
            json={
                "sha": "x",
                "truncated": False,
                "tree": [
                    {"path": "src", "type": "tree"},
                    {"path": "src/app.py", "type": "blob"},
                    {"path": "src/tests/test_x.py", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ],
            },
        )

    service = make_service(handler)
    output = service.walk_repository("a/b")

    assert output == [
        {"path": "src", "type": "dir"},
        {"path": "src/app.py", "type": "file"},
        {"path": "src/tests/test_x.py", "type": "file"},
        {"path": "README.md", "type": "file"},
    ]
    assert captured == ["/repos/a/b", "/repos/a/b/git/trees/main"]


def test_walk_repository_falls_back_and_skips_noise_dirs() -> None:
    """Truncated trees fall back to the contents walk without descending into noise."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        if request.url.path == "/repos/a/b":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path.endswith("/git/trees/main"):
            return httpx.Response(200, json={"sha": "x", "truncated": True, "tree": []})
        if request.url.path == "/repos/a/b/contents":
            return httpx.Response(
                200,
                json=[
                    {"name": "target", "type": "dir", "path": "target"},
                    {"name": "src", "type": "dir", "path": "src"},
                ],
            )
        if request.url.path == "/repos/a/b/contents/src":
            return httpx.Response(
                200,
                json=[
                    {"name": "node_modules", "type": "dir", "path": "src/node_modules"},
                    {"name": "app.py", "type": "file", "path": "src/app.py"},
                ],
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    service = make_service(handler)
    output = service.walk_repository("a/b", skip_dirs=("target", "node_modules"))

    assert output == [
        {"path": "target", "type": "dir"},
        {"path": "src", "type": "dir"},
        {"path": "src/node_modules", "type": "dir"},
        {"path": "src/app.py", "type": "file"},
    ]
    # noise dirs recorded but never descended into
    assert "/repos/a/b/contents/target" not in captured
    assert "/repos/a/b/contents/src/node_modules" not in captured


def test_get_file_content_decodes_and_capped() -> None:
    body = base64.b64encode(b"def charge():\n    pass").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": body, "type": "file", "name": "pay.py"})

    service = make_service(handler)
    output = service.get_file_content("a/b", "src/pay.py")

    assert output.startswith("[Source: GitHub: a/b:src/pay.py]")
    assert "def charge()" in output


def test_get_file_content_missing_arguments_message() -> None:
    service = make_service(lambda request: httpx.Response(200, json={}))
    with pytest.raises(GitHubApiError, match="repository and a file path"):
        service.get_file_content("", "")


def test_get_issue_formats_attributed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/repos/a/b/issues/12")
        return httpx.Response(
            200,
            json={
                "title": "Checkout fails",
                "state": "open",
                "html_url": "https://github.com/a/b/issues/12",
                "labels": [{"name": "bug"}],
                "body": "Reproduce by ...",
            },
        )

    service = make_service(handler)
    output = service.get_issue("a/b", 12)

    assert "[Source: GitHub: a/b#12]" in output
    assert "Title: Checkout fails" in output
    assert "State: open" in output
    assert "Labels: bug" in output
    assert "Reproduce by ..." in output


# -- repository contents retrieval -------------------------------------------


def _retrieval_handler(
    files: dict[str, str], *, tree_ok: bool = True
):
    """Mock GitHub for retrieve_repository_contents (meta + tree + contents)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url.path
        if url.endswith("/repos/acme/payments"):
            return httpx.Response(200, json=_repo_item("acme/payments"))
        if "git/trees" in url:
            if not tree_ok:
                return httpx.Response(500, text="boom")
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {"type": "blob", "path": path, "size": len(content)}
                        for path, content in files.items()
                    ],
                },
            )
        if "/contents" in url:
            path = url.split("/contents", 1)[1].strip("/")
            if path in files:
                return httpx.Response(
                    200,
                    json={"content": base64.b64encode(files[path].encode()).decode()},
                )
            prefix = path + "/" if path else ""
            items: list[dict] = []
            seen: set[str] = set()
            for file_path in files:
                if not file_path.startswith(prefix):
                    continue
                rest = file_path[len(prefix):]
                if not rest:
                    continue
                top = rest.split("/")[0]
                if top in seen:
                    continue
                seen.add(top)
                entry_path = prefix + top
                items.append(
                    {"path": entry_path, "type": "dir" if "/" in rest else "file"}
                )
            if items or not path:
                return httpx.Response(200, json=items)
            return httpx.Response(404, json={})
        raise AssertionError(f"unexpected mocked path: {url}")

    return handler


def test_retrieve_repository_contents_reads_priority_files_and_skips_binary() -> None:
    files = {
        "README.md": "# Payments\nPayments platform.",
        "package.json": '{"name": "payments-app"}',
        "src/main.py": "import fastapi\napp = FastAPI()\n",
        "assets/logo.png": "PNGDATA",
    }
    service = make_service(_retrieval_handler(files))
    output = service.retrieve_repository_contents("acme/payments")

    assert output.startswith("[Source: GitHub: acme/payments]")
    assert "### README.md" in output
    assert "### package.json" in output
    assert "### src/main.py" in output
    assert "logo.png" not in output  # binary extension is never sampled
    assert "# Payments" in output
    assert output.index("### README.md") < output.index("### package.json")
    assert output.index("### package.json") < output.index("### src/main.py")


def test_retrieve_repository_contents_caps_characters() -> None:
    files = {
        f"src/module_{index}.py": f"def fn_{index}():\n    pass\n" * 100
        for index in range(8)
    }
    service = make_service(_retrieval_handler(files))
    output = service.retrieve_repository_contents("acme/payments", max_chars=1_500)

    import re

    sampled = int(re.search(r"(\d+) characters sampled", output).group(1))  # type: ignore[union-attr]
    assert sampled <= 1_500
    assert "more file(s) left unsampled" in output


def test_retrieve_repository_contents_rejects_unallowed_repo_before_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unallowed repo must not reach the API: {request.url}")

    service = make_service(handler, allowed_repositories=["engine/store"])
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.retrieve_repository_contents("acme/payments")


def test_retrieve_repository_contents_walks_without_tree_endpoint() -> None:
    files = {
        "README.md": "# Walk",
        "src/main.py": "print('main')",
    }
    service = make_service(_retrieval_handler(files, tree_ok=False))
    output = service.retrieve_repository_contents("acme/payments")

    assert "### README.md" in output
    assert "### src/main.py" in output
    assert "walk" in output.lower()


# -- transport/error mapping -----------------------------------------------------


def test_bearer_auth_header_sent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer secret-token"
        return httpx.Response(200, json={"content": base64.b64encode(b"x").decode()})

    service = make_service(handler)
    service.get_repository("acme/payments")
    service.get_readme("a/b")
    service.search_code("acme/payments", "x")

    assert len(seen) == 3


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "authentication failed"),
        (403, "authentication failed"),
    ],
)
def test_auth_errors_map_to_github_api_error_after_anonymous_fallback(
    status: int, message: str
) -> None:
    auth_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth_headers.append(request.headers.get("Authorization", ""))
        return httpx.Response(status, json={"message": "nope"})

    service = make_service(handler)
    with pytest.raises(GitHubApiError, match=message):
        service.get_repository("acme/payments")

    # The authenticated attempt failed and the anonymous fallback also failed:
    # a controlled error, never a leaked token or a fabricated answer.
    assert auth_headers == ["Bearer secret-token", ""]


def test_bad_token_falls_back_to_anonymous_and_reads_public_repo() -> None:
    auth_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth_headers.append(request.headers.get("Authorization", ""))
        if "Bearer" in request.headers.get("Authorization", ""):
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json={"content": base64.b64encode(b"# payments").decode()})

    service = make_service(handler)
    output = service.get_readme("acme/payments")

    assert "# payments" in output
    # First call authenticated (rejected), second call anonymous (public fallback).
    assert auth_headers == ["Bearer secret-token", ""]


def test_private_repo_after_fallback_reports_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "Bearer" in request.headers.get("Authorization", ""):
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(404, json={})

    service = make_service(handler)
    with pytest.raises(GitHubApiError, match="not found"):
        service.get_readme("acme/payments")


def test_rate_limit_never_triggers_anonymous_fallback() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("Authorization", ""))
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={})

    service = make_service(handler)
    with pytest.raises(GitHubApiError, match="rate limit"):
        service.search_code("acme/payments", "x")
    assert len(calls) == 1  # one authenticated attempt, no anonymous retry


def test_no_token_never_sends_auth_or_anonymous_retry() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("Authorization", ""))
        return httpx.Response(401, json={})

    service = make_service(handler, api_token="")
    with pytest.raises(GitHubApiError, match="authentication failed"):
        service.get_repository("acme/payments")
    assert len(calls) == 1
    assert calls == [""]


def test_rate_limit_error_maps_to_github_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={})

    service = make_service(handler)
    with pytest.raises(GitHubApiError, match="rate limit"):
        service.search_code("acme/payments", "x")


def test_http_errors_map_to_github_api_error() -> None:
    service = make_service(lambda request: httpx.Response(404, json={}))
    with pytest.raises(GitHubApiError, match="not found"):
        service.get_repository("acme/payments")

    service = make_service(lambda request: httpx.Response(503, json={}))
    with pytest.raises(GitHubApiError, match="server error"):
        service.search_code("acme/payments", "x")


def test_timeout_maps_to_github_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    service = make_service(handler)
    with pytest.raises(GitHubApiError, match="timed out"):
        service.get_repository("acme/payments")


def test_non_json_response_maps_to_github_api_error() -> None:
    service = make_service(lambda request: httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(GitHubApiError, match="non-JSON"):
        service.get_repository("acme/payments")


# -- diagnostic allowlist regression tests (A-H) --------------------------------


E_COMMERCE_REAL = "nikhilkarankotk/E-Commerce_Application"
E_COMMERCE_TYPO = "nikhilkarankotk/E-commerce-Application"


def test_a_allowed_e_commerce_repo_is_allowed_and_listed() -> None:
    config = ["nikhilkarankotk/E-Commerce_Application"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/repos/nikhilkarankotk/E-Commerce_Application")
        return httpx.Response(200, json=_repo_item(E_COMMERCE_REAL, language="Java"))

    service = make_service(handler, allowed_repositories=config)
    listing = service.list_allowed_repositories()
    assert E_COMMERCE_REAL in listing
    assert E_COMMERCE_TYPO not in listing

    # D. API 200 -> information retrieved for the configured repo.
    output = service.get_repository(E_COMMERCE_REAL)
    assert "[Source: GitHub: nikhilkarankotk/E-Commerce_Application]" in output


def test_a_punctuation_variant_is_not_silently_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"typo repo must not reach the API: {request.url}")

    service = make_service(handler, allowed_repositories=[E_COMMERCE_REAL])
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.get_repository(E_COMMERCE_TYPO)
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.get_readme(E_COMMERCE_TYPO)
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.search_code(E_COMMERCE_TYPO, "checkout")


def test_b_unallowed_other_owner_repo_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unallowed repo must not reach the API: {request.url}")

    service = make_service(handler, allowed_repositories=[E_COMMERCE_REAL])
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.get_repository("some-other-owner/public-repository")
    with pytest.raises(GitHubRepositoryNotAllowedError, match="allowlist"):
        service.search_code("some-other-owner/public-repository", "login")


def test_c_empty_allowlist_no_github_discovery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"empty allowlist must never call the API: {request.url}")

    service = make_service(handler, allowed_repositories=[])
    assert service.list_allowed_repositories() == NO_REPOSITORIES_CONFIGURED
    for operation in (
        lambda: service.get_repository("a/b"),
        lambda: service.get_readme("a/b"),
        lambda: service.list_repository_contents("a/b"),
        lambda: service.get_file_content("a/b", "x"),
        lambda: service.search_code("a/b", "billing"),
        lambda: service.get_issue("a/b", 1),
    ):
        with pytest.raises(GitHubRepositoryNotAllowedError, match="No GitHub repositories"):
            operation()


def test_e_repo_404_and_403_map_to_controlled_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    service = make_service(handler)
    with pytest.raises(GitHubApiError, match="not found"):
        service.get_repository("acme/payments")

    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    service = make_service(forbidden)
    with pytest.raises(GitHubApiError, match="authentication failed"):
        service.get_repository("acme/payments")


def test_h_returned_content_cannot_authorize_unlisted_repository() -> None:
    readme = base64.b64encode(b"See also https://github.com/other/thing for legacy.").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/repos/a/b/readme")
        return httpx.Response(200, json={"content": readme})

    service = make_service(handler, allowed_repositories=["a/b"])
    output = service.get_readme("a/b")
    assert "other/thing" in output
    # The allowlist is immutable: tool output referencing another repo must NOT
    # extend authorization after the fact (conversation-history attack).
    assert service.allowed_repositories == ["a/b"]
    try:
        service.get_repository("other/thing")
    except GitHubRepositoryNotAllowedError as exc:
        assert "allowlist" in str(exc)
    else:  # pragma: no cover - regression guard
        pytest.fail("unlisted repository referenced by tool output became authorized")


def test_f_generic_ecommerce_query_stays_repo_scoped() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.url.path.endswith("/search/code")
        params = parse_qs(request.url.query.decode())
        assert params["q"][0].startswith("repo:acme/payments ")
        return httpx.Response(200, json={"items": []})

    service = make_service(handler, allowed_repositories=["acme/payments"])
    service.search_code("acme/payments", "e-commerce architecture")
    assert "/search/repositories" not in requested
    assert requested == ["/search/code"]


# -- settings wiring -------------------------------------------------------------


def _settings(**overrides) -> SimpleNamespace:
    defaults = dict(
        github_enabled=True,
        github_token="tok",
        github_base_url=BASE_URL,
        github_limit=5,
        github_timeout_seconds=15.0,
        github_repo_char_limit=10000,
        github_allowed_repository_list=["acme/payments"],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_from_settings_disabled_returns_none() -> None:
    assert GitHubService.from_settings(_settings(github_enabled=False)) is None
    assert GitHubService.from_settings(_settings(github_token="")) is None


def test_from_settings_enabled_builds_service_with_allowlist() -> None:
    service = GitHubService.from_settings(_settings())
    assert service is not None
    assert service.enabled is True
    assert service.allowed_repositories == ["acme/payments"]
    service.close()


def test_decode_base64_is_robust() -> None:
    assert _decode_base64("aGVsbG8=") == "hello"
    assert _decode_base64("not-base64!!")  # must not raise
