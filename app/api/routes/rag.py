"""Equivalent of ``RagController.java`` — extended with attached-document management.

Endpoints:

* ``POST   /api/rag/ingest`` (multipart, file part named ``file``)
* ``POST   /api/rag/query`` (raw request body is the user query)
* ``GET    /api/rag/documents`` (attached documents for the ``X-Session-ID``)
* ``DELETE /api/rag/documents/{document_id}`` (detach a document and its chunks)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import get_rag_service, get_session_id
from app.rag.rag_service import RagService
from app.schemas import DocumentOut

router = APIRouter()


@router.post("/ingest")
async def ingest_document(
    file: UploadFile = File(..., description="Document to ingest (PDF/DOCX)"),
    session_id: str = Depends(get_session_id),
    rag_service: RagService = Depends(get_rag_service),
) -> Response:
    content = await file.read()
    await run_in_threadpool(rag_service.ingest_document, content, file.filename, session_id)
    return Response(
        content=f"Document ingested successfully for session: {session_id}",
        media_type="text/plain",
    )


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    session_id: str = Depends(get_session_id),
    rag_service: RagService = Depends(get_rag_service),
) -> list[DocumentOut]:
    documents = await run_in_threadpool(rag_service.list_documents, session_id)
    return [DocumentOut.from_model(document) for document in documents]


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: int,
    session_id: str = Depends(get_session_id),
    rag_service: RagService = Depends(get_rag_service),
) -> Response:
    deleted = await run_in_threadpool(
        rag_service.delete_document, document_id, session_id
    )
    if not deleted:
        return Response(content="Document not found.", status_code=404)
    return Response(
        content="Document removed successfully.",
        media_type="text/plain",
    )


@router.post("/query")
async def query(
    request: Request,
    session_id: str = Depends(get_session_id),
    rag_service: RagService = Depends(get_rag_service),
) -> Response:
    raw = (await request.body()).decode("utf-8")
    user_query = raw
    # Angular sends the raw query string; some clients may send a JSON-encoded string.
    if raw.startswith('"'):
        import json

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                user_query = parsed
        except json.JSONDecodeError:
            pass
    answer = await run_in_threadpool(rag_service.query, user_query, session_id)
    return Response(content=answer, media_type="text/plain")
