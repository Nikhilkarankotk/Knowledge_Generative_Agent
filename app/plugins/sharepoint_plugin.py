"""SharePointPlugin - exposes read-only SharePoint knowledge to the agent.

Delegates to :class:`~app.services.sharepoint_service.SharePointService`, which owns
Microsoft Graph authentication, the ``SHAREPOINT_ALLOWED_SITES`` site allowlist, the
``SHAREPOINT_ALLOWED_FOLDERS`` knowledge-base folder allowlist and result
formatting. The plugin only ever reads the configured SharePoint site and its
configured knowledge-base folder; there is no tenant-wide site discovery, no
arbitrary site/drive/folder ids and no raw Graph functions. The plugin never
raises an unexpected error into the auto-invocation loop: SharePoint failures
(including allowlist rejections) are translated into honest, non-fabricated
markers so the agent can answer from the sources that are still available.

The plugin exposes no ``folder``/``site_id`` arguments: the knowledge-base folder
is a deployment setting enforced inside the service, so the LLM cannot steer
retrieval toward any other folder or site.
"""

from __future__ import annotations

import logging

from semantic_kernel.functions import kernel_function

from app.core.exceptions import SharePointApiError
from app.export.capture import ExportItemDraft, RetrievalCapture, parse_sharepoint_items
from app.export.formats import SourceType
from app.services.sharepoint_service import SharePointService

# The @kernel_function decorator below runs signature introspection at class-definition
# time, which crashes on Python 3.14 unless the compatibility patch is applied first.
from app.sk.compat import apply_py314_compatibility_patch

apply_py314_compatibility_patch()

logger = logging.getLogger(__name__)

LIST_SHAREPOINT_DOCUMENTS_DESCRIPTION = (
    "List the SharePoint knowledge available to this agent, with metadata (name, "
    "size, created and modified date, mime type, URL, site ids). In tenant-wide "
    "mode (SHAREPOINT_TENANT_WIDE=true) this lists the SharePoint sites the "
    "application can access; otherwise it lists the files and folders in the "
    "configured knowledge-base folder (inside the configured site's Documents "
    "library), with drive and item ids and parent path. Use this to browse the "
    "available SharePoint knowledge before searching or reading documents. "
    "Pass a limit (default is up to 200 items). Only configured/content-restricted "
    "SharePoint is ever queried; no arbitrary site, drive or folder id is accepted."
)

SEARCH_SHAREPOINT_DESCRIPTION = (
    "Search the organization's approved SharePoint knowledge for enterprise "
    "documents, application documentation, architecture and system design "
    "documents, onboarding documentation, operational procedures, internal "
    "policies, deployment guides and other organizational knowledge. In tenant-wide "
    "mode (SHAREPOINT_TENANT_WIDE=true) the search covers every SharePoint site "
    "and document the application can access; otherwise it is restricted to the "
    "configured knowledge-base folder of the configured SharePoint site "
    "(SHAREPOINT_ALLOWED_SITES + SHAREPOINT_ALLOWED_FOLDERS). You cannot pass a "
    "folder or site. "
    "Pass a non-empty query for the content you are looking for (for example "
    "'content boundaries', 'governance', 'deployment', 'architecture'). "
    "SharePoint may contain information that is not present in the uploaded "
    "documents, Confluence or GitHub. If the question concerns enterprise or "
    "organizational documents, invoke this function automatically without asking "
    "the user for permission."
)

SEARCH_SHAREPOINT_CONTENT_DESCRIPTION = (
    "Search the organization's approved SharePoint knowledge AND return the "
    "full extracted text content of the matching documents in one call. Use this "
    "when you need the actual content of documents (for example to answer a "
    "detailed question about a policy or process) rather than just browsing file "
    "names. Pass a non-empty query. The scope is the same as search_sharepoint "
    "(tenant-wide when SHAREPOINT_TENANT_WIDE=true, otherwise the configured "
    "knowledge-base folder of the configured SharePoint site); no other folder or "
    "site is ever queried."
)

GET_SHAREPOINT_DOCUMENT_DESCRIPTION = (
    "Retrieve the full content of a single SharePoint document by its drive id and "
    "document id (both are returned in the results of search_sharepoint or "
    "list_sharepoint_documents). The file is downloaded and its content is extracted "
    "for common enterprise document types (PDF, DOCX, XLSX, PPTX, TXT, MD, CSV, "
    "JSON, HTML). The document access is limited to the configured SharePoint scope "
    "(the configured knowledge-base folder, or any document the application can "
    "access in tenant-wide mode); reading outside it is refused. Use this when you "
    "have identified the correct document and need to read its content."
)


class SharePointPlugin:
    """Read-only SharePoint knowledge limited to the configured site + folder."""

    def __init__(
        self,
        sharepoint_service: SharePointService | None,
        capture: RetrievalCapture | None = None,
    ) -> None:
        self._service = sharepoint_service
        self._capture = capture

    @kernel_function(
        description=LIST_SHAREPOINT_DOCUMENTS_DESCRIPTION,
        name="list_sharepoint_documents",
    )
    def list_sharepoint_documents(self, limit: int | None = None) -> str:
        if not self._enabled:
            logger.info(
                "SharePoint list_sharepoint_documents not invoked: "
                "SharePoint not configured"
            )
            return "SharePoint access is not configured for this deployment."
        logger.info(
            "SharePoint list_sharepoint_documents invoked: limit=%r",
            limit,
        )
        try:
            result = self._service.list_files(limit=limit)  # type: ignore[union-attr]
            _capture_sharepoint_results(self._capture, result, exportable=False)
            logger.info("SharePoint list_sharepoint_documents completed")
            return result
        except SharePointApiError as exc:
            logger.warning(
                "SharePoint list_sharepoint_documents failed: %s", exc
            )
            return f"Could not list SharePoint documents: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception(
                "Unexpected SharePoint list_sharepoint_documents failure"
            )
            return "Could not list SharePoint documents."

    @kernel_function(
        description=SEARCH_SHAREPOINT_DESCRIPTION,
        name="search_sharepoint",
    )
    def search_sharepoint(
        self,
        query: str,
        limit: int | None = None,
    ) -> str:
        if not self._enabled:
            logger.info(
                "SharePoint search_sharepoint not invoked: SharePoint not configured"
            )
            return "SharePoint access is not configured for this deployment."
        logger.info(
            "SharePoint search_sharepoint invoked: query=%r limit=%r",
            query,
            limit,
        )
        try:
            result = self._service.search(  # type: ignore[union-attr]
                query=query,
                limit=limit,
            )
            _capture_sharepoint_results(self._capture, result, exportable=False)
            logger.info(
                "SharePoint search_sharepoint completed: %d results returned",
                _count_sources(result),
            )
            return result
        except SharePointApiError as exc:
            logger.warning("SharePoint search failed: %s", exc)
            return f"SharePoint search is currently unavailable: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected SharePoint search failure")
            return "SharePoint search is currently unavailable."

    @kernel_function(
        description=SEARCH_SHAREPOINT_CONTENT_DESCRIPTION,
        name="search_sharepoint_content",
    )
    def search_sharepoint_content(
        self,
        query: str,
        limit: int | None = None,
    ) -> str:
        if not self._enabled:
            logger.info(
                "SharePoint search_sharepoint_content not invoked: "
                "SharePoint not configured"
            )
            return "SharePoint access is not configured for this deployment."
        logger.info(
            "SharePoint search_sharepoint_content invoked: query=%r limit=%r",
            query,
            limit,
        )
        try:
            result = self._service.search_file_content(  # type: ignore[union-attr]
                query=query,
                limit=limit,
            )
            # Every hit's content is returned to the model, but a hit is only a
            # *candidate* for export: the end-of-turn promotion keeps just the
            # documents the answer is actually about (see RetrievalCapture).
            _capture_sharepoint_results(self._capture, result, exportable=False)
            logger.info(
                "SharePoint search_sharepoint_content completed: %d sources returned",
                _count_sources(result),
            )
            return result
        except SharePointApiError as exc:
            logger.warning("SharePoint search content failed: %s", exc)
            return f"SharePoint content search is currently unavailable: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected SharePoint search content failure")
            return "SharePoint content search is currently unavailable."

    @kernel_function(
        description=GET_SHAREPOINT_DOCUMENT_DESCRIPTION,
        name="get_sharepoint_document",
    )
    def get_sharepoint_document(
        self,
        document_id: str,
        drive_id: str,
    ) -> str:
        if not self._enabled:
            logger.info(
                "SharePoint get_sharepoint_document not invoked: "
                "SharePoint not configured"
            )
            return "SharePoint access is not configured for this deployment."
        logger.info(
            "SharePoint get_sharepoint_document invoked: document_id=%r drive_id=%r",
            document_id,
            drive_id,
        )
        try:
            result = self._service.get_document_content(  # type: ignore[union-attr]
                document_id=document_id,
                drive_id=drive_id,
            )
            # The agent deliberately opened this document: it IS a source.
            _capture_sharepoint_results(self._capture, result, exportable=True)
            logger.info("SharePoint get_sharepoint_document completed")
            return result
        except SharePointApiError as exc:
            logger.warning(
                "SharePoint get_document(%s) failed: %s", document_id, exc
            )
            return f"Could not retrieve SharePoint document {document_id}: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected SharePoint get_document failure")
            return f"Could not retrieve SharePoint document {document_id}."

    @property
    def _enabled(self) -> bool:
        return self._service is not None and self._service.enabled


def _capture_sharepoint_results(
    capture: RetrievalCapture | None, result: str, *, exportable: bool = True
) -> None:
    """Persist each returned SharePoint document into the turn's export capture.

    ``exportable=False`` records the document as a *candidate* (found by a
    search/listing); it becomes exportable only if the agent reads it with
    ``get_sharepoint_document`` or the end-of-turn promotion finds the answer is
    about it. This keeps unrelated search hits out of the export.
    """
    if capture is None:
        return
    for item in parse_sharepoint_items(result):
        if str(item.get("type") or "").lower() == "folder":
            continue
        document_id = item.get("document_id") or item.get("name")
        if not document_id:
            continue
        metadata = {
            key: item[key]
            for key in (
                "drive_id",
                "site_id",
                "size",
                "modified",
                "created",
                "parent",
                "type",
                "owner",
                "last_editor",
            )
            if item.get(key) is not None
        }
        draft = ExportItemDraft(
            source_type=SourceType.SHAREPOINT.value,
            source_id=str(document_id),
            source_name=str(item.get("name") or document_id),
            filename=str(item.get("name") or "") or None,
            mime_type=item.get("mime_type"),
            source_url=item.get("url"),
            metadata=metadata or {},
            size_bytes=int(item["size"]) if isinstance(item.get("size"), (int, str)) and str(item.get("size")).isdigit() else None,
            exportable=exportable,
        )
        if item.get("content"):
            draft.merge_content(str(item["content"]))
        capture.add(draft)


def _count_sources(text: str) -> int:
    return text.count("[Source")
