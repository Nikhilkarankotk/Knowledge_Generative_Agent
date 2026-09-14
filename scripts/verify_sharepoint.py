"""Live verification for the SharePoint integration (Phase 3).

Exercises the real production wiring against Microsoft Graph without the database
or HTTP server: ``Settings`` -> ``SharePointService`` (OAuth 2.0 client-credentials
flow against ``login.microsoftonline.com``, app-only read) and optionally the
``SharePointPlugin`` through the Semantic Kernel runtime.

Scoped mode (SHAREPOINT_TENANT_WIDE=false) checks:

1. Configuration -> the service is enabled and the configured site + knowledge-base
   folder scope is loaded from ``SHAREPOINT_ALLOWED_SITES`` /
   ``SHAREPOINT_ALLOWED_FOLDERS``.
2. ``list_allowed_sites`` -> shows the configured scope (no network call).
3. ``get_site`` -> resolves the configured site (``SHAREPOINT_SITE_HOSTNAME`` +
   ``SHAREPOINT_SITE_RELATIVE_PATH``) from Microsoft Graph and validates it against
   the allowlist (proves the token flow + the read permission grant).
4. ``list_drives`` -> lists the site's document libraries.
5. ``list_files`` -> lists files inside the configured knowledge-base folder.
6. ``search`` -> folder-scoped filename search (no tenant-wide search).
7. ``search_file_content`` -> content of the matching documents.
8. ``get_document_content`` -> single document content download/extraction.

Tenant-wide mode (SHAREPOINT_TENANT_WIDE=true) checks:

1. Configuration -> the service is enabled in tenant-wide mode (allowlists are not
   enforced).
2. ``get_site``/``list_drives``/``list_files`` -> enumerate the SharePoint sites the
   application's Microsoft Graph permission can access (``GET /sites``; requires a
   broad read permission like ``Sites.Read.All`` - under ``Sites.Selected`` alone
   this returns no sites / HTTP 403, which is reported honestly).
3. ``search`` -> tenant-wide search via the Microsoft Graph Search JSON API
   (``POST /search/query``; also requires ``Sites.Read.All`` / ``Files.Read.All``).
4. ``search_file_content`` / ``get_document_content`` -> content of matches.

The retrieval turns are best-effort and report HTTP/Graph errors honestly rather
than failing the script. The agent turn (SharePointPlugin through
``SemanticKernelFactory``) is a single question that must invoke
``search_sharepoint`` automatically; it is skipped when ``SK_AGENT_ENABLED=false``
or Mistral is not configured.

Without ``SHAREPOINT_ENABLED=true`` plus tenant/client credentials (and, in scoped
mode, an allowlist), the script prints a skip message and exits (returns 0).

Run (from the repository root, using the project venv):

    .venv\\Scripts\\python.exe scripts\\verify_sharepoint.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.core.config import Settings  # noqa: E402
from app.core.exceptions import SharePointApiError  # noqa: E402
from app.services.sharepoint_service import SharePointService  # noqa: E402
from app.sk.semantic_kernel_factory import SemanticKernelFactory  # noqa: E402


def _section(title: str) -> None:
    print(f"\n===== {title} =====")


def _first_known_document(service: SharePointService) -> tuple[str, str]:
    """Find a real document id in the knowledge-base folder, if any."""
    out = service.search("content boundaries governance", limit=3)
    print(out[:1200])
    drive_id = ""
    document_id = ""
    for line in out.splitlines():
        if line.startswith("Drive id: "):
            drive_id = line.split(":", 1)[-1].strip()
        if line.startswith("Document id: "):
            document_id = line.split(":", 1)[-1].strip()
    return drive_id, document_id


def main() -> None:
    settings = Settings()
    if not settings.sharepoint_enabled:
        print(
            "SHAREPOINT_ENABLED=false - SharePoint is disabled in .env. "
            "Set it to true to run the Phase 3 verification."
        )
        return
    if not settings.sharepoint_tenant_id:
        print("SHAREPOINT_TENANT_ID is missing - .env not configured.")
        return
    if not settings.sharepoint_client_id:
        print("SHAREPOINT_CLIENT_ID is missing - .env not configured.")
        return
    if not settings.sharepoint_client_secret:
        print(
            "SHAREPOINT_CLIENT_SECRET is empty - the app-only client secret has not "
            "been added to .env yet, so the Microsoft Graph flow cannot run. Once the "
            "secret is set, re-run this script for the live verification."
        )
        return
    if not settings.sharepoint_tenant_wide and not settings.sharepoint_allowed_sites_list:
        print(
            "SHAREPOINT_ALLOWED_SITES is empty - SharePoint access is disabled "
            "(the agent answers 'No SharePoint sites are configured'). Add site IDs "
            "to .env (or enable SHAREPOINT_TENANT_WIDE=true) to run the Phase 3 "
            "verification."
        )
        return
    if not settings.sharepoint_tenant_wide and not settings.sharepoint_allowed_folders_list:
        print(
            "SHAREPOINT_ALLOWED_FOLDERS is empty - no knowledge-base folder is "
            "configured, so SharePoint retrieval is disabled. Add a folder path "
            "(e.g. sharepoint-rag-knowledge-base) to .env (or enable "
            "SHAREPOINT_TENANT_WIDE=true) to run the Phase 3 verification."
        )
        return

    service = SharePointService.from_settings(settings)
    if service is None or not service.enabled:
        print("SharePoint service is disabled - nothing to verify.")
        return

    failed = False

    def check(name: str, output: str) -> None:
        nonlocal failed
        print(output[:2200])
        lowered = output.lower()
        if "unavailable" in lowered or "could not" in lowered or "error" in lowered:
            print(f">> Note: {name} did not fully succeed (see output above). <<")

    mode = "tenant-wide" if service.tenant_wide else "scoped"
    try:
        _section("1. Configuration")
        print(f"enabled={service.enabled} mode={mode} graph_base_url={service._graph_base_url}")
        print(f"site_hostname={service._site_hostname}")
        print(f"site_relative_path={service._site_relative_path}")
        print(f"allowed_sites={service.allowed_sites}")
        print(f"allowed_folders={service.allowed_folders}")

        _section("2. list_allowed_sites (no network)")
        print(service.list_allowed_sites())

        _section("3. get_site - resolved sites from Microsoft Graph")
        check("get_site", service.get_site())

        _section("4. list_drives - document libraries")
        check("list_drives", service.list_drives())

        _section("5. list_files - site / knowledge-base folder listing")
        check("list_files", service.list_files(limit=20))

        _section("6. search - filename search (best effort)")
        try:
            check("search", service.search("governance", limit=5))
        except SharePointApiError as exc:
            failed = True
            print(f">> search failed: {exc}")

        _section("7. search_file_content - matching content (best effort)")
        try:
            check(
                "search_file_content",
                service.search_file_content("content boundaries", limit=3),
            )
        except SharePointApiError as exc:
            print(f">> search_file_content failed: {exc}")

        _section("8. get_document_content - single document (best effort)")
        try:
            drive_id, document_id = _first_known_document(service)
            if drive_id and document_id:
                check(
                    "get_document_content",
                    service.get_document_content(document_id, drive_id),
                )
            else:
                print("No document id available to fetch - skipping get_document_content.")
        except SharePointApiError as exc:
            print(f">> get_document_content failed: {exc}")

        _section("9. Agent turn (SharePointPlugin routing)")
        try:
            _run_agent_turn(settings, service)
        except Exception as exc:  # noqa: BLE001
            print(f">> agent turn failed to run: {exc}")

        print("\n===== Done =====")
        if failed and mode == "tenant-wide":
            print(
                "Some tenant-wide live checks could not complete (see notes above). "
                "Tenant-wide search/site enumeration needs the Entra app to hold a "
                "broad read permission (Sites.Read.All / Files.Read.All) granted with "
                "admin consent - Sites.Selected alone does not authorize "
                "POST /search/query or GET /sites. Document reads by drive/item id "
                "still work for any drive the token can read."
            )
        elif failed:
            print(
                "Some live checks could not complete (see notes above). This is usually "
                "a Graph permission/consent or site-content issue, not a code error."
            )
    finally:
        service.close()


def _run_agent_turn(settings: Settings, sharepoint: SharePointService) -> None:
    if not settings.sk_agent_enabled:
        print("SK_AGENT_ENABLED=false - skipping the agent turn.")
        return
    if not settings.mistral_api_key:
        print("MISTRAL_API_KEY missing - skipping the agent turn.")
        return
    from semantic_kernel.contents import ChatHistory

    factory = SemanticKernelFactory(
        settings,
        sharepoint_service=sharepoint,
        use_loop=True,
    )
    try:
        agent = factory.build_agent(session_id="verify-sharepoint", rag_service=None)
        history = ChatHistory()
        history.add_user_message(
            "Where is the content boundaries and governance document stored in "
            "SharePoint? Search the SharePoint knowledge and report the document "
            "you find."
        )
        answer = factory.run_agent(agent, history)
        print("\nAgent answer (first 2500 chars):\n")
        print(answer[:2500])
        if "[Source: SharePoint:" in answer:
            print("\n>> SharePoint sources were retrieved and attributed <<")
        elif "not configured" in answer.lower():
            print("\n>> SharePoint markers indicate the plugin is disabled <<")
        else:
            print("\n>> Review output below (no SharePoint sources surfaced) <<")
    finally:
        factory.close()


if __name__ == "__main__":
    main()
