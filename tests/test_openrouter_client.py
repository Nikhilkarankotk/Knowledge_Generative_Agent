"""Tests for :mod:`app.llm.openrouter` and the report LLM provider factory."""

import httpx
import pytest

from app.core.exceptions import OpenRouterApiError
from app.llm import build_report_llm_service
from app.llm.openrouter import OpenRouterClient
from app.services.mistral_api_service import MistralApiService
from app.services.openrouter_api_service import OpenRouterApiService


@pytest.fixture
def client() -> OpenRouterClient:
    return OpenRouterClient(api_key="test-key", retries=2)


def test_chat_completion_extracts_choice_content(client: OpenRouterClient, monkeypatch) -> None:
    response = httpx.Response(200, json={"choices": [{"message": {"content": "Hello OpenRouter"}}]})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    assert client.chat_completion("prompt") == "Hello OpenRouter"


def test_chat_completion_sends_openai_compatible_body(client: OpenRouterClient, monkeypatch) -> None:
    captured: dict = {}

    def fake_post(url: str, *, json: dict | None = None) -> httpx.Response:
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(client, "_post_with_retry", fake_post)
    client.chat_completion("hello")
    assert captured["url"] == "/chat/completions"
    assert captured["json"]["model"] == "openrouter/free"
    assert captured["json"]["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["json"]["max_tokens"] == 3000
    assert captured["json"]["temperature"] == 0.1


def test_chat_completion_missing_choice_raises(client: OpenRouterClient, monkeypatch) -> None:
    response = httpx.Response(200, json={})
    monkeypatch.setattr(client, "_post_with_retry", lambda *a, **k: response)
    with pytest.raises(OpenRouterApiError):
        client.chat_completion("prompt")


def test_retry_loop_succeeds_after_transient_failures(client: OpenRouterClient, monkeypatch) -> None:
    attempts = {"n": 0}

    def flaky_post(url: str, **kwargs) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom")
        request = httpx.Request("POST", "http://test" + str(url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=request)

    monkeypatch.setattr(client._client, "post", flaky_post)
    assert client.chat_completion("prompt") == "ok"
    assert attempts["n"] == 3


def test_retry_loop_gives_up_after_all_attempts(client: OpenRouterClient, monkeypatch) -> None:
    def always_fail(url: str, **kwargs) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(client._client, "post", always_fail)
    with pytest.raises(OpenRouterApiError):
        client.chat_completion("prompt")


def test_openrouter_api_service_generate_response(monkeypatch) -> None:
    client = OpenRouterClient(api_key="k")
    monkeypatch.setattr(client, "chat_completion", lambda p: f"echo:{p}")
    service = OpenRouterApiService(client)
    assert service.generate_response("hi") == "echo:hi"
    assert service.chat_completion("hi") == "echo:hi"


def _settings(**overrides) -> object:
    base = {
        "github_llm_provider": "",
        "github_llm_model": "openrouter/free",
        "openrouter_api_key": "",
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "openrouter_timeout_seconds": 60.0,
        "openrouter_retries": 2,
        "github_analysis_max_tokens": 3000,
        "github_analysis_temperature": 0.1,
        "mistral_api_key": "mistral-key",
        "mistral_base_url": "https://api.mistral.ai/v1",
        "mistral_chat_model": "open-mistral-nemo",
        "mistral_embedding_model": "mistral-embed",
        "mistral_ocr_model": "mistral-ocr-latest",
    }
    base.update(overrides)
    return type("Settings", (), base)()


def test_factory_defaults_to_mistral() -> None:
    service = build_report_llm_service(_settings())
    assert isinstance(service, MistralApiService)


def test_factory_uses_openrouter_when_configured() -> None:
    service = build_report_llm_service(
        _settings(github_llm_provider="openrouter", openrouter_api_key="or-key")
    )
    assert isinstance(service, OpenRouterApiService)


def test_factory_falls_back_when_openrouter_key_missing() -> None:
    service = build_report_llm_service(_settings(github_llm_provider="openrouter"))
    assert isinstance(service, MistralApiService)
