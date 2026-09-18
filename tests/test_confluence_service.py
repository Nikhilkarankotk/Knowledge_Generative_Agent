"""Tests for :mod:`app.services.confluence_service` (ConfluencePlugin data provider)."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.exceptions import ConfluenceApiError
from app.services.confluence_service import ConfluenceService, html_to_text

BASE_URL = "https://wiki.example.com"


def make_service(handler, **kwargs) -> ConfluenceService:
    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return ConfluenceService(base_url=BASE_URL, client=client, **kwargs)


def _page_item(page_id: str, title: str, *, space: str = "Payments") -> dict:
    return {
        "id": page_id,
        "type": "page",
        "title": title,
        "space": {"name": space},
        "excerpt": f"Excerpt for {title}",
        "_links": {"webui": f"/spaces/{space}/pages/{page_id}"},
    }


def test_search_formats_attributed_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rest/api/content/search")
        params = parse_qs(request.url.query.decode())
        assert params["expand"][0] == "version,space,history,excerpt,ancestors"
        assert 'title ~ "billing"' in params["cql"][0] and "type = page" in params["cql"][0]
        return httpx.Response(
            200,
            json={"results": [_page_item("1000", "Billing 101")]},
        )

    service = make_service(handler)
    output = service.search("billing")

    assert "[Source: Confluence: Billing 101] (space: Payments)" in output
    assert "Page id: 1000" in output
    assert f"URL: {BASE_URL}/spaces/Payments/pages/1000" in output
    assert "Excerpt: Excerpt for Billing 101" in output


def test_search_title_phrase_single_keyword_makes_single_call() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cql = parse_qs(request.url.query.decode())["cql"][0]
        calls.append(cql)
        return httpx.Response(200, json={"results": [_page_item("1000", "Billing 101")]})

    service = make_service(handler, limit=5)
    output = service.search("billing")

    assert len(calls) == 1
    assert 'title ~ "billing"' in calls[0]
    assert "1000" in output


def test_search_cql_escapes_quotes() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(parse_qs(request.url.query.decode())["cql"][0])
        return httpx.Response(200, json={"results": []})

    service = make_service(handler)
    service.search('say "hello"')
    assert 'text ~ "say \\"hello\\""' in " ".join(captured)


def test_search_empty_results_message() -> None:
    service = make_service(lambda request: httpx.Response(200, json={"results": []}))
    output = service.search("nothing matches")
    assert output.startswith("No Confluence pages matched this query.")
    assert "no relevant documentation" in output


def test_search_finds_nested_page_under_application_anchor() -> None:
    """The bug: 'API Documentation' nested under 'Payments Application' was missed.

    The progressive strategy resolves the anchor page ("Payments application") by
    title, then searches its descendants with ``ancestor`` plus the topic, so the
    nested page surfaces even though flat keyword-OR would bury it.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cql = parse_qs(request.url.query.decode())["cql"][0]
        calls.append(cql)
        if 'title ~ "Payments application"' in cql:
            return httpx.Response(
                200,
                json={
                    "results": [
                        _page_item("PA1", "Payments Application", space="PAY")
                    ]
                },
            )
        if 'ancestor = "PA1"' in cql:
            return httpx.Response(
                200,
                json={
                    "results": [
                        _page_item("DOC1", "API Documentation", space="PAY")
                    ]
                },
            )
        if " OR " in cql:
            return httpx.Response(200, json={"results": [_page_item("X1", "Noise page")]})
        return httpx.Response(200, json={"results": []})

    service = make_service(handler, limit=5)
    output = service.search("Payments application API documentation")

    assert any("ancestor" in cql for cql in calls), "must resolve the anchor's descendants"
    assert "[Source: Confluence: API Documentation]" in output
    assert "Page id: DOC1" in output


def test_search_progressive_falls_back_to_keyword_or_when_no_hierarchy() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cql = parse_qs(request.url.query.decode())["cql"][0]
        calls.append(cql)
        if " OR " in cql:
            return httpx.Response(
                200,
                json={
                    "results": [
                        _page_item("1000", "Payments Architecture"),
                        _page_item("2000", "Billing notes"),
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})

    service = make_service(handler, limit=5)
    output = service.search("payments architecture governance")

    assert len(calls) >= 3, "title phrase, text phrase, then broaden"
    assert any(" OR " in cql for cql in calls)
    assert "[Source: Confluence: Payments Architecture]" in output
    assert "[Source: Confluence: Billing notes]" in output


def test_search_result_includes_direct_parent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "DOC1",
                        "type": "page",
                        "title": "API Documentation",
                        "space": {"name": "Payments"},
                        "excerpt": "REST endpoints",
                        "_links": {"webui": "/spaces/PAY/pages/DOC1"},
                        "ancestors": [
                            {"id": "ROOT", "title": "Home"},
                            {"id": "PA1", "title": "Payments Application"},
                        ],
                    }
                ]
            },
        )

    service = make_service(handler)
    output = service.search("API documentation")

    assert "[Source: Confluence: API Documentation]" in output
    assert "Parent: Payments Application" in output


def _page_item_with_ancestors(page_id: str, title: str, *, space: str = "Payments") -> dict:
    return {
        "id": page_id,
        "type": "page",
        "title": title,
        "space": {"name": space},
        "excerpt": f"Excerpt for {title}",
        "_links": {"webui": f"/spaces/{space}/pages/{page_id}"},
        "ancestors": [{"id": "PA1", "title": "Payments Application"}],
    }


def test_search_results_include_ancestors_in_expand() -> None:
    expand_values: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        expand_values.append(params["expand"][0])
        return httpx.Response(
            200,
            json={"results": [_page_item_with_ancestors("1000", "API Documentation")]},
        )

    service = make_service(handler)
    service.search("API documentation")

    assert all("ancestors" in expand for expand in expand_values)


def test_search_does_not_include_drafts_by_default() -> None:
    cqls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cqls.append(parse_qs(request.url.query.decode())["cql"][0])
        return httpx.Response(200, json={"results": []})

    service = make_service(handler)
    service.search("payments architecture")

    assert cqls and all("draft" not in cql for cql in cqls)


def test_search_includes_drafts_when_enabled() -> None:
    cqls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cqls.append(parse_qs(request.url.query.decode())["cql"][0])
        return httpx.Response(200, json={"results": []})

    service = make_service(handler, include_drafts=True)
    service.search("payments architecture")

    assert cqls and any("type = draft" in cql for cql in cqls)


def test_debug_logs_query_cql_and_result_counts(caplog) -> None:
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    _page_item("2000", "Onboarding"),
                    _page_item("1000", "Payments Architecture"),
                ]
            },
        )

    service = make_service(handler, limit=5)

    with caplog.at_level(logging.DEBUG, logger="app.services.confluence_service"):
        service.search("payments architecture")

    debug_lines = " ".join(record.getMessage() for record in caplog.records)
    assert "user_query='payments architecture'" in debug_lines
    assert 'title ~ "payments architecture"' in debug_lines
    assert "rest/api/content/search" in debug_lines
    assert "status=200" in debug_lines
    assert "titles=['Onboarding', 'Payments Architecture']" in debug_lines
    assert "ids=['2000', '1000']" in debug_lines


def test_search_scopes_cql_to_space() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["cql"] = parse_qs(request.url.query.decode())["cql"][0]
        return httpx.Response(200, json={"results": []})

    service = make_service(handler)
    service.search("billing", space_key="PAY")
    assert 'space = "PAY"' in captured["cql"]


def test_search_broadens_and_dedupes_on_low_recall() -> None:
    calls: list[str] = []
    phrase_only = [_page_item("1000", "Payments Architecture")]

    def handler(request: httpx.Request) -> httpx.Response:
        cql = parse_qs(request.url.query.decode())["cql"][0]
        calls.append(cql)
        if " OR " in cql:
            return httpx.Response(
                200,
                json={
                    "results": [
                        _page_item("1000", "Payments Architecture"),
                        _page_item("2000", "Billing notes"),
                        _page_item("3000", "Ledger overview"),
                    ]
                },
            )
        return httpx.Response(200, json={"results": phrase_only})

    service = make_service(handler, limit=2)
    output = service.search("payments architecture")

    assert len(calls) == 4, "expected title phrase, text phrase, ancestor, then broadening"
    assert 'title ~ "payments architecture"' in calls[0]
    assert 'text ~ "payments architecture"' in calls[1]
    assert 'title ~ "payments"' in calls[2]
    assert "ancestor" in calls[2] or "ancestor" in calls[3]
    ranked = [line for line in output.splitlines() if line.startswith("[Source")]
    assert len(ranked) == 2, "second query must cap merged results at the limit"
    assert ranked[0].startswith("[Source: Confluence: Payments Architecture]")
    assert ranked[1].startswith("[Source: Confluence: Billing notes]")


def test_search_single_keyword_does_not_broaden() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cql = parse_qs(request.url.query.decode())["cql"][0]
        calls.append(cql)
        return httpx.Response(200, json={"results": [_page_item("1000", "Billing 101")]})

    service = make_service(handler, limit=5)
    output = service.search("billing")

    assert len(calls) == 1
    assert "1000" in output


def test_list_spaces_formats_key_and_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rest/api/space")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"key": "PAY", "name": "Payments"},
                    {"key": "OPS", "name": "Operations"},
                ]
            },
        )

    service = make_service(handler)
    output = service.list_spaces()

    assert "Available Confluence spaces:" in output
    assert "[Source: Confluence] key=PAY, name=Payments" in output
    assert "[Source: Confluence] key=OPS, name=Operations" in output


def test_list_spaces_no_results_message() -> None:
    service = make_service(lambda request: httpx.Response(200, json={"results": []}))
    assert service.list_spaces() == "No Confluence spaces are accessible."


def test_get_page_strips_html_and_attributes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "2000",
                "type": "page",
                "title": "Onboarding",
                "_links": {"webui": "/spaces/OBS/pages/2000"},
                "body": {
                    "view": {
                        "value": "<p>Connect to <b>VPN</b>.</p><script>alert('x')</script><p>Then install the tools.</p>"
                    }
                },
            },
        )

    service = make_service(handler)
    output = service.get_page("2000")

    assert output.startswith("[Source: Confluence: Onboarding]")
    assert f"{BASE_URL}/spaces/OBS/pages/2000" in output
    assert "Connect to VPN" in output
    assert "Then install the tools" in output
    assert "alert" not in output


def test_get_page_without_content_reports_missing() -> None:
    service = make_service(
        lambda request: httpx.Response(
            200,
            json={
                "id": "2000",
                "title": "Empty Page",
                "body": {"view": {"value": ""}},
                "_links": {"webui": "/spaces/OBS/pages/2000"},
            },
        )
    )
    assert "No content available for Confluence page 'Empty Page'" in service.get_page("2000")


def _design_page_payload() -> dict:
    return {
        "id": "9000",
        "type": "page",
        "title": "Netflix System Design and Implementation",
        "_links": {"webui": "/spaces/ARCH/pages/9000"},
        "body": {"view": {"value": "<p>Design doc</p>"}},
        "version": {"by": {"displayName": "Tejaswinik"}, "when": "2026-02-02T10:00:00Z"},
        "history": {
            "createdBy": {"displayName": "Nikhil Karankot"},
            "lastUpdated": {
                "by": {"displayName": "Tejaswinik"},
                "when": "2026-02-02T10:00:00Z",
            },
        },
    }


def test_get_page_includes_owner_and_recent_editors_from_version_history() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "number": 2,
                            "by": {"displayName": "Tejaswinik", "accountId": "a2"},
                            "when": "2026-02-02T10:00:00Z",
                        },
                        {
                            "number": 1,
                            "by": {"displayName": "Nikhil Karankot", "accountId": "a1"},
                            "when": "2026-01-01T10:00:00Z",
                        },
                    ]
                },
            )
        return httpx.Response(200, json=_design_page_payload())

    service = make_service(handler)
    output = service.get_page("9000")

    assert "Owner: Nikhil Karankot" in output
    assert "Last modified by: Tejaswinik" in output
    assert "Recent editor: Tejaswinik | 2026-02-02T10:00:00Z | 2" in output
    assert "Recent editor: Nikhil Karankot | 2026-01-01T10:00:00Z | 1" in output
    assert "Design doc" in output


def test_get_page_degrades_gracefully_when_version_history_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version"):
            return httpx.Response(403, json={})
        return httpx.Response(200, json=_design_page_payload())

    service = make_service(handler)
    output = service.get_page("9000")

    assert "Owner: Nikhil Karankot" in output
    assert "Last modified by: Tejaswinik" in output
    assert "Recent editor: Tejaswinik | 2026-02-02T10:00:00Z" in output
    assert "Design doc" in output


def test_recent_editor_names_dedupes_same_person_across_versions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rest/api/content/9000/version")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"by": {"displayName": "Tejaswinik", "accountId": "a2"}},
                    {"by": {"displayName": "Tejaswinik", "accountId": "a2"}},
                    {"by": {"displayName": "Nikhil", "accountId": "a1"}},
                ]
            },
        )

    service = make_service(handler)
    assert service.recent_editor_names("9000") == ["Tejaswinik", "Nikhil"]


def test_recent_editor_versions_returns_ordered_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "number": 3,
                        "by": {"displayName": "Tejaswinik"},
                        "when": "2026-03-03T09:00:00Z",
                    },
                    {
                        "number": 2,
                        "by": {"displayName": "Tejaswinik"},
                        "when": "2026-02-02T10:00:00Z",
                    },
                    {
                        "number": 1,
                        "by": {"displayName": "Nikhil"},
                        "when": "2026-01-01T10:00:00Z",
                    },
                ]
            },
        )

    service = make_service(handler)
    assert service.recent_editor_versions("9000") == [
        {"name": "Tejaswinik", "when": "2026-03-03T09:00:00Z", "version": "3"},
        {"name": "Nikhil", "when": "2026-01-01T10:00:00Z", "version": "1"},
    ]


def test_recent_editor_names_never_fabricates_for_empty_history() -> None:
    service = make_service(
        lambda request: httpx.Response(200, json={"results": []})
    )
    assert service.recent_editor_names("9000") == []


def test_search_formats_owner_and_editor_from_history_expand() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "1",
                        "title": "Billing",
                        "space": {"name": "Payments"},
                        "_links": {"webui": "/pages/1"},
                        "version": {"by": {"displayName": "Tejaswinik"}},
                        "history": {"createdBy": {"displayName": "Nikhil Karankot"}},
                    }
                ]
            },
        )

    service = make_service(handler)
    output = service.search("billing")

    assert "Owner: Nikhil Karankot" in output
    assert "Last modified by: Tejaswinik" in output


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "authentication failed"),
        (403, "authentication failed"),
    ],
)
def test_bearer_auth_and_error_mapping(status: int, message: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        return httpx.Response(status, json={"message": "nope"})

    service = make_service(handler, api_token="secret-token")
    with pytest.raises(ConfluenceApiError, match=message):
        service.search("billing")


def test_basic_auth_when_username_set() -> None:
    import base64

    expected = "Basic " + base64.b64encode(b"me@example.com:token123").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == expected
        return httpx.Response(200, json={"results": []})

    service = make_service(
        handler, api_token="token123", username="me@example.com"
    )
    service.search("anything")


def test_http_errors_map_to_confluence_api_error() -> None:
    service = make_service(lambda request: httpx.Response(404, json={}))
    with pytest.raises(ConfluenceApiError, match="not found"):
        service.get_page("999")

    service = make_service(lambda request: httpx.Response(503, json={}))
    with pytest.raises(ConfluenceApiError, match="server error"):
        service.search("billing")


def test_timeout_maps_to_confluence_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    service = make_service(handler)
    with pytest.raises(ConfluenceApiError, match="timed out"):
        service.search("billing")


def test_non_json_response_maps_to_confluence_api_error() -> None:
    service = make_service(lambda request: httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(ConfluenceApiError, match="non-JSON"):
        service.search("billing")


def test_from_settings_disabled_returns_none() -> None:
    settings = SimpleNamespace(
        confluence_enabled=False,
        confluence_base_url="",
        confluence_api_token="",
        confluence_username="",
        confluence_limit=5,
        confluence_timeout_seconds=15.0,
        confluence_page_char_limit=15000,
    )
    assert ConfluenceService.from_settings(settings) is None


def test_from_settings_enabled_builds_service() -> None:
    settings = SimpleNamespace(
        confluence_enabled=True,
        confluence_base_url=BASE_URL,
        confluence_api_token="token",
        confluence_username="",
        confluence_limit=5,
        confluence_timeout_seconds=15.0,
        confluence_page_char_limit=15000,
    )
    service = ConfluenceService.from_settings(settings)
    assert service is not None
    assert service.enabled is True
    service.close()


def test_html_to_text_strips_tags_and_scripts() -> None:
    output = html_to_text("<html><body><p>Hello</p> <b>world</b><script>var secret = 1;</script></body></html>")
    assert "Hello" in output
    assert "world" in output
    assert "secret" not in output
