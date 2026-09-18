"""Tests for :mod:`app.services.sharepoint_service` (SharePoint/Graph client).

Covers the Phase 3 security boundaries:

* The configured site is resolved from ``SHAREPOINT_SITE_HOSTNAME`` +
  ``SHAREPOINT_SITE_RELATIVE_PATH`` and validated against
  ``SHAREPOINT_ALLOWED_SITES`` before any further Graph call.
* Every list/search/read is restricted to ``SHAREPOINT_ALLOWED_FOLDERS``; folder
  and document-id arguments outside the allowlist are rejected before any Graph
  call, so there is no arbitrary site, drive, folder or Graph-URL access.
* No tenant-wide site discovery/search is performed.
* Token acquisition and Graph reads are mocked via ``httpx`` ``MockTransport``
  clients; normal tests never require live SharePoint credentials.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.exceptions import (
    SharePointApiError,
    SharePointFolderNotAllowedError,
    SharePointSiteNotAllowedError,
)
from app.services.sharepoint_service import (
    NO_SITES_CONFIGURED,
    SharePointService,
)

TOKEN_HOST = "https://login.microsoftonline.com"
GRAPH_HOST = "https://graph.microsoft.com/v1.0"

SITE_HOST = "knowledgegenagent.sharepoint.com"
SITE_PATH = "/sites/KnowledgeGenAgent"
SITE = (
    "knowledgegenagent.sharepoint.com,"
    "438af0af-fed2-4470-9ac8-7216f61117c9,"
    "25126283-31b9-43ca-8dfa-c9168ad08501"
)
FOLDER = "sharepoint-rag-knowledge-base"
DRIVE = "b!doclib"

SITE_PAYLOAD = {
    "id": SITE,
    "name": "KnowledgeGenAgent",
    "displayName": "KnowledgeGenAgent",
    "webUrl": f"https://{SITE_HOST}/sites/KnowledgeGenAgent",
}

ITEM_SELECT = ("id,name,size,webUrl,createdDateTime,lastModifiedDateTime,"
               "folder,file,mimeType,parentReference")


def _make_service(
    graph_handler,
    *,
    allowed_sites: list[str] | None = None,
    allowed_folders: list[str] | None = None,
    site_hostname: str = SITE_HOST,
    site_relative_path: str = SITE_PATH,
    tenant_id: str = "tenant",
    client_id: str = "client",
    client_secret: str = "secret",
    **kwargs,
) -> SharePointService:
    if allowed_sites is None:
        allowed_sites = [SITE]
    if allowed_folders is None:
        allowed_folders = [FOLDER]
    token_client = httpx.Client(
        base_url=TOKEN_HOST,
        transport=httpx.MockTransport(_token_handler),
    )
    graph_client = httpx.Client(
        base_url=GRAPH_HOST,
        transport=httpx.MockTransport(graph_handler),
    )
    kwargs.setdefault("retry_backoff_seconds", 0)
    return SharePointService(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        graph_base_url=GRAPH_HOST,
        site_hostname=site_hostname,
        site_relative_path=site_relative_path,
        allowed_sites=allowed_sites,
        allowed_folders=allowed_folders,
        token_client=token_client,
        graph_client=graph_client,
        **kwargs,
    )


def _token_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": "test-token", "expires_in": 3600},
    )


def _drives_payload() -> dict:
    return {
        "value": [
            {"id": DRIVE, "name": "Documents", "driveType": "documentLibrary"},
            {"id": "b!other", "name": "Assets", "driveType": "documentLibrary"},
        ]
    }


def _file_item(name: str, item_id: str, *, in_folder: bool = True) -> dict:
    parent = {
        "name": "Documents",
        "path": (
            f"/drives/{DRIVE}/root:/{FOLDER}" if in_folder else f"/drives/{DRIVE}/root:"
        ),
    }
    return {
        "id": item_id,
        "name": name,
        "size": 1844,
        "webUrl": f"https://{SITE_HOST}/sites/KnowledgeGenAgent/Shared Documents/{FOLDER}/{name}",
        "createdDateTime": "2026-09-14T15:18:44Z",
        "lastModifiedDateTime": "2026-09-14T15:18:44Z",
        "mimeType": "text/markdown",
        "file": {},
        "parentReference": parent,
    }


def _folder_child(name: str, item_id: str) -> dict:
    parent = {"name": "Documents", "path": f"/drives/{DRIVE}/root:/{FOLDER}"}
    return {
        "id": item_id,
        "name": name,
        "size": None,
        "webUrl": f"https://{SITE_HOST}/sites/KnowledgeGenAgent/Shared Documents/{FOLDER}/{name}",
        "createdDateTime": "2026-09-14T15:18:44Z",
        "lastModifiedDateTime": "2026-09-14T15:18:44Z",
        "mimeType": "",
        "folder": {"childCount": 0},
        "parentReference": parent,
    }


def _library_handler(value: list[dict]) -> callable:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        if ":/children" in path or path.endswith("/root/children"):
            return httpx.Response(200, json={"value": value}, request=request)
        raise AssertionError(f"unexpected path: {path}")

    return handler


# -- enablement -----------------------------------------------------------------


def test_enabled_requires_all_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit the network")

    assert _make_service(handler, client_secret="").enabled is False
    assert _make_service(handler, tenant_id="").enabled is False
    assert _make_service(handler, client_id="").enabled is False
    assert _make_service(handler).enabled is True


def test_from_settings_disabled_returns_none() -> None:
    from app.core.config import Settings

    assert SharePointService.from_settings(Settings(sharepoint_enabled=False)) is None


def test_from_settings_uses_allowlist_and_folders() -> None:
    from app.core.config import Settings

    service = SharePointService.from_settings(
        Settings(
            sharepoint_enabled=True,
            sharepoint_tenant_id="t",
            sharepoint_client_id="c",
            sharepoint_client_secret="s",
            sharepoint_site_hostname="a.sharepoint.com",
            sharepoint_site_relative_path="/sites/KnowledgeGenAgent",
            sharepoint_allowed_sites="a.com,1,2;b.com,3,4",
            sharepoint_allowed_folders="kb-a; kb-b",
        )
    )
    assert service is not None
    assert service.allowed_sites == ["a.com,1,2", "b.com,3,4"]
    assert service.allowed_folders == ["kb-a", "kb-b"]
    service.close()


def test_missing_credentials_raises_controlled_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the network")

    service = _make_service(handler, client_secret="")
    with pytest.raises(SharePointApiError, match="credentials missing"):
        service.search("query")


# -- allowlist / guard rails -----------------------------------------------------


def test_empty_site_allowlist_refuses_every_operation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"API must not be called: {request.url}")

    service = _make_service(handler, allowed_sites=[])
    with pytest.raises(SharePointSiteNotAllowedError, match="No SharePoint sites"):
        service.get_site()
    with pytest.raises(SharePointSiteNotAllowedError, match="No SharePoint sites"):
        service.search("query")


def test_empty_folder_allowlist_refuses_every_retrieval() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"API must not be called: {request.url}")

    service = _make_service(handler, allowed_folders=[])
    with pytest.raises(SharePointFolderNotAllowedError, match="folders are configured"):
        service.search("query")
    with pytest.raises(SharePointFolderNotAllowedError, match="folders are configured"):
        service.list_files()


def test_list_allowed_sites_returns_configured_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("list_allowed_sites must not call the SharePoint API")

    service = _make_service(handler)
    output = service.list_allowed_sites()
    assert output.startswith("[Source: SharePoint] Configured SharePoint sites")
    assert SITE in output
    assert f"Shared Documents/{FOLDER}" in output
    assert "evil.sharepoint.com" not in output


def test_list_allowed_sites_empty_allowlist_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty allowlist must never touch the SharePoint API")

    service = _make_service(handler, allowed_sites=[])
    assert service.list_allowed_sites() == NO_SITES_CONFIGURED


def test_wrong_folder_rejected_before_any_graph_call() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        raise AssertionError(f"API must not be called for disallowed folder: {request.url}")

    service = _make_service(handler)
    for operation in (
        lambda: service.search("x", folder="some-other-folder"),
        lambda: service.list_files(folder="../../../tmp"),
        lambda: service.search_file_content("x", folder="Shared Documents/kb"),
    ):
        with pytest.raises(SharePointFolderNotAllowedError, match="allowlist"):
            operation()
    assert seen == []


def test_no_arbitrary_folder_traversal_tokens_accepted() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        raise AssertionError("must not call the network")

    service = _make_service(handler)
    with pytest.raises(SharePointFolderNotAllowedError, match="allowlist"):
        service.search("x", folder="../escape")
    with pytest.raises(SharePointFolderNotAllowedError, match="allowlist"):
        service.search("x", folder="sharepoint-rag-knowledge-base/../other")
    assert seen == []


# -- site resolution ------------------------------------------------------------

SITE_PAYLOAD_RESPONSE = {"value": []}


def test_get_site_resolves_configured_site() -> None:
    seen: list[str] = []

    def graph_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        raise AssertionError(f"unexpected path: {request.url.path}")

    service = _make_service(graph_handler)
    output = service.get_site()
    assert "[Source: SharePoint: KnowledgeGenAgent]" in output
    assert f"Site id: {SITE}" in output
    assert "URL: https://knowledgegenagent.sharepoint.com/sites/KnowledgeGenAgent" in output
    assert any("/sites/knowledgegenagent.sharepoint.com:/sites/KnowledgeGenAgent" in p for p in seen)


def test_resolved_site_outside_allowlist_rejected_before_further_api_calls() -> None:
    seen: list[str] = []
    evil = "evil.sharepoint.com,999,888"

    def graph_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        # Only the resolution endpoint may be called; it returns a site outside
        # the allowlist, so every further Graph call must be refused.
        if request.url.path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            payload = dict(SITE_PAYLOAD)
            payload["id"] = evil
            payload["name"] = "Some Other Site"
            payload["webUrl"] = "https://evil.sharepoint.com/sites/Other"
            return httpx.Response(200, json=payload, request=request)
        raise AssertionError(f"API must not be called after rejection: {request.url}")

    service = _make_service(graph_handler)
    with pytest.raises(SharePointSiteNotAllowedError, match="evil.sharepoint.com"):
        service.search("anything")
    # Only the site-resolution request happened.
    assert len(seen) == 1


def test_resolved_site_weburl_mismatch_rejected() -> None:
    seen: list[str] = []

    def graph_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            payload = dict(SITE_PAYLOAD)
            payload["name"] = "SomethingElse"
            payload["webUrl"] = "https://knowledgegenagent.sharepoint.com/sites/SomethingElse"
            return httpx.Response(200, json=payload, request=request)
        raise AssertionError(f"unexpected path: {request.url.path}")

    service = _make_service(graph_handler)
    with pytest.raises(SharePointApiError, match="does not match the intended site"):
        service.list_files()


# -- token handling -------------------------------------------------------------


def test_token_request_uses_client_credentials_flow() -> None:
    requests: list[httpx.Request] = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "tok-1", "expires_in": 3600},
        )

    def graph_handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" in request.headers
        assert request.headers["Authorization"] == "Bearer tok-1"
        if request.url.path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        raise AssertionError(f"unexpected path: {request.url.path}")

    token_client = httpx.Client(base_url=TOKEN_HOST, transport=httpx.MockTransport(token_handler))
    graph_client = httpx.Client(base_url=GRAPH_HOST, transport=httpx.MockTransport(graph_handler))
    service = SharePointService(
        tenant_id="tenant-1",
        client_id="client-1",
        client_secret="secret-1",
        graph_base_url=GRAPH_HOST,
        site_hostname=SITE_HOST,
        site_relative_path=SITE_PATH,
        allowed_sites=[SITE],
        allowed_folders=[FOLDER],
        token_client=token_client,
        graph_client=graph_client,
        retry_backoff_seconds=0,
    )
    result = service.get_site()
    assert "[Source: SharePoint: KnowledgeGenAgent]" in result
    assert requests[0].url.path == "/tenant-1/oauth2/v2.0/token"
    from urllib.parse import parse_qs

    data = parse_qs(requests[0].content.decode())
    assert data["grant_type"] == ["client_credentials"]
    assert data["client_id"] == ["client-1"]
    assert data["client_secret"] == ["secret-1"]
    assert data["scope"] == ["https://graph.microsoft.com/.default"]
    service.close()


def test_access_token_cached_until_expiry() -> None:
    token_calls: list[str] = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        token_calls.append("token")
        return httpx.Response(
            200,
            json={"access_token": "tok-cached", "expires_in": 3600},
        )

    def graph_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok-cached"
        if request.url.path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        raise AssertionError(f"unexpected path: {request.url.path}")

    token_client = httpx.Client(base_url=TOKEN_HOST, transport=httpx.MockTransport(token_handler))
    graph_client = httpx.Client(base_url=GRAPH_HOST, transport=httpx.MockTransport(graph_handler))
    service = SharePointService(
        tenant_id="tenant-1",
        client_id="client-1",
        client_secret="secret-1",
        graph_base_url=GRAPH_HOST,
        site_hostname=SITE_HOST,
        site_relative_path=SITE_PATH,
        allowed_sites=[SITE],
        allowed_folders=[FOLDER],
        token_client=token_client,
        graph_client=graph_client,
        retry_backoff_seconds=0,
    )
    service.get_site()
    service.get_site()
    assert len(token_calls) == 1
    service.close()


def test_missing_or_wrong_site_target_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("API must not be called")

    service = _make_service(handler, site_hostname="", site_relative_path="")
    with pytest.raises(SharePointApiError, match="SHAREPOINT_SITE_HOSTNAME"):
        service.get_site()


# -- drive resolution and listing -------------------------------------------------


def test_list_drives_lists_document_libraries() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler)
    output = service.list_drives()
    assert "b!doclib" in output
    assert "Assets" in output


def test_documents_drive_resolved_for_retrieval() -> None:
    seen: list[str] = []

    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        if f"/drives/{DRIVE}/root:/{FOLDER}:/children" in path:
            return httpx.Response(
                200,
                json={"value": [_file_item("a.md", "aa")]},
                request=request,
            )
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler)
    output = service.list_files(limit=10)
    assert "Document library drive id: b!doclib" in output
    # Drives listing happened scoped to the resolved site, never tenant-wide.
    assert f"/sites/{SITE}/drives" in " ".join(seen)


def test_root_folder_allowlist_lists_and_reads_library_root() -> None:
    """SHAREPOINT_ALLOWED_FOLDERS='.' scopes the agent to the whole Documents
    library of the configured site (the 'Shared Documents' view), where files
    stored directly at the root - not in a subfolder - must be listable, searchable
    and readable, while the scope still never leaves that one site."""
    from app.core.config import _normalize_folder_id

    assert _normalize_folder_id(".") == "."
    assert _normalize_folder_id("Shared Documents") == "."
    assert _normalize_folder_id("root") == "."

    seen: list[str] = []

    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        if path.endswith(f"/drives/{DRIVE}/root/children"):
            return httpx.Response(
                200,
                json={"value": [_file_item("Job_Portal CICD Pipeline flow.md", "jp", in_folder=False)]},
                request=request,
            )
        if path.endswith(f"/drives/{DRIVE}/items/jp"):
            return httpx.Response(
                200, json=_file_item("Job_Portal CICD Pipeline flow.md", "jp", in_folder=False), request=request
            )
        if path.endswith(f"/drives/{DRIVE}/items/jp/content"):
            return httpx.Response(200, content=b"Job Portal pipeline text", request=request)
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler, allowed_folders=["."])

    listing = service.list_files()
    assert "Folder: Shared Documents" in listing
    assert "Shared Documents/." not in listing
    assert "Job_Portal CICD Pipeline flow.md" in listing
    assert any(p.endswith("/root/children") for p in seen)  # root, not a subfolder

    # A natural-language question still finds the root file by distinctive keywords.
    found = service.search("Explain the CI/CD pipeline flow for the Job Portal web application")
    assert "Job_Portal CICD Pipeline flow.md" in found

    # A root-level item is inside the '.' scope, so it can be read.
    content = service.get_document_content(document_id="jp", drive_id=DRIVE)
    assert "Job Portal pipeline text" in content


def test_list_files_lists_allowed_folder_only() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        if f"/drives/{DRIVE}/root:/{FOLDER}:/children" in path:
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("01-content-boundaries-and-governance.md", "doc1"),
                        _file_item("README.md", "doc2"),
                        _folder_child("subfolder", "dir1"),
                    ]
                },
                request=request,
            )
        if path.endswith(":/children"):
            return httpx.Response(200, json={"value": []}, request=request)
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler)
    output = service.list_files(limit=10)
    assert "[Source: SharePoint: 01-content-boundaries-and-governance.md]" in output
    assert "[Source: SharePoint: subfolder]" in output
    assert "Folder: Shared Documents/sharepoint-rag-knowledge-base" in output
    assert "Parent: Shared Documents/sharepoint-rag-knowledge-base" in output
    assert "MimeType: text/markdown" in output
    assert "Created: 2026-09-14T15:18:44Z" in output


# -- search ---------------------------------------------------------------------


def test_search_scoped_to_allowed_folder_matches_filenames() -> None:
    seen: list[str] = []

    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        if f"/drives/{DRIVE}/root:/{FOLDER}:/children" in path:
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("01-content-boundaries-and-governance.md", "doc1"),
                        _file_item("02-onboarding-process.md", "doc2"),
                        _file_item("03-deployment-guide.md", "doc3"),
                    ]
                },
                request=request,
            )
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler)
    output = service.search("governance onboarding", limit=10)
    assert "[Source: SharePoint: 01-content-boundaries-and-governance.md]" in output
    assert "[Source: SharePoint: 02-onboarding-process.md]" in output
    assert "03-deployment-guide.md" not in output
    # Only folder-scoped children listings happened - never a tenant-wide search.
    assert not any("/search" in p for p in seen)
    assert not any("sites?" in p for p in seen)


def test_search_no_match_returns_honest_message() -> None:
    service = _make_service(_library_handler([]))
    output = service.search("nonexistent-topic", limit=5)
    assert "No matching documents found" in output
    assert "sharepoint-rag-knowledge-base" in output


def test_search_multiple_results_and_source_attribution() -> None:
    service = _make_service(
        _library_handler(
            [
                _file_item("01-content-boundaries-and-governance.md", "doc1"),
                _file_item("06-security-and-responsible-ai.md", "doc6"),
            ]
        )
    )
    output = service.search("governance", limit=5)
    assert output.count("[Source: SharePoint:") == 2  # header + 1 item (only doc1 matches)
    assert "01-content-boundaries" in output
    assert "06-security" not in output


def test_search_empty_query_returns_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty query must not call the SharePoint API")

    service = _make_service(handler)
    assert service.search("   ") == "Please provide a non-empty search query."


def test_search_defaults_to_first_allowed_folder() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        if f"/drives/{DRIVE}/root:/{FOLDER}:/children" in path:
            return httpx.Response(200, json={"value": []}, request=request)
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler)
    assert "No matching documents" in service.search("x")


# -- content retrieval ----------------------------------------------------------


class _FakeParser:
    def parse_document(self, content: bytes, filename: str | None) -> str:
        return "Extracted text from the document."


def _item_handler(item: dict) -> callable:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        item_id = str(item.get("id") or "")
        if path.endswith(f"/drives/{DRIVE}/items/{item_id}"):
            return httpx.Response(200, json=item, request=request)
        if path.endswith(f"/drives/{DRIVE}/items/{item_id}/content"):
            return httpx.Response(200, content=b"document-bytes", request=request)
        raise AssertionError(f"unexpected path: {path}")

    return handler


def test_get_document_content_reads_document_inside_allowed_folder() -> None:
    item = _file_item("01-content-boundaries-and-governance.md", "doc1")
    service = _make_service(_item_handler(item), document_parser=_FakeParser())
    output = service.get_document_content("doc1", DRIVE)
    assert "[Source: SharePoint: 01-content-boundaries-and-governance.md]" in output
    assert "Extracted text from the document." in output
    assert "URL: https://knowledgegenagent.sharepoint.com" in output


def test_get_document_content_with_explicit_allowed_folder_ok() -> None:
    item = _file_item("docs.md", "doc9")
    service = _make_service(_item_handler(item), document_parser=_FakeParser())
    output = service.get_document_content("doc9", DRIVE, folder=FOLDER)
    assert "docs.md" in output


def test_get_document_content_rejects_document_outside_folder_allowlist() -> None:
    # The item is directly in the library root (not under the allowed folder).
    item = _file_item("secret.docx", "docroot", in_folder=False)
    service = _make_service(_item_handler(item), document_parser=_FakeParser())
    with pytest.raises(SharePointFolderNotAllowedError, match="inside any configured"):
        service.get_document_content("docroot", DRIVE)


def test_get_document_content_rejects_explicit_wrong_folder() -> None:
    item = _file_item("docs.md", "doc9")
    service = _make_service(_item_handler(item), document_parser=_FakeParser())
    with pytest.raises(SharePointFolderNotAllowedError, match="not in the configured allowlist"):
        service.get_document_content("doc9", DRIVE, folder="other-folder")


def test_get_document_content_explicit_folder_mismatch_rejected() -> None:
    # Item is in the allowed folder but caller demands a *different* allowed
    # folder: the item-folder gate must still reject the read.
    item = _file_item("a.md", "doc1")
    service = _make_service(
        _item_handler(item),
        allowed_folders=[FOLDER, "other-folder"],
        document_parser=_FakeParser(),
    )
    with pytest.raises(SharePointFolderNotAllowedError, match="not inside the allowed"):
        service.get_document_content("doc1", DRIVE, folder="other-folder")


def test_get_document_content_missing_ids_rejected() -> None:
    service = _make_service(_library_handler([]))
    with pytest.raises(SharePointApiError, match="drive id and a document id"):
        service.get_document_content("", "")
    with pytest.raises(SharePointApiError, match="drive id and a document id"):
        service.get_document_content("doc1", "")


def test_get_document_content_surfaces_http_404() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith("/items/missing"):
            return httpx.Response(
                404, json={"error": {"message": "not found"}}, request=request
            )
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler, document_parser=_FakeParser())
    with pytest.raises(SharePointApiError, match="HTTP 404"):
        service.get_document_content("missing", DRIVE)


def test_get_document_content_large_file_not_downloaded() -> None:
    big = _file_item("big.pptx", "big")
    big["size"] = 30 * 1024 * 1024

    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith("/items/big"):
            return httpx.Response(200, json=big, request=request)
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler, document_parser=_FakeParser())
    output = service.get_document_content("big", DRIVE)
    assert "exceeds the" in output
    assert "content not retrieved" in output


def test_get_document_content_truncates_to_char_limit() -> None:
    item = _file_item("long.md", "d")

    class _LongParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "a" * 3000

    service = _make_service(_item_handler(item), document_parser=_LongParser(), content_char_limit=600)
    output = service.get_document_content("d", DRIVE)
    assert "a" * 597 in output
    assert "a" * 598 not in output
    assert "..." in output


def test_search_file_content_returns_extracted_text() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        if f"/drives/{DRIVE}/root:/{FOLDER}:/children" in path:
            return httpx.Response(
                200,
                json={
                    "value": [
                        _file_item("01-content-boundaries-and-governance.md", "doc1"),
                        _file_item("vacation.md", "doc2"),
                    ]
                },
                request=request,
            )
        if path.endswith("/items/doc1/content") or path.endswith("/items/doc2/content"):
            return httpx.Response(200, content=b"vacation policy content", request=request)
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler, document_parser=_FakeParser())
    output = service.search_file_content("vacation policy", limit=3)
    assert "vacation.md" in output
    assert "content of" in output


# -- error handling --------------------------------------------------------------


def test_graph_401_maps_to_auth_error() -> None:
    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "unauthorized"}}, request=request)

    service = _make_service(bad_handler)
    with pytest.raises(SharePointApiError, match="401"):
        service.list_drives()


def test_graph_403_maps_to_permission_error() -> None:
    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "forbidden"}}, request=request)

    service = _make_service(bad_handler)
    with pytest.raises(SharePointApiError, match="permission"):
        service.get_site()


def test_graph_404_maps_to_not_found_error() -> None:
    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "missing"}}, request=request)

    service = _make_service(bad_handler)
    with pytest.raises(SharePointApiError, match="HTTP 404"):
        service.get_site()


def test_graph_429_retries_then_fails() -> None:
    calls: list[str] = []

    def bad_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, json={"error": {"message": "rate limited"}}, request=request)

    service = _make_service(bad_handler, retry_backoff_seconds=0.01)
    with pytest.raises(SharePointApiError, match="429"):
        service.get_site()
    assert len(calls) == 3


def test_graph_500_maps_to_server_error() -> None:
    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "generalException"}}, request=request)

    service = _make_service(bad_handler, retry_backoff_seconds=0)
    with pytest.raises(SharePointApiError, match="HTTP 500"):
        service.list_drives()


def test_graph_timeout_maps_to_controlled_error() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out connecting", request=request)

    service = _make_service(timeout_handler)
    with pytest.raises(SharePointApiError, match="timed out"):
        service.get_site()


def test_no_document_libraries_raises() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json={"value": []}, request=request)
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler)
    with pytest.raises(SharePointApiError, match="No document libraries"):
        service.search("query")


# -- no broader scope ------------------------------------------------------------


def test_no_tenant_wide_site_discovery_used() -> None:
    seen: list[str] = []

    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        assert not (
            "/sites/" in path
            and not path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent")
            and not path.endswith(f"/sites/{SITE}/drives")
        ), f"tenant-wide /sites access attempted: {path}"
        if path.endswith(f"/sites/{SITE_HOST}:/sites/KnowledgeGenAgent"):
            return httpx.Response(200, json=SITE_PAYLOAD, request=request)
        if path.endswith(f"/sites/{SITE}/drives"):
            return httpx.Response(200, json=_drives_payload(), request=request)
        if f"/drives/{DRIVE}/root:/{FOLDER}:/children" in path:
            return httpx.Response(200, json={"value": []}, request=request)
        raise AssertionError(f"unexpected path: {path}")

    service = _make_service(graph_handler)
    service.search("governance")
    assert len(seen) == 3  # site, drives, children - nothing tenant-wide


# -- close ----------------------------------------------------------------------


def test_close_does_not_raise() -> None:
    service = _make_service(_library_handler([]))
    service.close()
    service.close()


# -- tenant-wide mode ------------------------------------------------


def _make_tenant_wide_service(
    graph_handler,
    *,
    tenant_wide: bool = True,
    client_secret: str = "secret",
    search_region: str = "IND",
    **kwargs,
) -> SharePointService:
    token_client = httpx.Client(
        base_url=TOKEN_HOST,
        transport=httpx.MockTransport(_token_handler),
    )
    graph_client = httpx.Client(
        base_url=GRAPH_HOST,
        transport=httpx.MockTransport(graph_handler),
    )
    kwargs.setdefault("retry_backoff_seconds", 0)
    return SharePointService(
        tenant_id="tenant",
        client_id="client",
        client_secret=client_secret,
        graph_base_url=GRAPH_HOST,
        site_hostname="",
        site_relative_path="",
        allowed_sites=None,
        allowed_folders=None,
        tenant_wide=tenant_wide,
        search_region=search_region,
        token_client=token_client,
        graph_client=graph_client,
        **kwargs,
    )


def _search_query_payload(resource: dict) -> dict:
    return {
        "value": [
            {
                "searchTerms": ["governance"],
                "hitsContainers": [
                    {
                        "total": 1,
                        "hits": [{"hitId": "h1", "rank": 1, "resource": resource}],
                    }
                ],
            }
        ]
    }


SEARCH_HIT = {
    "id": "item1",
    "name": "01-content-boundaries-and-governance.md",
    "webUrl": f"https://{SITE_HOST}/sites/KnowledgeGenAgent/Shared Documents/{FOLDER}/01-content-boundaries-and-governance.md",
    "size": 1844,
    "createdDateTime": "2026-09-14T15:18:44Z",
    "lastModifiedDateTime": "2026-09-14T15:18:44Z",
    "mimeType": "text/markdown",
    "parentReference": {"driveId": DRIVE, "driveType": "documentLibrary"},
}


def test_tenant_wide_search_uses_search_query_api() -> None:
    seen: list[tuple[str, str]] = []

    def graph_handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/search/query"):
            return httpx.Response(200, json=_search_query_payload(SEARCH_HIT), request=request)
        raise AssertionError(f"unexpected path: {request.url.path}")

    service = _make_tenant_wide_service(graph_handler)
    output = service.search("governance", limit=5)
    assert ("POST", "/v1.0/search/query") in seen
    assert "01-content-boundaries-and-governance.md" in output
    assert "Drive id: b!doclib" in output
    assert "Document id: item1" in output


def test_tenant_wide_search_sends_region_with_application_permission() -> None:
    bodies: list[dict] = []

    def graph_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/query"):
            bodies.append(request.read().decode() if not isinstance(request.content, str) else request.content)
            return httpx.Response(200, json=_search_query_payload(SEARCH_HIT), request=request)
        raise AssertionError(f"unexpected path: {request.url.path}")

    service = _make_tenant_wide_service(graph_handler, search_region="IND")
    service.search("governance")
    assert bodies
    assert '"region":"IND"' in bodies[0]

    no_region = _make_tenant_wide_service(graph_handler, search_region="")
    no_region.search("governance")
    assert '"region"' not in bodies[-1]


def test_tenant_wide_search_does_not_require_allowlist() -> None:
    # No site allowlist / folder allowlist present, yet search works via Search API.
    def graph_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/query"):
            return httpx.Response(200, json=_search_query_payload(SEARCH_HIT), request=request)
        raise AssertionError(f"unexpected path: {request.url.path}")

    service = _make_tenant_wide_service(graph_handler)
    assert service.allowed_sites == []
    assert service.allowed_folders == []
    output = service.search("governance")
    assert "01-content-boundaries-and-governance.md" in output


def test_tenant_wide_blank_query_returns_controlled_message() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit the network")

    service = _make_tenant_wide_service(graph_handler)
    assert service.search("  ") == "Please provide a non-empty search query."


def test_tenant_wide_search_no_hits_returns_honest_message() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/query"):
            return httpx.Response(200, json={"value": []}, request=request)
        raise AssertionError(f"unexpected path: {request.url.path}")

    service = _make_tenant_wide_service(graph_handler)
    assert "No matching SharePoint documents found." in service.search("zzz")


def test_tenant_wide_search_relaxes_natural_language_question() -> None:
    """Regression: Graph ANDs every token, so the verbatim question
    'Explain the CI/CD pipeline flow for the Job Portal web application' found
    nothing although the 'Job_Portal_Web_Application CICD Pipeline flow.pdf'
    document exists. The search must fall back to distinctive keywords."""
    import json

    queries: list[str] = []
    hit = dict(SEARCH_HIT, name="Job_Portal_Web_Application CICD Pipeline flow.pdf")

    def graph_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/query"):
            body = json.loads(request.read().decode())
            q = body["requests"][0]["query"]["queryString"]
            queries.append(q)
            # Only a keyword-style query (no stopwords) matches, like real Graph.
            if "explain" in q.casefold() or " the " in f" {q.casefold()} ":
                return httpx.Response(200, json={"value": []}, request=request)
            return httpx.Response(200, json=_search_query_payload(hit), request=request)
        raise AssertionError(f"unexpected path: {request.url.path}")

    service = _make_tenant_wide_service(graph_handler)
    output = service.search("Explain the CI/CD pipeline flow for the Job Portal web application", limit=5)

    assert "Job_Portal_Web_Application CICD Pipeline flow.pdf" in output
    # Verbatim first, then a relaxed keyword variant.
    assert queries[0].startswith("Explain the CI/CD")
    assert len(queries) >= 2
    assert "explain" not in queries[1].casefold()
    assert "matched on" in output  # the response says which variant matched


def test_search_keyword_helpers_drop_stopwords_and_join_compounds() -> None:
    from app.services.sharepoint_service import _query_variants, _search_keywords

    assert _search_keywords("Explain the CI/CD pipeline flow for the Job Portal web application") == [
        "cicd", "pipeline", "flow", "job", "portal", "web", "application",
    ]
    variants = _query_variants("Explain the CI/CD pipeline flow for the Job Portal web application")
    assert variants[0].startswith("Explain the")
    assert "cicd pipeline flow job portal web application" in variants
    assert "cicd pipeline job portal" in variants  # generic words dropped


def test_tenant_wide_get_document_reads_by_drive_and_item_ids() -> None:
    item = _file_item("01-content-boundaries-and-governance.md", "item1")

    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/items/item1"):
            return httpx.Response(200, json=item, request=request)
        if path.endswith("/items/item1/content"):
            return httpx.Response(
                200, content=b"content boundaries governance policy", request=request
            )
        raise AssertionError(f"unexpected path: {path}")

    service = _make_tenant_wide_service(graph_handler, document_parser=_FakeParser())
    output = service.get_document_content("item1", DRIVE)
    assert "01-content-boundaries-and-governance.md" in output
    assert "Drive id: b!doclib" in output
    assert "Document id: item1" in output
    assert "Extracted text from the document." in output


def test_tenant_wide_get_document_no_drive_gating() -> None:
    # In tenant-wide mode a drive/item pair that is NOT inside any configured
    # folder is still readable (no site/folder resolution happens).
    item = _file_item("anywhere.md", "item9", in_folder=False)

    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/items/item9"):
            return httpx.Response(200, json=item, request=request)
        if path.endswith("/items/item9/content"):
            return httpx.Response(200, content=b"some content", request=request)
        raise AssertionError(f"unexpected path: {path}")

    service = _make_tenant_wide_service(graph_handler, document_parser=_FakeParser())
    output = service.get_document_content("item9", DRIVE)
    assert "anywhere.md" in output


def test_tenant_wide_list_files_lists_accessible_sites() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sites"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "s1",
                            "displayName": "Site One",
                            "name": "siteone",
                            "webUrl": "https://x.sharepoint.com/sites/siteone",
                        }
                    ]
                },
                request=request,
            )
        raise AssertionError(f"unexpected path: {path}")

    service = _make_tenant_wide_service(graph_handler)
    output = service.list_files(limit=5)
    assert "Site One" in output
    assert "s1" in output


def test_tenant_wide_list_allowed_sites_explains_mode() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit the network")

    service = _make_tenant_wide_service(graph_handler)
    output = service.list_allowed_sites()
    assert "tenant-wide" in output
    assert "SHAREPOINT_TENANT_WIDE" in output


def test_tenant_wide_get_site_lists_accessible_sites() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sites"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "s1",
                            "displayName": "Site One",
                            "name": "siteone",
                            "webUrl": "https://x.sharepoint.com/sites/siteone",
                        }
                    ]
                },
                request=request,
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    service = _make_tenant_wide_service(graph_handler)
    output = service.get_site()
    assert "Site One" in output
    assert "s1" in output


def test_tenant_wide_search_content_downloads_hits() -> None:
    def graph_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search/query"):
            return httpx.Response(200, json=_search_query_payload(SEARCH_HIT), request=request)
        if path.endswith("/items/item1/content"):
            return httpx.Response(
                200, content=b"content boundaries governance policy", request=request
            )
        raise AssertionError(f"unexpected path: {path}")

    service = _make_tenant_wide_service(graph_handler, document_parser=_FakeParser())
    output = service.search_file_content("governance", limit=5)
    assert "01-content-boundaries-and-governance.md" in output
    assert "content of 1 match(es))" in output
    assert "Extracted text from the document." in output


def test_tenant_wide_property_reflects_flag() -> None:
    assert _make_tenant_wide_service(lambda r: httpx.Response(404)).tenant_wide is True
    assert _make_tenant_wide_service(lambda r: httpx.Response(404), tenant_wide=False).tenant_wide is False
