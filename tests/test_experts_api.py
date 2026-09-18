"""Tests for ``GET /api/experts`` (dashboard Top Experts / SME card)."""

from __future__ import annotations

from datetime import datetime

from app.models import ExportContext, ExportContextItem


def _seed(
    db_session,
    *,
    chat_message_id: int,
    session_id: str,
    meta: dict | None = None,
    source_type: str = "CONFLUENCE",
    source_id: str = "1",
    source_name: str = "Payments Architecture",
) -> None:
    context = ExportContext(
        chat_message_id=chat_message_id,
        session_id=session_id,
        created_at=datetime.now(),
        status="ready",
        source_count=1,
    )
    db_session.add(context)
    db_session.commit()
    db_session.add(
        ExportContextItem(
            export_context_id=context.id,
            source_type=source_type,
            source_id=source_id,
            source_name=source_name,
            meta=meta or {},
            retrieval_rank=0,
            exportable=True,
        )
    )
    db_session.commit()


def test_experts_endpoint_returns_owner_and_editor(client, db_session) -> None:
    _seed(
        db_session,
        chat_message_id=500,
        session_id="s-exp",
        meta={"owner": "Alice Doe", "last_editor": "Bob Ray"},
    )
    response = client.get("/api/experts", headers={"X-Session-ID": "s-exp"})

    assert response.status_code == 200
    body = response.json()
    assert body["chatMessageId"] == 500
    assert [expert["name"] for expert in body["experts"]] == ["Alice Doe", "Bob Ray"]
    first = body["experts"][0]
    assert first["role"] == "Owner"
    assert first["source"] == "Confluence"
    assert first["sourceCount"] == 1
    assert first["initials"] == "AD"


def test_experts_endpoint_empty_state(client, db_session) -> None:
    _seed(db_session, chat_message_id=501, session_id="s-empty", meta={})
    response = client.get("/api/experts", headers={"X-Session-ID": "s-empty"})

    assert response.status_code == 200
    assert response.json()["experts"] == []


def test_experts_endpoint_without_context_returns_empty(client) -> None:
    response = client.get("/api/experts", headers={"X-Session-ID": "never"})

    assert response.status_code == 200
    assert response.json() == {"chatMessageId": None, "experts": []}


def test_experts_endpoint_session_isolation(client, db_session) -> None:
    _seed(
        db_session,
        chat_message_id=502,
        session_id="owner",
        meta={"owner": "Alice Doe"},
    )
    response = client.get(
        "/api/experts",
        params={"chatMessageId": 502},
        headers={"X-Session-ID": "intruder"},
    )

    assert response.status_code == 200
    assert response.json()["experts"] == []


def test_experts_endpoint_specific_message(client, db_session) -> None:
    _seed(
        db_session,
        chat_message_id=503,
        session_id="s-msg",
        meta={"owner": "Alice Doe"},
    )
    _seed(
        db_session,
        chat_message_id=504,
        session_id="s-msg",
        meta={"owner": "Bob Ray"},
        source_id="2",
        source_name="Other Doc",
    )
    response = client.get(
        "/api/experts",
        params={"chatMessageId": 503},
        headers={"X-Session-ID": "s-msg"},
    )

    assert response.status_code == 200
    assert response.json()["chatMessageId"] == 503
    assert [expert["name"] for expert in response.json()["experts"]] == ["Alice Doe"]
