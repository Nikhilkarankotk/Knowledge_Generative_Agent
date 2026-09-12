"""Tests for :mod:`app.services.translation_service` (TranslationService.java)."""

import json

from app.services.mistral_api_service import MistralApiService
from app.services.translation_service import TranslationService
from tests.conftest import FakeLLM


def build_service(llm: FakeLLM) -> TranslationService:
    return TranslationService(MistralApiService(llm))


def test_short_text_short_circuits_to_english() -> None:
    llm = FakeLLM()
    service = build_service(llm)
    result = service.detect_and_translate("hi")
    assert result.lang == "en"
    assert result.langName == "English"
    assert llm.chat_requests == []


def test_null_text_returns_english() -> None:
    llm = FakeLLM()
    result = build_service(llm).detect_and_translate(None)
    assert result.lang == "en"
    assert result.translatedText is None


def test_detect_and_translate_parses_llm_json() -> None:
    llm = FakeLLM()
    llm.chat_response = json.dumps(
        {"lang": "te", "langName": "Romanized Telugu", "translatedText": "What are you doing?"}
    )
    result = build_service(llm).detect_and_translate("am chestunnav")
    assert result.lang == "te"
    assert result.langName == "Romanized Telugu"
    assert result.translatedText == "What are you doing?"
    # The prompt must be preserved.
    assert "Telugu markers: 'Ninu/Ninnu/Nuvvu/Evaru/Enti/Anti'." in llm.chat_requests[0]


def test_detect_and_translate_strips_code_fences() -> None:
    llm = FakeLLM()
    llm.chat_response = "```json\n" + json.dumps(
        {"lang": "en", "langName": "English", "translatedText": "text"}
    ) + "\n```"
    result = build_service(llm).detect_and_translate("some longer text")
    assert result.lang == "en"


def test_detect_and_translate_falls_back_to_english_on_error() -> None:
    llm = FakeLLM()
    llm.chat_response = "complete garbage"
    result = build_service(llm).detect_and_translate("some longer text")
    assert result.lang == "en"
    assert result.langName == "English"
    assert result.translatedText == "some longer text"


def test_translate_from_english_skips_english_target() -> None:
    llm = FakeLLM()
    result = build_service(llm).translate_from_english("hello", "English")
    assert result == "hello"
    assert llm.chat_requests == []


def test_translate_from_english_calls_llm_and_trims() -> None:
    llm = FakeLLM()
    llm.chat_response = "  hola   "
    service = build_service(llm)
    result = service.translate_from_english("hello", "Spanish")
    assert result == "hola"
    assert "from English to Spanish accurately" in llm.chat_requests[0]
    assert "Reply ONLY with the translated text." in llm.chat_requests[0]


def test_translate_from_english_none_text() -> None:
    llm = FakeLLM()
    assert build_service(llm).translate_from_english(None, "Spanish") is None
