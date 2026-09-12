"""Tests for :mod:`app.llm.client` and :mod:`app.services.mistral_api_service`."""

import httpx
import pytest

from app.core.exceptions import MistralApiError
from app.llm import MistralClient
from app.services.mistral_api_service import MistralApiService


@pytest.fixture
def client() -> MistralClient:
    return MistralClient(api_key="test-key", retries=3)


def test_chat_completion_extracts_choice_content(client: MistralClient, monkeypatch) -> None:
    response = httpx.Response(200, json={"choices": [{"message": {"content": "Hello!"}}]})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    assert client.chat_completion("prompt") == "Hello!"


def test_chat_completion_missing_choice_raises(client: MistralClient, monkeypatch) -> None:
    response = httpx.Response(200, json={})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    with pytest.raises(MistralApiError):
        client.chat_completion("prompt")


def test_generate_embedding_extracts_data_embedding(client: MistralClient, monkeypatch) -> None:
    response = httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    assert client.generate_embedding("text") == [0.1, 0.2, 0.3]


def test_upload_file_returns_id(client: MistralClient, monkeypatch) -> None:
    response = httpx.Response(200, json={"id": "file-42"})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    assert client.upload_file("a.pdf", b"data") == "file-42"


def test_upload_file_missing_id_raises(client: MistralClient, monkeypatch) -> None:
    response = httpx.Response(200, json={"x": 1})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    with pytest.raises(MistralApiError):
        client.upload_file("a.pdf", b"data")


def test_retry_loop_succeeds_after_transient_failures(client: MistralClient, monkeypatch) -> None:
    attempts = {"n": 0}

    def flaky_request(method, url, json=None, files=None, headers=None, params=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom")
        request = httpx.Request("POST", "http://test" + str(url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=request)

    monkeypatch.setattr(client._client, "request", flaky_request)
    assert client.chat_completion("prompt") == "ok"
    assert attempts["n"] == 3


def test_retry_loop_gives_up_after_all_attempts(client: MistralClient, monkeypatch) -> None:
    def always_fail(method, url, json=None, files=None, headers=None, params=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(client._client, "request", always_fail)
    with pytest.raises(MistralApiError):
        client.chat_completion("prompt")


def test_ocr_process_returns_joined_markdown(client: MistralClient, monkeypatch) -> None:
    responses = [
        httpx.Response(200, json={"url": "https://signed/url"}, request=httpx.Request("GET", "http://test/x")),
        httpx.Response(
            200,
            json={
                "pages": [
                    {"index": 0, "markdown": "Page one"},
                    {"index": 1, "markdown": "Page two"},
                ]
            },
            request=httpx.Request("POST", "http://test/x"),
        ),
    ]
    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(client, "_request_with_retry", fake_request)
    assert client.ocr_process("f1", "m") == "Page one\n\nPage two"
    assert calls == ["/files/f1/url", "/ocr"]


def test_api_service_generate_response_passthrough(monkeypatch) -> None:
    client = MistralClient(api_key="k")
    monkeypatch.setattr(client, "chat_completion", lambda p: f"echo:{p}")
    service = MistralApiService(client)
    assert service.generate_response("hi") == "echo:hi"
