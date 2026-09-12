"""End-to-end API tests.

Verifies the endpoints exposed by the FastAPI app against the contract of the original
Spring Boot controllers (paths, methods, status codes, request/response shapes and the
``X-Session-ID`` header handling).
"""

from tests.conftest import make_docx_with_text, make_pdf_with_text, make_png_bytes


def _chat_json(client, message: str, session_id: str = "session-1"):
    return client.post(
        "/api/chat",
        json={"message": message},
        headers={"X-Session-ID": session_id},
    )


def test_chat_json_returns_chat_message(client, api_llm) -> None:
    response = _chat_json(client, "Hello!")
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "assistant"
    assert body["content"] == "Test assistant response"
    assert body["sessionId"] == "session-1"
    assert body["id"] is not None
    assert body["isTranslated"] is False
    assert body["detectedLanguage"] == "en"
    assert body["timestamp"] is not None


def test_chat_defaults_to_default_session(client, api_llm) -> None:
    response = client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 200
    assert response.json()["sessionId"] == "default-session"


def test_chat_multipart_uploads_pdf_and_replies(client, api_llm) -> None:
    response = client.post(
        "/api/chat",
        data={"message": "What is in this document?"},
        files={"pdf": ("doc.pdf", make_pdf_with_text("Project portfolio details"), "application/pdf")},
        headers={"X-Session-ID": "session-2"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "assistant"
    assert body["sessionId"] == "session-2"


def test_chat_multipart_missing_pdf_returns_400(client, api_llm) -> None:
    response = client.post(
        "/api/chat",
        data={"message": "no file"},
        headers={"X-Session-ID": "session-2"},
    )
    assert response.status_code == 400


def test_chat_without_body_returns_400(client, api_llm) -> None:
    response = client.post("/api/chat", content="not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 400


def test_history_returns_messages_for_session(client, api_llm) -> None:
    _chat_json(client, "first", "s1")
    _chat_json(client, "second", "s1")
    _chat_json(client, "other session", "s2")

    history = client.get("/api/history", headers={"X-Session-ID": "s1"})
    assert history.status_code == 200
    entries = history.json()
    assert [e["role"] for e in entries] == ["user", "assistant", "user", "assistant"]
    assert entries[0]["content"] == "first"
    assert all(e["sessionId"] == "s1" for e in entries)


def test_history_sessions_lists_each_session(client, api_llm) -> None:
    _chat_json(client, "first", "s1")
    _chat_json(client, "second", "s2")
    sessions = client.get("/api/history/sessions")
    assert sessions.status_code == 200
    session_ids = {s["sessionId"] for s in sessions.json()}
    assert session_ids == {"s1", "s2"}


def test_delete_session_removes_history(client, api_llm) -> None:
    _chat_json(client, "hello", "s1")
    deleted = client.delete("/api/history/sessions/s1")
    assert deleted.status_code == 200
    assert client.get("/api/history", headers={"X-Session-ID": "s1"}).json() == []


def test_feedback_submit_and_update(client, api_llm) -> None:
    message = _chat_json(client, "hello", "s1").json()
    message_id = message["id"]

    first = client.post(
        "/api/feedback",
        json={"messageId": message_id, "rating": 1, "correctedAnswer": None},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/feedback",
        json={"messageId": message_id, "rating": -1, "correctedAnswer": "better answer"},
    )
    assert second.status_code == 200


def test_feedback_unknown_message_returns_500(client, api_llm) -> None:
    response = client.post(
        "/api/feedback",
        json={"messageId": 999999, "rating": 1, "correctedAnswer": ""},
    )
    assert response.status_code == 500
    assert response.json()["message"] == "Message not found"


def test_rag_ingest_accepts_pdf(client, api_llm) -> None:
    response = client.post(
        "/api/rag/ingest",
        files={"file": ("portfolio.pdf", make_pdf_with_text("Portfolio project details"), "application/pdf")},
        headers={"X-Session-ID": "rag-session"},
    )
    assert response.status_code == 200
    assert response.text == "Document ingested successfully for session: rag-session"


def test_rag_ingest_accepts_docx(client, api_llm) -> None:
    response = client.post(
        "/api/rag/ingest",
        files={"file": ("notes.docx", make_docx_with_text("Some document content here"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers={"X-Session-ID": "rag-session"},
    )
    assert response.status_code == 200


def test_rag_ingest_accepts_txt(client, api_llm) -> None:
    response = client.post(
        "/api/rag/ingest",
        files={"file": ("notes.md", b"# Heading\nMarkdown body", "text/markdown")},
        headers={"X-Session-ID": "rag-session"},
    )
    assert response.status_code == 200


def test_rag_ingest_accepts_image_via_ocr(client, api_llm) -> None:
    response = client.post(
        "/api/rag/ingest",
        files={"file": ("scan.png", make_png_bytes(), "image/png")},
        headers={"X-Session-ID": "rag-session"},
    )
    assert response.status_code == 200


def test_rag_ingest_unsupported_type_returns_400(client, api_llm) -> None:
    response = client.post(
        "/api/rag/ingest",
        files={"file": ("notes.xyz", b"\x00\x01binary data", "application/octet-stream")},
        headers={"X-Session-ID": "rag-session"},
    )
    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["message"]


def test_rag_documents_list_and_delete(client, api_llm) -> None:
    ingest = client.post(
        "/api/rag/ingest",
        files={"file": ("portfolio.pdf", make_pdf_with_text("Portfolio project details"), "application/pdf")},
        headers={"X-Session-ID": "doc-session"},
    )
    assert ingest.status_code == 200

    documents = client.get("/api/rag/documents", headers={"X-Session-ID": "doc-session"})
    assert documents.status_code == 200
    body = documents.json()
    assert len(body) == 1
    assert body[0]["filename"] == "portfolio.pdf"
    assert body[0]["contentType"] == "pdf"
    assert body[0]["status"] == "indexed"
    assert body[0]["sessionId"] == "doc-session"
    assert body[0]["id"] is not None
    assert body[0]["sizeBytes"] is not None
    assert body[0]["uploadedAt"] is not None

    doc_id = body[0]["id"]
    deleted = client.delete(f"/api/rag/documents/{doc_id}", headers={"X-Session-ID": "doc-session"})
    assert deleted.status_code == 200
    assert client.get("/api/rag/documents", headers={"X-Session-ID": "doc-session"}).json() == []


def test_rag_documents_are_session_scoped(client, api_llm) -> None:
    client.post(
        "/api/rag/ingest",
        files={"file": ("alpha.pdf", make_pdf_with_text("Alpha content"), "application/pdf")},
        headers={"X-Session-ID": "doc-a"},
    )
    client.post(
        "/api/rag/ingest",
        files={"file": ("beta.pdf", make_pdf_with_text("Beta content"), "application/pdf")},
        headers={"X-Session-ID": "doc-b"},
    )
    a = client.get("/api/rag/documents", headers={"X-Session-ID": "doc-a"}).json()
    b = client.get("/api/rag/documents", headers={"X-Session-ID": "doc-b"}).json()
    assert [d["filename"] for d in a] == ["alpha.pdf"]
    assert [d["filename"] for d in b] == ["beta.pdf"]


def test_rag_delete_document_other_session_returns_404(client, api_llm) -> None:
    client.post(
        "/api/rag/ingest",
        files={"file": ("mine.pdf", make_pdf_with_text("Mine"), "application/pdf")},
        headers={"X-Session-ID": "doc-owner"},
    )
    doc_id = client.get("/api/rag/documents", headers={"X-Session-ID": "doc-owner"}).json()[0]["id"]
    forbidden = client.delete(
        f"/api/rag/documents/{doc_id}", headers={"X-Session-ID": "doc-intruder"}
    )
    assert forbidden.status_code == 404
    assert len(
        client.get("/api/rag/documents", headers={"X-Session-ID": "doc-owner"}).json()
    ) == 1


def test_rag_delete_document_unknown_returns_404(client, api_llm) -> None:
    response = client.delete("/api/rag/documents/99999", headers={"X-Session-ID": "doc-session"})
    assert response.status_code == 404


def test_rag_delete_session_also_removes_documents(client, api_llm) -> None:
    client.post(
        "/api/rag/ingest",
        files={"file": ("alpha.pdf", make_pdf_with_text("Alpha content"), "application/pdf")},
        headers={"X-Session-ID": "doc-cleanup"},
    )
    client.delete("/api/history/sessions/doc-cleanup")
    assert client.get(
        "/api/rag/documents", headers={"X-Session-ID": "doc-cleanup"}
    ).json() == []


def test_rag_query_returns_llm_answer(client, api_llm) -> None:
    response = client.post(
        "/api/rag/query",
        content="Tell me about the project",
        headers={"X-Session-ID": "rag-session", "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.text == "Test assistant response"


def test_rag_query_with_quoted_json_body(client, api_llm) -> None:
    response = client.post(
        "/api/rag/query",
        content='"Tell me about the project"',
        headers={"X-Session-ID": "rag-session", "Content-Type": "application/json"},
    )
    assert response.status_code == 200


def test_mistral_upload_returns_ocr_response(client, api_llm) -> None:
    response = client.post(
        "/api/mistral/upload",
        data={"message": "Read this"},
        files={"file": ("scan.pdf", make_pdf_with_text("scanned"), "application/pdf")},
    )
    assert response.status_code == 200
    assert response.text == "OCR text response"


def test_mistral_upload_error_returns_500(client, api_llm, monkeypatch) -> None:
    def fail_upload(filename: str, content: bytes) -> str:
        raise RuntimeError("upload exploded")

    monkeypatch.setattr(api_llm, "upload_file", fail_upload)
    response = client.post(
        "/api/mistral/upload",
        data={"message": "Read this"},
        files={"file": ("scan.pdf", make_pdf_with_text("scanned"), "application/pdf")},
    )
    assert response.status_code == 500
    assert response.text.startswith("Error: upload exploded")


def test_cors_allows_frontend_origin(client, api_llm) -> None:
    response = client.options(
        "/api/chat",
        headers={
            "Origin": "http://localhost:4200",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "http://localhost:4200"


def test_health_endpoint(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
