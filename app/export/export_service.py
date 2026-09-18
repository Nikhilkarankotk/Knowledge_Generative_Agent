"""ExportService - central orchestration for source-aware exports.

The service runs entirely off persisted retrieval context:
:class:`app.repositories.export_context_repository.ExportContext` rows bound to
the assistant ``chat_message.id`` that was the source of truth for the answer.
All logic lives here - chat/rag/confluence/github/sharepoint services and the
Semantic Kernel plugins contain no export code.

Flow per export request:

1. load the context + items for the assistant message and verify the requested
   session owns them (session isolation) and the context is not expired;
2. resolve each item into native bytes or generated content
   (:mod:`app.export.sources`);
3. compute a deterministic intent proposal and - when enabled - an optional LLM
   recommendation, then *validate* the final intent against the sources and the
   live set of available exporters (:mod:`app.export.intent`);
4. execute the chosen scenario with the matched exporter (single file, generated
   document/report, or a sanitized ZIP + ``manifest.json`` with limits);
5. observe the outcome (ids, source types, scenario, formats, sizes, status) with
   no secrets in the log.

The service never accepts file paths, URLs or arbitrary extensions from the
client or the model.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import tempfile
import time
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from app.export import registry as exporter_registry
from app.export.errors import (
    ExportLimitError,
    ExportNotAllowedError,
    ExportNotFoundError,
    ExportValidationError,
)
from app.export.exporters.zip_writer import ZipEntry, write_zip_to_path
from app.export.formats import ScenarioType, SourceType, ensure_extension, safe_filename
from app.export.intent import (
    ExportIntent,
    IntentRecommender,
    propose_intent,
    validate_intent,
)
from app.export.registry import ExportPayload, ExportSection
from app.export.sources import (
    ExportLimits,
    ExportServices,
    ResolvedSource,
    resolve_sources,
)
from app.models import ExportContext, ExportContextItem
from app.repositories import ChatMessageRepository, ExportContextRepository

logger = logging.getLogger(__name__)

# Top-level folders of the session-wide export archive (Export.zip).
_SESSION_CHAT_DIR = "ChatHistory"
_SESSION_SOURCE_DIRS = {
    SourceType.CONFLUENCE.value: "Confluence",
    SourceType.SHAREPOINT.value: "SharePoint",
    SourceType.UPLOADED_DOCUMENT.value: "UploadedDocuments",
    SourceType.GITHUB.value: "GitHub",
}
_SESSION_METADATA_NAME = "metadata.json"

_ZIP_ROOT_DIRS = {
    SourceType.UPLOADED_DOCUMENT.value: "uploaded_documents",
    SourceType.CONFLUENCE.value: "confluence",
    SourceType.GITHUB.value: "github",
    SourceType.SHAREPOINT.value: "sharepoint",
}


@dataclass
class ExportArtifact:
    """Result of an export: either in-memory bytes or a temp file (ZIP)."""

    filename: str
    mime_type: str
    data: bytes | None = None
    path: str | None = None
    cleanup_dir: str | None = None
    scenario: str | None = None


class ExportService:
    def __init__(
        self,
        export_repo: ExportContextRepository,
        settings: Any,
        *,
        services: ExportServices | None = None,
        recommender: IntentRecommender | None = None,
        synthesizer: Any | None = None,
        limits: ExportLimits | None = None,
        chat_repo: ChatMessageRepository | None = None,
    ) -> None:
        self._export_repo = export_repo
        self._settings = settings
        self._services = services or ExportServices()
        self._recommender = recommender
        self._synthesizer = synthesizer
        self._limits = limits or ExportLimits.from_settings(settings)
        # Needed only by the session-wide export (query text + AI responses).
        self._chat_repo = chat_repo

    # -- context loading -----------------------------------------------------

    def load_context(
        self, chat_message_id: int, session_id: str
    ) -> tuple[ExportContext, list[ExportContextItem]]:
        """Load + authorize the export context for an assistant message."""
        context = self._export_repo.find_by_chat_message_id(chat_message_id)
        if context is None:
            raise ExportNotFoundError(
                f"No export context was recorded for chat message {chat_message_id}."
            )
        if context.session_id != session_id:
            raise ExportNotAllowedError(
                "This export belongs to a different chat session."
            )
        if context.expires_at is not None:
            from datetime import datetime

            now = datetime.now()
            ts = context.expires_at.replace(tzinfo=None)
            if ts < now:
                raise ExportNotFoundError(
                    "This chat message's export context has expired."
                )
        items = self._export_repo.find_items(context.id, export_only=True)
        if not items:
            raise ExportNotFoundError(
                f"No retrievable sources were recorded for chat message {chat_message_id}."
            )
        return context, items

    # -- main entry point ----------------------------------------------------

    def export(
        self,
        chat_message_id: int,
        session_id: str,
        *,
        assistant_text: str = "",
        user_hint: str = "",
    ) -> ExportArtifact:
        started = time.monotonic()
        context, items = self.load_context(chat_message_id, session_id)
        resolved = resolve_sources(
            items, session_id, services=self._services, limits=self._limits
        )
        if not resolved:
            raise ExportNotFoundError(
                f"No exportable sources could be resolved for chat message {chat_message_id}."
            )
        available = exporter_registry.available_formats()
        proposed = propose_intent(resolved, available)
        intended = proposed
        if self._recommender is not None and self._recommender.enabled:
            recommendation = self._recommender.recommend(resolved, available, user_hint)
            if recommendation is not None:
                try:
                    intended = validate_intent(recommendation, resolved, available)
                except ExportValidationError:
                    intended = proposed
        if proposed.type != intended.type or proposed.format != intended.format:
            logger.info(
                "export intent adjusted: proposed=(%s,%s) selected=(%s,%s)",
                proposed.type,
                proposed.format,
                intended.type,
                intended.format,
            )

        artifact = self._execute(intended, resolved, assistant_text=assistant_text)
        self._record_outcome(context, intended, resolved)
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "export completed: chat_message_id=%s session=%r scenario=%s format=%s "
            "sources=%s (%s) filename=%r size=%s duration_ms=%s",
            chat_message_id,
            session_id,
            intended.type,
            intended.format,
            len(resolved),
            _source_type_counts(resolved),
            artifact.filename,
            len(artifact.data) if artifact.data is not None else _path_size(artifact.path),
            duration_ms,
        )
        return artifact

    # -- sources-only export for ONE response --------------------------------------

    def find_latest_exportable_message(
        self, session_id: str, *, preferred_chat_message_id: int | None = None
    ) -> int | None:
        """Id of the most recent assistant response in ``session_id`` that has any
        exportable sources (Confluence, GitHub, SharePoint or uploaded documents).

        ``preferred_chat_message_id`` wins when it qualifies; otherwise the newest
        qualifying response is returned so "Export" still works after a page
        refresh even if the very last answer used no sources. ``None`` when
        nothing in the session is exportable.
        """
        candidates: list[int] = []
        for context in self._export_repo.find_by_session_id(session_id):
            if context.chat_message_id is None:
                continue
            if self._export_repo.find_items(context.id, export_only=True):
                candidates.append(context.chat_message_id)
        if preferred_chat_message_id is not None and preferred_chat_message_id in candidates:
            return preferred_chat_message_id
        return max(candidates) if candidates else None

    def export_response_sources(self, chat_message_id: int, session_id: str) -> ExportArtifact:
        """Export the source documents actually used for one AI response.

        This is what the chat "Export" button produces: a ZIP holding just the
        sources that were retrieved for - and used to generate - the selected /
        latest assistant response. Nothing else is included: no chat history, no
        earlier queries, no sources from other responses.

        The archive is organised by source system and named after what was used:

        * only Confluence pages            -> ``confluence.zip``
        * only SharePoint documents        -> ``sharepoint.zip``
        * only a GitHub repository         -> ``github.zip``
        * only uploaded documents          -> ``uploaded-documents.zip``
        * more than one source system      -> ``Knowledge Export.zip``

        Layout::

            Knowledge Export.zip
            ├── Confluence/<Page>.docx        (original retrieved page content)
            ├── GitHub/<owner-repo>/analysis.pdf + source/...
            ├── SharePoint/<file>             (original bytes)
            └── metadata.json                 (which sources backed this response)

        Each source appears once (retrieval chunks of the same page are merged).
        """
        started = time.monotonic()
        context, items = self.load_context(chat_message_id, session_id)
        resolved = resolve_sources(items, session_id, services=self._services, limits=self._limits)
        if not resolved:
            raise ExportNotFoundError(
                "The source documents for this response could not be resolved."
            )

        entries: list[ZipEntry] = []
        sources_meta: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        used_types: set[str] = set()
        for source in resolved:
            key = (source.source_type or "", source.source_id or source.source_name or "")
            if key in seen:
                continue  # never write the same source twice
            folder = _SESSION_SOURCE_DIRS.get(source.source_type or "", "Sources")
            new_entries = self._response_source_entries(source, folder)
            if not new_entries:
                continue
            seen.add(key)
            used_types.add(source.source_type or "")
            entries.extend(new_entries)
            sources_meta.append(
                {
                    "source_type": source.source_type,
                    "source_id": source.source_id,
                    "source_name": source.source_name,
                    "source_url": source.source_url,
                    "files": [entry.name for entry in new_entries],
                }
            )
        if not entries:
            raise ExportNotFoundError(
                "The source documents for this response had no exportable content."
            )
        archive_name = _response_archive_name(used_types)

        import json
        from datetime import datetime

        metadata = {
            "export_version": "2.0",
            "generated_by": "knowledge-generative-agent",
            "scope": "single_response_sources",
            "session_id": session_id,
            "chat_message_id": chat_message_id,
            "exported_at": datetime.now().isoformat(),
            "source_types": sorted(used_types),
            "source_count": len(sources_meta),
            "sources": sources_meta,
        }
        entries.append(
            ZipEntry(
                name=_SESSION_METADATA_NAME,
                data=json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8"),
                metadata={"source_type": "METADATA", "source_name": "Export metadata"},
            )
        )

        temp_dir = tempfile.mkdtemp(prefix="ask-pnc-sources-export-")
        try:
            archive_path = os.path.join(temp_dir, archive_name)
            write_zip_to_path(
                archive_path,
                entries,
                max_files=self._limits.max_files,
                max_total_bytes=self._limits.max_total_bytes,
                max_single_file_bytes=self._limits.max_single_file_bytes,
            )
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        context.strategy = ScenarioType.MULTI_ARTIFACT.value
        context.requested_format = "zip"
        context.format_reason = (
            "Source documents used for this response: " + ", ".join(sorted(used_types)) + "."
        )
        context.status = "completed"
        self._export_repo.save(context)

        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "response sources export completed: chat_message_id=%s session=%r "
            "sources=%s types=%s filename=%r duration_ms=%s",
            chat_message_id,
            session_id,
            len(sources_meta),
            ",".join(sorted(used_types)),
            archive_name,
            duration_ms,
        )
        return ExportArtifact(
            filename=archive_name,
            mime_type="application/zip",
            path=archive_path,
            cleanup_dir=temp_dir,
            scenario=ScenarioType.MULTI_ARTIFACT.value,
        )

    def _response_source_entries(self, source: ResolvedSource, folder: str) -> list[ZipEntry]:
        """Archive entries for one source under ``<Folder>/``.

        GitHub repositories expand to an ``analysis.<fmt>`` report plus the sampled
        ``source/`` files; everything else is a single document (native bytes are
        copied as-is, Confluence pages are rendered as DOCX from the original
        retrieved content).
        """
        if source.source_type == SourceType.GITHUB.value:
            metadata: dict[str, Any] = {
                "source_type": source.source_type,
                "source_name": source.source_name,
                "source_url": source.source_url,
                "retrieval_rank": (source.item.retrieval_rank if source.item else None),
            }
            return self._github_entries(source, folder, metadata)
        entry = self._session_source_entry(source, folder, label="")
        return [entry] if entry is not None else []

    # -- session-wide, query-organized export -----------------------------------

    def export_session(self, session_id: str) -> ExportArtifact:
        """Export the whole chat session as one organized ``Export.zip``.

        Layout::

            Export.zip
            ├── ChatHistory/Query1_Response.docx      (user query + AI answer)
            ├── Confluence/Query1/<Page>.docx         (only pages used by Query1)
            ├── Confluence/Query2/<Page>.docx
            └── metadata.json                         (query -> source mapping)

        Rules:
        * only the sources actually recorded for each assistant answer are
          exported under that query's folder;
        * a page that was used by several queries is written **once** (under the
          first query that used it) and referenced from later queries in
          ``metadata.json`` - never duplicated inside the archive;
        * the page content is the original retrieved Confluence page.
        """
        if self._chat_repo is None:
            raise ExportValidationError(
                "Session export requires a chat repository to read the conversation."
            )
        started = time.monotonic()
        contexts = self._export_repo.find_by_session_id(session_id)
        if not contexts:
            raise ExportNotFoundError(
                "No exportable retrieval context was recorded for this chat session."
            )

        entries: list[ZipEntry] = []
        queries_meta: list[dict[str, Any]] = []
        # (source_type, source_id) -> archive path of the first copy written.
        written_sources: dict[tuple[str, str], str] = {}
        query_index = 0

        for context in contexts:
            if context.chat_message_id is None:
                continue
            assistant = self._chat_repo.find_by_id(context.chat_message_id)
            if assistant is None or assistant.role != "assistant":
                continue
            items = self._export_repo.find_items(context.id, export_only=True)
            user_message = self._chat_repo.find_last_user_message(
                session_id, context.chat_message_id
            )
            user_query = (user_message.content if user_message else "") or ""
            query_index += 1
            label = f"Query{query_index}"

            # 1. ChatHistory/QueryN_Response.docx - the question and the answer.
            response_name = f"{_SESSION_CHAT_DIR}/{label}_Response.docx"
            entries.append(
                ZipEntry(
                    name=response_name,
                    data=self._render_query_response(label, user_query, assistant.content or ""),
                    metadata={
                        "source_type": "CHAT_RESPONSE",
                        "source_name": f"{label} response",
                        "retrieval_rank": query_index,
                    },
                )
            )

            # 2. <Source>/QueryN/<Page>.docx - only the sources used for this answer.
            sources_meta: list[dict[str, Any]] = []
            resolved = (
                resolve_sources(items, session_id, services=self._services, limits=self._limits)
                if items
                else []
            )
            for source in resolved:
                key = (source.source_type or "", source.source_id or source.source_name or "")
                folder = _SESSION_SOURCE_DIRS.get(source.source_type, "Sources")
                if key in written_sources:
                    # Already exported under an earlier query - reference, don't duplicate.
                    sources_meta.append(
                        self._session_source_meta(
                            source, file=written_sources[key], duplicate_of_query=True
                        )
                    )
                    continue
                entry = self._session_source_entry(source, folder, label)
                if entry is None:
                    continue
                written_sources[key] = entry.name
                entries.append(entry)
                sources_meta.append(
                    self._session_source_meta(source, file=entry.name, duplicate_of_query=False)
                )

            queries_meta.append(
                {
                    "index": query_index,
                    "label": label,
                    "chat_message_id": context.chat_message_id,
                    "asked_at": (
                        user_message.timestamp.isoformat()
                        if user_message is not None and user_message.timestamp
                        else None
                    ),
                    "answered_at": (
                        assistant.timestamp.isoformat() if assistant.timestamp else None
                    ),
                    "user_query": user_query,
                    "response_file": response_name,
                    "source_count": len(sources_meta),
                    "sources": sources_meta,
                }
            )

        if not queries_meta:
            raise ExportNotFoundError(
                "No exportable assistant responses were found for this chat session."
            )

        # 3. metadata.json - the authoritative query -> source-document mapping.
        from datetime import datetime
        import json

        metadata = {
            "export_version": "2.0",
            "generated_by": "knowledge-generative-agent",
            "session_id": session_id,
            "exported_at": datetime.now().isoformat(),
            "query_count": len(queries_meta),
            "unique_source_count": len(written_sources),
            "layout": {
                "chat_history": f"{_SESSION_CHAT_DIR}/",
                "sources": {k: f"{v}/" for k, v in _SESSION_SOURCE_DIRS.items()},
            },
            "queries": queries_meta,
        }
        entries.append(
            ZipEntry(
                name=_SESSION_METADATA_NAME,
                data=json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8"),
                metadata={"source_type": "METADATA", "source_name": "Export metadata"},
            )
        )

        temp_dir = tempfile.mkdtemp(prefix="ask-pnc-session-export-")
        try:
            archive_path = os.path.join(temp_dir, "Export.zip")
            write_zip_to_path(
                archive_path,
                entries,
                max_files=self._limits.max_files,
                max_total_bytes=self._limits.max_total_bytes,
                max_single_file_bytes=self._limits.max_single_file_bytes,
            )
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "session export completed: session=%r queries=%s unique_sources=%s "
            "files=%s duration_ms=%s",
            session_id,
            len(queries_meta),
            len(written_sources),
            len(entries),
            duration_ms,
        )
        return ExportArtifact(
            filename="Export.zip",
            mime_type="application/zip",
            path=archive_path,
            cleanup_dir=temp_dir,
            scenario=ScenarioType.MULTI_ARTIFACT.value,
        )

    def _render_query_response(self, label: str, user_query: str, answer: str) -> bytes:
        """Render ``ChatHistory/QueryN_Response.docx`` (question + AI answer)."""
        payload = ExportPayload(
            title=f"{label} - AI Response",
            subtitle="Knowledge Generative Agent chat export",
            sections=[
                ExportSection(heading="User Query", body=(user_query or "").strip() or "(not recorded)"),
                ExportSection(heading="AI Response", body=(answer or "").strip() or "(empty response)"),
            ],
        )
        return self._render("docx", payload)

    def _session_source_entry(
        self, source: ResolvedSource, folder: str, label: str
    ) -> ZipEntry | None:
        """One archive entry for a source, under ``<Folder>/<QueryN>/``.

        ``label`` may be empty, in which case the file is placed directly under
        ``<Folder>/``. Native files (uploads, SharePoint originals) are copied
        byte-for-byte to preserve the original document; text sources (Confluence
        pages) are rendered as DOCX from the original retrieved page content.
        """
        base = f"{folder}/{label}" if label else folder
        metadata: dict[str, Any] = {
            "source_type": source.source_type,
            "source_name": source.source_name,
            "source_url": source.source_url,
            "mime_type": source.mime_type,
            "native_format": source.native_format,
            "retrieval_rank": (source.item.retrieval_rank if source.item else None),
        }
        if source.has_native_bytes and source.filename:
            return ZipEntry(
                name=f"{base}/{safe_filename(source.filename)}",
                data=source.native_bytes or b"",
                metadata=metadata,
            )
        if source.source_type == SourceType.GITHUB.value:
            entries = self._github_entries(source, base, metadata)
            return entries[0] if entries else None
        if not (source.content or "").strip():
            return None
        payload = ExportPayload(
            title=source.source_name or source.source_id,
            source_url=source.source_url,
            source_name=source.source_name,
            source_type=source.source_type,
            content=source.content,
            metadata={"source_id": source.source_id},
        )
        data = self._render("docx", payload)
        stem = safe_filename(source.source_name or source.source_id, default="page")
        return ZipEntry(
            name=f"{base}/{ensure_extension(stem, 'docx')}",
            data=data,
            metadata=metadata,
        )

    @staticmethod
    def _session_source_meta(
        source: ResolvedSource, *, file: str, duplicate_of_query: bool
    ) -> dict[str, Any]:
        return {
            "source_type": source.source_type,
            "source_id": source.source_id,
            "source_name": source.source_name,
            "source_url": source.source_url,
            "file": file,
            # True when this page was already exported under an earlier query;
            # the file above points at that single copy.
            "referenced_from_earlier_query": duplicate_of_query,
        }

    # -- scenario execution ---------------------------------------------------

    def _execute(
        self,
        intent: ExportIntent,
        resolved: list[ResolvedSource],
        *,
        assistant_text: str,
    ) -> ExportArtifact:
        scenario = ScenarioType(intent.type)
        if scenario == ScenarioType.NATIVE_FILE:
            return self._exec_native(intent, resolved)
        if scenario == ScenarioType.GENERATED_DOCUMENT:
            return self._exec_generated_document(intent, resolved)
        if scenario == ScenarioType.GENERATED_REPORT:
            return self._exec_generated_report(intent, resolved)
        return self._exec_multi(intent, resolved, assistant_text=assistant_text)

    def _exec_native(self, intent: ExportIntent, resolved: list[ResolvedSource]) -> ExportArtifact:
        source = resolved[0]
        if not source.has_native_bytes:
            raise ExportValidationError("Native export selected but no native bytes are available.")
        filename = source.filename or source.source_name or f"export.{intent.format}"
        filename = ensure_extension(safe_filename(filename), intent.format)
        mime = source.mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return ExportArtifact(
            filename=filename,
            mime_type=mime,
            data=source.native_bytes,
            scenario=ScenarioType.NATIVE_FILE.value,
        )

    def _exec_generated_document(self, intent: ExportIntent, resolved: list[ResolvedSource]) -> ExportArtifact:
        source = resolved[0]
        payload = self._document_payload(source, intent.format)
        self._prepend_synthesis(payload, kind="document")
        data = self._render(intent.format, payload)
        _ensure_generated_size(intent.format, data, self._limits)
        filename = self._generated_filename(source, intent.format, mode="document")
        return ExportArtifact(
            filename=filename,
            mime_type=_mime_for(intent.format),
            data=data,
            scenario=ScenarioType.GENERATED_DOCUMENT.value,
        )

    def _exec_generated_report(self, intent: ExportIntent, resolved: list[ResolvedSource]) -> ExportArtifact:
        source = resolved[0]
        payload = source.analysis or self._captured_payload(source, intent.format)
        if not (intent.format == "pdf" and payload.architecture is not None):
            self._prepend_synthesis(payload, kind="report")
        data = self._render(intent.format, payload)
        _ensure_generated_size(intent.format, data, self._limits)
        filename = self._generated_filename(source, intent.format, mode="report")
        return ExportArtifact(
            filename=filename,
            mime_type=_mime_for(intent.format),
            data=data,
            scenario=ScenarioType.GENERATED_REPORT.value,
        )

    def _exec_multi(
        self, intent: ExportIntent, resolved: list[ResolvedSource], *, assistant_text: str
    ) -> ExportArtifact:
        if len(resolved) < 2:
            raise ExportValidationError(
                "ZIP export requires at least two resolved sources."
            )

        entries: list[ZipEntry] = []
        for source in resolved:
            entries.extend(self._source_entries(source))
        if self._settings.export_include_summary and assistant_text.strip():
            entries.append(
                ZipEntry(
                    name="export-summary.md",
                    data=(assistant_text.strip() + "\n").encode("utf-8"),
                    metadata={"source_type": "ASSISTANT_SUMMARY", "source_name": "Assistant summary"},
                )
            )
        temp_dir = tempfile.mkdtemp(prefix="ask-pnc-export-")
        try:
            archive_path = os.path.join(temp_dir, "knowledge-export.zip")
            write_zip_to_path(
                archive_path,
                entries,
                max_files=self._limits.max_files,
                max_total_bytes=self._limits.max_total_bytes,
                max_single_file_bytes=self._limits.max_single_file_bytes,
            )
            filename = self._zip_filename(resolved)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        return ExportArtifact(
            filename=filename,
            mime_type="application/zip",
            path=archive_path,
            cleanup_dir=temp_dir,
            scenario=ScenarioType.MULTI_ARTIFACT.value,
        )

    # -- artifact helpers ------------------------------------------------------

    def _source_entries(self, source: ResolvedSource) -> list[ZipEntry]:
        root = _ZIP_ROOT_DIRS.get(source.source_type, "sources")
        metadata: dict[str, Any] = {
            "source_type": source.source_type,
            "source_name": source.source_name,
            "source_url": source.source_url,
            "mime_type": source.mime_type,
            "native_format": source.native_format,
            "retrieval_rank": (source.item.retrieval_rank if source.item else None),
            "export_strategy": (source.item.export_strategy if source.item else None),
        }
        if source.has_native_bytes and source.filename:
            return [
                ZipEntry(
                    name=f"{root}/{safe_filename(source.filename)}",
                    data=source.native_bytes or b"",
                    metadata=metadata,
                )
            ]
        if source.source_type == SourceType.GITHUB.value:
            return self._github_entries(source, root, metadata)
        payload = self._document_payload(source, "docx")
        if (source.structure or {}).get("kind") == "tabular" and "xlsx" in exporter_registry.available_formats():
            payload = self._document_payload(source, "xlsx")
            fmt = "xlsx"
        else:
            fmt = "docx"
        self._prepend_synthesis(payload, kind="document")
        data = self._render(fmt, payload)
        filename = self._generated_filename(source, fmt, mode="document")
        return [
            ZipEntry(
                name=f"{root}/{safe_filename(filename)}",
                data=data,
                metadata=metadata,
            )
        ]

    def _github_entries(
        self, source: ResolvedSource, root: str, metadata: dict[str, Any]
    ) -> list[ZipEntry]:
        entries: list[ZipEntry] = []
        repo_folder = f"{root}/{safe_filename(source.source_id)}"
        fmt = _pick_for_repo(exporter_registry.available_formats())
        payload = source.analysis or self._captured_payload(source, fmt)
        if not (fmt == "pdf" and payload.architecture is not None):
            self._prepend_synthesis(payload, kind="report")
        data = self._render(fmt, payload)
        entries.append(
            ZipEntry(
                name=f"{repo_folder}/analysis.{fmt}",
                data=data,
                metadata={**metadata, "export_strategy": ScenarioType.GENERATED_REPORT.value},
            )
        )
        for rel_path, text in source.sampled_sources[: self._limits.max_files]:
            entry_path = f"{repo_folder}/source/{_safe_relpath(rel_path)}"
            if not entry_path.endswith((".md", ".rst", ".txt")) and not _is_text_sample(text):
                continue
            entries.append(
                ZipEntry(
                    name=entry_path,
                    data=(text + "\n").encode("utf-8"),
                    metadata={**metadata, "source_path": rel_path},
                )
            )
        return entries

    def _prepend_synthesis(self, payload: ExportPayload, *, kind: str) -> None:
        """Prepend LLM narrative sections to ``payload`` when a synthesizer is active."""
        if self._synthesizer is None or payload is None:
            return
        synthesize = getattr(self._synthesizer, "synthesize", None)
        if not callable(synthesize):
            return
        try:
            sections = synthesize(payload, kind=kind)
        except Exception as exc:  # noqa: BLE001 - synthesis is advisory only
            logger.warning("Export narrative synthesis failed: %s", exc)
            return
        if sections:
            payload.sections = [*sections, *payload.sections]

    def _zip_filename(self, resolved: list[ResolvedSource]) -> str:
        types = {source.source_type for source in resolved}
        if len(types) == 1:
            single = next(iter(types))
            label = {
                SourceType.UPLOADED_DOCUMENT.value: "uploaded-documents",
                SourceType.CONFLUENCE.value: "confluence",
                SourceType.GITHUB.value: "github",
                SourceType.SHAREPOINT.value: "sharepoint",
            }.get(single, "knowledge")
            return f"{label}-export.zip"
        return "knowledge-export.zip"

    def _document_payload(self, source: ResolvedSource, fmt: str) -> ExportPayload:
        payload = ExportPayload(
            title=source.source_name,
            source_url=source.source_url,
            source_name=source.source_name,
            source_type=source.source_type,
            content=source.content,
            metadata={"source_id": source.source_id, "native_format": source.native_format},
        )
        if fmt in {"csv", "xlsx"}:
            payload.rows = (source.structure or {}).get("rows") or []
            payload.headers = (source.structure or {}).get("headers") or []
        return payload

    def _captured_payload(self, source: ResolvedSource, fmt: str) -> ExportPayload:
        payload = ExportPayload(
            title=source.source_name,
            source_url=source.source_url,
            source_name=source.source_name,
            source_type=source.source_type,
        )
        if source.content:
            payload.sections.append(
                ExportSection(
                    heading="Repository Information",
                    body=source.content.strip(),
                    evidence="Observed from retrieval context",
                )
            )
        payload.sections.append(
            ExportSection(
                heading="Repository Analysis",
                body="A full repository re-analysis could not be performed for this export.",
                evidence="Unavailable",
            )
        )
        return payload

    def _render(self, fmt: str, payload: ExportPayload) -> bytes:
        exporter = exporter_registry.registry().get(fmt)
        if exporter is None:
            raise ExportValidationError(
                f"Export format '{fmt}' has no available exporter."
            )
        return exporter.render(payload)

    def _generated_filename(
        self, source: ResolvedSource, fmt: str, *, mode: str
    ) -> str:
        if mode == "report":
            stem = safe_filename(source.source_id or source.source_name, default="repository")
        elif source.filename and len(source.filename.split(".")) > 1:
            stem = safe_filename(source.filename.rsplit(".", 1)[0], default="document")
        else:
            stem = safe_filename(source.source_name, default="document")
        return ensure_extension(stem, fmt)

    def _record_outcome(
        self,
        context: ExportContext,
        intent: ExportIntent,
        resolved: list[ResolvedSource],
    ) -> None:
        context.strategy = intent.type
        context.requested_format = intent.format
        context.format_reason = intent.reason
        context.status = "completed"
        context.detail = {
            "sources": [
                {
                    "source_type": source.source_type,
                    "source_id": source.source_id,
                    "source_name": source.source_name,
                    "native": source.has_native_bytes,
                    "native_format": source.native_format,
                    "size_bytes": source.item.size_bytes,
                    "resolved_error": source.error,
                }
                for source in resolved
            ]
        }
        self._export_repo.save(context)


def _mime_for(fmt: str) -> str:
    if fmt in {"pdf", "docx", "xlsx", "pptx", "csv", "txt", "md", "json", "html"}:
        from app.export.formats import MIME_BY_EXTENSION

        return MIME_BY_EXTENSION.get(fmt, "application/octet-stream")
    return "application/octet-stream"


_RESPONSE_ARCHIVE_NAMES = {
    SourceType.CONFLUENCE.value: "confluence.zip",
    SourceType.SHAREPOINT.value: "sharepoint.zip",
    SourceType.GITHUB.value: "github.zip",
    SourceType.UPLOADED_DOCUMENT.value: "uploaded-documents.zip",
}


def _response_archive_name(used_types: set[str]) -> str:
    """Name the response export after the source system(s) it contains.

    Exactly one source system -> ``<system>.zip`` (``confluence.zip``,
    ``sharepoint.zip``, ``github.zip``...). Anything mixed -> ``Knowledge Export.zip``.
    """
    if len(used_types) == 1:
        return _RESPONSE_ARCHIVE_NAMES.get(next(iter(used_types)), "Knowledge Export.zip")
    return "Knowledge Export.zip"


def _source_type_counts(resolved: list[ResolvedSource]) -> str:
    counts: dict[str, int] = {}
    for source in resolved:
        counts[source.source_type] = counts.get(source.source_type, 0) + 1
    return ",".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _path_size(path: str | None) -> int:
    if not path:
        return 0
    try:
        import os

        return os.path.getsize(path)
    except OSError:
        return 0


def _ensure_generated_size(fmt: str, data: bytes, limits: ExportLimits) -> None:
    if len(data) > limits.max_generated_report_bytes:
        raise ExportLimitError(
            f"Generated '{fmt}' export exceeds the maximum report/document size of "
            f"{limits.max_generated_report_bytes} bytes."
        )


def _pick_for_repo(available: AbstractSet[str]) -> str:
    for candidate in ("pdf", "docx", "md", "html", "json", "txt"):
        if candidate in available:
            return candidate
    return sorted(available)[0]


def _safe_relpath(path: str) -> str:
    parts = []
    for segment in path.split("/"):
        cleaned = safe_filename(segment, default="file")
        if cleaned:
            parts.append(cleaned)
    return "/".join(parts) or "file"


def _is_text_sample(text: str) -> bool:
    return "\x00" not in text and bool(text.strip())
