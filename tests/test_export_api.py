"""API tests for the two export contracts.

* ``POST /api/export/{id}``            - lightweight AI response TXT export
* ``GET/POST /api/knowledge-export/{id}`` - detailed source-aware export (ZIP)

Covers session isolation, error mapping, downloads, the pre-flight OPTIONS
contract, and the regression that `POST /api/export/{id}` returns TXT (not 404)
even when no retrieval context was recorded for the message.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

from app.models import ChatMessage, DocumentFile, ExportContext, ExportContextItem


def _seed_exchange(
    client_session,
    *,
    session_id: str,
    user_text: str = "Explain the architecture",
    assistant_text: str = "The system is layered.",
    with_sources: bool = True,
    n_sources: int = 1,
) -> int:
    """Insert a user + assistant message pair; optionally a retrieval context.

    Returns the assistant message id (the id a client would export).
    """
    client_session.add(
        ChatMessage(role="user", session_id=session_id, content=user_text)
    )
    assistant = ChatMessage(
        role="assistant",
        session_id=session_id,
        content=assistant_text,
        detected_language="en",
    )
    client_session.add(assistant)
    client_session.commit()
    if not with_sources:
        return assistant.id

    context = ExportContext(
        chat_message_id=assistant.id,
        session_id=session_id,
        created_at=datetime.now(),
        status="ready",
        source_count=n_sources,
    )
    client_session.add(context)
    client_session.commit()
    for index in range(n_sources):
        client_session.add(
            ExportContextItem(
                export_context_id=context.id,
                source_type="CONFLUENCE" if index % 2 else "UPLOADED_DOCUMENT",
                source_id=f"src-{index}",
                source_name=f"Source {index}",
                content_reference="Retrieved content for export.",
                retrieval_rank=index,
                exportable=True,
            )
        )
    client_session.commit()
    return assistant.id


# ---------------------------------------------------------------------------
# Lightweight AI response export: POST /api/export/{id} -> TXT
# ---------------------------------------------------------------------------


def test_response_export_txt(client, db_session) -> None:
    message_id = _seed_exchange(db_session, session_id="s-txt", n_sources=2)
    response = client.post(f"/api/export/{message_id}", headers={"X-Session-ID": "s-txt"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "attachment" in response.headers["content-disposition"]
    assert f"ai-response-{message_id}.txt" in response.headers["content-disposition"]
    assert response.headers["x-export-filename"] == f"ai-response-{message_id}.txt"

    text = response.text
    assert "AI Response Export" in text
    assert "Explain the architecture" in text
    assert "The system is layered." in text
    assert "2026" in text  # timestamp rendered
    assert "Sources:" in text
    assert "Source 0" in text
    assert "Source 1" in text


def test_response_export_without_context_still_works(client, db_session) -> None:
    message_id = _seed_exchange(db_session, session_id="s-nc", with_sources=False)
    response = client.post(f"/api/export/{message_id}", headers={"X-Session-ID": "s-nc"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "Explain the architecture" in response.text
    assert "The system is layered." in response.text
    assert "Sources:" not in response.text


def test_response_export_pairs_prevailing_user_message(client, db_session) -> None:
    session_id = "s-pair"
    db_session.add(ChatMessage(role="user", session_id=session_id, content="First question"))
    db_session.add(
        ChatMessage(role="assistant", session_id=session_id, content="First answer", detected_language="en")
    )
    db_session.add(ChatMessage(role="user", session_id=session_id, content="Second question"))
    db_session.add(
        ChatMessage(role="assistant", session_id=session_id, content="Second answer", detected_language="en")
    )
    db_session.commit()
    second_assistant = db_session.query(ChatMessage).filter_by(content="Second answer").one()

    response = client.post(f"/api/export/{second_assistant.id}", headers={"X-Session-ID": session_id})

    assert response.status_code == 200
    assert "Second question" in response.text
    assert "Second answer" in response.text
    assert "First question" not in response.text
    assert "First answer" not in response.text


def test_response_export_for_user_message_404(client, db_session) -> None:
    user_msg = ChatMessage(role="user", session_id="s-u", content="hello")
    db_session.add(user_msg)
    db_session.commit()

    response = client.post(f"/api/export/{user_msg.id}", headers={"X-Session-ID": "s-u"})
    assert response.status_code == 404


def test_response_export_missing_message_404(client) -> None:
    response = client.post("/api/export/999999", headers={"X-Session-ID": "s"})
    assert response.status_code == 404


def test_response_export_session_isolation(client, db_session) -> None:
    message_id = _seed_exchange(db_session, session_id="owner")
    response = client.post(f"/api/export/{message_id}", headers={"X-Session-ID": "intruder"})
    assert response.status_code == 403


def test_response_export_ignores_unknown_body(client, db_session) -> None:
    message_id = _seed_exchange(db_session, session_id="s-body")
    response = client.post(
        f"/api/export/{message_id}",
        json={"hint": "xlsx"},
        headers={"X-Session-ID": "s-body"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# Detailed knowledge export: GET/POST /api/knowledge-export/{id}
# ---------------------------------------------------------------------------


def _seed_context_only(client_session, *, chat_message_id: int, session_id: str) -> None:
    context = ExportContext(
        chat_message_id=chat_message_id,
        session_id=session_id,
        created_at=datetime.now(),
        status="ready",
        source_count=1,
    )
    client_session.add(context)
    client_session.commit()
    client_session.add(
        ExportContextItem(
            export_context_id=context.id,
            source_type="CONFLUENCE",
            source_id="1234",
            source_name="Policy Page",
            content_reference="The policy page explains the monthly expense cycle.",
            retrieval_rank=0,
            exportable=True,
        )
    )
    client_session.commit()


def test_knowledge_export_metadata(client, db_session) -> None:
    _seed_context_only(db_session, chat_message_id=200, session_id="s-meta")
    response = client.get("/api/knowledge-export/200", headers={"X-Session-ID": "s-meta"})

    assert response.status_code == 200
    body = response.json()
    assert body["chat_message_id"] == 200
    assert body["status"] == "ready"
    assert body["source_count"] == 1
    assert body["sources"][0]["source_type"] == "CONFLUENCE"
    assert body["sources"][0]["source_name"] == "Policy Page"


def test_knowledge_export_metadata_session_isolation(client, db_session) -> None:
    _seed_context_only(db_session, chat_message_id=201, session_id="owner")
    response = client.get("/api/knowledge-export/201", headers={"X-Session-ID": "intruder"})
    assert response.status_code == 403
    assert "different chat session" in response.json()["message"].lower()


def test_knowledge_export_metadata_missing_context_404(client) -> None:
    response = client.get("/api/knowledge-export/404404", headers={"X-Session-ID": "s"})
    assert response.status_code == 404


def test_knowledge_export_generated_document(client, db_session) -> None:
    _seed_context_only(db_session, chat_message_id=202, session_id="s-dl")
    response = client.post(
        "/api/knowledge-export/202",
        json={"hint": "docx"},
        headers={"X-Session-ID": "s-dl"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"PK")


def test_knowledge_export_native_upload_bytes(client, db_session) -> None:
    session_id = "s-native"
    pdf = b"%PDF-native-download"
    db_session.add(
        DocumentFile(session_id=session_id, filename="policy.pdf", content=pdf, content_type="application/pdf")
    )
    db_session.commit()
    db_session.add(
        ChatMessage(role="assistant", session_id=session_id, content="Reply", detected_language="en")
    )
    db_session.commit()
    context = ExportContext(chat_message_id=203, session_id=session_id, created_at=datetime.now(), status="ready", source_count=1)
    db_session.add(context)
    db_session.commit()
    db_session.add(
        ExportContextItem(
            export_context_id=context.id,
            source_type="UPLOADED_DOCUMENT",
            source_id="policy.pdf",
            source_name="policy.pdf",
            filename="policy.pdf",
            mime_type="application/pdf",
            content_reference="policy",
            retrieval_rank=0,
            exportable=True,
        )
    )
    db_session.commit()

    response = client.post("/api/knowledge-export/203", headers={"X-Session-ID": session_id})

    assert response.status_code == 200
    assert response.content == pdf
    assert "attachment" in response.headers["content-disposition"]


def test_knowledge_export_zip_download(client, db_session) -> None:
    session_id = "s-zip"
    pdf = b"%PDF-zip"
    db_session.add(
        DocumentFile(session_id=session_id, filename="policy.pdf", content=pdf, content_type="application/pdf")
    )
    db_session.add(ChatMessage(role="assistant", session_id=session_id, content="Summary", detected_language="en"))
    db_session.commit()
    message_id = db_session.query(ChatMessage).filter_by(session_id=session_id).first().id
    context = ExportContext(chat_message_id=message_id, session_id=session_id, created_at=datetime.now(), status="ready", source_count=2)
    db_session.add(context)
    db_session.commit()
    db_session.add(
        ExportContextItem(
            export_context_id=context.id,
            source_type="UPLOADED_DOCUMENT",
            source_id="policy.pdf",
            source_name="policy.pdf",
            filename="policy.pdf",
            mime_type="application/pdf",
            content_reference="policy text",
            retrieval_rank=0,
            exportable=True,
        )
    )
    db_session.add(
        ExportContextItem(
            export_context_id=context.id,
            source_type="CONFLUENCE",
            source_id="5",
            source_name="How To",
            content_reference="guide describes steps.",
            retrieval_rank=1,
            exportable=True,
        )
    )
    db_session.commit()

    response = client.post(f"/api/knowledge-export/{message_id}", headers={"X-Session-ID": session_id})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    stream = zipfile.ZipFile(io.BytesIO(response.content))
    names = stream.namelist()
    assert names[0] == "manifest.json"
    assert "export-summary.md" in names
    assert any(name.startswith("uploaded_documents/") for name in names)


def test_knowledge_export_session_isolation(client, db_session) -> None:
    _seed_context_only(db_session, chat_message_id=205, session_id="owner")
    response = client.post("/api/knowledge-export/205", headers={"X-Session-ID": "intruder"})
    assert response.status_code == 403


def test_knowledge_export_missing_context_404(client) -> None:
    response = client.post("/api/knowledge-export/404404", headers={"X-Session-ID": "s"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# CORS pre-flight contract
# ---------------------------------------------------------------------------


def test_export_preflight_routes_support_post(client) -> None:
    for path in ("/api/export/142", "/api/knowledge-export/142"):
        response = client.options(
            path,
            headers={
                "Origin": "http://localhost:4200",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        # CORS pre-flight (OPTIONS) succeeds and POST is allowed on both routes.
        assert response.status_code == 200
        assert "POST" in response.headers["access-control-allow-methods"]