"""Tests for the Anthropic (Azure AI Foundry) report-synthesis LLM."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import AnthropicFoundryApiError
from app.llm.anthropic_foundry import AnthropicFoundryClient
from app.llm.factory import build_report_llm_service
from app.services.anthropic_foundry_api_service import AnthropicFoundryApiService


def _client() -> AnthropicFoundryClient:
    return AnthropicFoundryClient(
        api_key="test-key",
        endpoint="https://example.services.ai.azure.com/anthropic",
        model="claude-opus-4-8",
        retries=2,
    )


def test_chat_completion_concatenates_text_blocks(monkeypatch) -> None:
    client = _client()
    response = httpx.Response(
        200,
        json={"content": [{"type": "text", "text": "The capital of France is Paris."}]},
    )
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    assert client.chat_completion("prompt") == "The capital of France is Paris."


def test_chat_completion_missing_content_raises(monkeypatch) -> None:
    client = _client()
    response = httpx.Response(200, json={})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    with pytest.raises(AnthropicFoundryApiError):
        client.chat_completion("prompt")


def test_chat_completion_builds_messages_body(monkeypatch) -> None:
    client = _client()
    captured: dict[str, object] = {}

    def fake_post(url, json=None):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = json
        request = httpx.Request("POST", "http://test" + str(url))
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}]},
            request=request,
        )

    monkeypatch.setattr(client._client, "post", fake_post)
    assert client.chat_completion("hello") == "ok"
    assert str(captured["url"]) == "/v1/messages"
    body = captured["json"]
    assert body["model"] == "claude-opus-4-8"
    assert body["messages"][0]["content"] == "hello"
    assert "max_tokens" in body
    # No temperature is sent unless one is configured.
    assert "temperature" not in body


def test_client_sends_required_headers() -> None:
    client = _client()
    headers = client._client.headers
    assert headers.get("x-api-key") == "test-key"
    assert headers.get("anthropic-version") == "2023-06-01"


def test_missing_credentials_raise() -> None:
    with pytest.raises(AnthropicFoundryApiError):
        AnthropicFoundryClient(api_key="", endpoint="https://x/anthropic", model="claude-opus-4-8")
    with pytest.raises(AnthropicFoundryApiError):
        AnthropicFoundryClient(api_key="k", endpoint="", model="claude-opus-4-8")


def test_service_generate_response_delegates(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(client, "chat_completion", lambda prompt: f"claude:{prompt}")
    service = AnthropicFoundryApiService(client)
    assert service.generate_response("q") == "claude:q"


def test_factory_builds_anthropic_when_configured() -> None:
    settings = Settings(
        github_llm_provider="anthropic_foundry",
        anthropic_foundry_api_key="k",
        anthropic_foundry_endpoint="https://example.services.ai.azure.com/anthropic",
        anthropic_foundry_model="claude-opus-4-8",
    )
    service = build_report_llm_service(settings)
    assert isinstance(service, AnthropicFoundryApiService)
    service.close()


def test_factory_anthropic_falls_back_to_mistral_when_unconfigured() -> None:
    # Provider requested but no key/endpoint -> safe fallback to the Mistral client.
    settings = Settings(
        github_llm_provider="anthropic_foundry",
        anthropic_foundry_api_key="",
        anthropic_foundry_endpoint="",
        mistral_api_key="m",
    )
    service = build_report_llm_service(settings)
    assert type(service).__name__ == "MistralApiService"
    service.close()
