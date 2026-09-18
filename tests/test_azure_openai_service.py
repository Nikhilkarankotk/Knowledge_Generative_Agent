"""Tests for the Azure OpenAI RAG answer LLM (client, service, factory selection)."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import AzureOpenAIApiError
from app.llm.azure_openai import AzureOpenAIClient
from app.llm.factory import build_rag_answer_service
from app.services.azure_openai_api_service import AzureOpenAIApiService


def _client() -> AzureOpenAIClient:
    return AzureOpenAIClient(
        api_key="test-key",
        endpoint="https://example.services.ai.azure.com/openai/v1",
        deployment="gpt-5.6-luna",
        retries=2,
    )


def test_chat_completion_extracts_choice_content(monkeypatch) -> None:
    client = _client()
    response = httpx.Response(200, json={"choices": [{"message": {"content": "Hi from luna"}}]})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    assert client.chat_completion("prompt") == "Hi from luna"


def test_chat_completion_missing_choice_raises(monkeypatch) -> None:
    client = _client()
    response = httpx.Response(200, json={})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    with pytest.raises(AzureOpenAIApiError):
        client.chat_completion("prompt")


def test_chat_completion_uses_foundry_v1_body(monkeypatch) -> None:
    # Foundry /openai/v1 surface: POST /chat/completions with the model in the
    # body, max_completion_tokens (not max_tokens), and NO temperature by default.
    client = _client()
    captured: dict[str, object] = {}

    def fake_post(url, json=None):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = json
        request = httpx.Request("POST", "http://test" + str(url))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}, request=request
        )

    monkeypatch.setattr(client._client, "post", fake_post)
    assert client.chat_completion("hello") == "ok"
    assert str(captured["url"]) == "/chat/completions"
    body = captured["json"]
    assert body["model"] == "gpt-5.6-luna"
    assert body["messages"][0]["content"] == "hello"
    assert "max_completion_tokens" in body
    assert "max_tokens" not in body
    assert "temperature" not in body  # luna rejects a custom temperature


def test_chat_completion_sends_temperature_when_supported(monkeypatch) -> None:
    client = AzureOpenAIClient(
        api_key="k",
        endpoint="https://example.services.ai.azure.com/openai/v1",
        deployment="gpt-4o",
        temperature=0.3,
        supports_temperature=True,
    )
    captured: dict[str, object] = {}

    def fake_post(url, json=None):  # noqa: ANN001
        captured["json"] = json
        request = httpx.Request("POST", "http://test" + str(url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=request)

    monkeypatch.setattr(client._client, "post", fake_post)
    client.chat_completion("hi")
    assert captured["json"]["temperature"] == 0.3


def test_missing_credentials_raise() -> None:
    with pytest.raises(AzureOpenAIApiError):
        AzureOpenAIClient(api_key="", endpoint="https://x", deployment="luna")
    with pytest.raises(AzureOpenAIApiError):
        AzureOpenAIClient(api_key="k", endpoint="", deployment="luna")


def test_service_generate_response_delegates(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(client, "chat_completion", lambda prompt: f"answer:{prompt}")
    service = AzureOpenAIApiService(client)
    assert service.generate_response("q") == "answer:q"


def test_factory_returns_mistral_when_provider_unset() -> None:
    settings = Settings(rag_llm_provider="")
    sentinel = object()
    assert build_rag_answer_service(settings, sentinel) is sentinel


def test_factory_falls_back_to_mistral_when_azure_unconfigured() -> None:
    # Provider requested but no key/endpoint -> safe fallback to Mistral service.
    settings = Settings(rag_llm_provider="azure_openai", azure_openai_api_key="", azure_openai_endpoint="")
    sentinel = object()
    assert build_rag_answer_service(settings, sentinel) is sentinel


def test_factory_builds_azure_service_when_configured() -> None:
    settings = Settings(
        rag_llm_provider="azure_openai",
        azure_openai_api_key="k",
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_deployment="luna",
    )
    service = build_rag_answer_service(settings, object())
    assert isinstance(service, AzureOpenAIApiService)
    service.close()
