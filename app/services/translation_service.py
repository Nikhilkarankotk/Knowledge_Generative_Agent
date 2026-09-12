"""Equivalent of ``TranslationService.java``.

The two LLM prompts used for language detection/translation are preserved verbatim from
the Java implementation (including the examples and strict rules).
"""

from __future__ import annotations

import json
import logging

from app.schemas import TranslationResult
from app.services.mistral_api_service import MistralApiService

logger = logging.getLogger(__name__)


class TranslationService:
    def __init__(self, mistral_service: MistralApiService) -> None:
        self._mistral_service = mistral_service

    def detect_and_translate(self, text: str | None) -> TranslationResult:
        if text is None or len(text.strip()) < 3:
            return TranslationResult(lang="en", langName="English", translatedText=text)

        prompt = (
            "Analyze the following text.\n"
            "1. Detect its base language (ISO 639-1 code).\n"
            "2. Detect the full language name and style. Be extremely careful to distinguish "
            "between South Indian languages like Telugu (te), Kannada (kn), and Tamil (ta).\n"
            "   Telugu markers: 'Ninu/Ninnu/Nuvvu/Evaru/Enti/Anti'. Kannada markers: 'Yaaru/Avaru/Ninna/Esaru'.\n"
            "3. If it is NOT English, translate it accurately to English.\n\n"
            "EXAMPLES:\n"
            "- 'am chestunnav' -> { 'lang': 'te', 'langName': 'Romanized Telugu', 'translatedText': 'What are you doing?' }\n"
            "- 'ala unnav' -> { 'lang': 'te', 'langName': 'Romanized Telugu', 'translatedText': 'How are you?' }\n"
            "- 'Ni peru anti' -> { 'lang': 'te', 'langName': 'Romanized Telugu', 'translatedText': 'What is your name?' }\n"
            "- 'Ninnu evaru design chesaru' -> { 'lang': 'te', 'langName': 'Romanized Telugu', 'translatedText': 'Who designed you?' }\n"
            "- 'Ninna hesaru enu' -> { 'lang': 'kn', 'langName': 'Romanized Kannada', 'translatedText': 'What is your name?' }\n\n"
            "Return a raw JSON object ONLY: { 'lang': '...', 'langName': '...', 'translatedText': '...' }.\n\n"
            f"Text: {text}"
        )

        try:
            response = self._mistral_service.generate_response(prompt)
            json_text = response.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(json_text)
            return TranslationResult.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001 - Java catches broadly and falls back to English
            logger.error("Language detection failed, falling back to English: %s", exc)
            return TranslationResult(lang="en", langName="English", translatedText=text)

    def translate_from_english(self, text: str | None, lang_name: str | None) -> str | None:
        if lang_name is None or "english" in lang_name.lower() or text is None:
            return text

        prompt = (
            f"System: You are a professional translator. Translate the following text from English to {lang_name} accurately.\n"
            "STRICT RULES:\n"
            "1. Do NOT add any conversational filler, unrelated greetings, or hallucinations.\n"
            "2. Ensure the translation is faithful to the original English meaning.\n"
            "3. If the target is a Romanized language (like Romanized Telugu), use the English alphabet.\n"
            "4. Reply ONLY with the translated text.\n\n"
            f"Text to translate: {text}"
        )
        translation = self._mistral_service.generate_response(prompt)
        return translation.strip() if translation is not None else text
