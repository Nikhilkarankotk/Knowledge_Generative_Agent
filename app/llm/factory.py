"""LLM provider selection for export narrative synthesis.

The GitHub repository analysis report can use a *separate* LLM (OpenRouter) while
chat, RAG and embeddings stay on Mistral. This factory picks the right backend from
``GITHUB_LLM_PROVIDER`` and returns a duck-typed service exposing
``generate_response(prompt)`` (``MistralApiService`` or ``OpenRouterApiService``).
"""

from __future__ import annotations

import logging
from typing import Any

from app.llm import MistralClient
from app.llm.openrouter import OpenRouterClient
from app.services.mistral_api_service import MistralApiService
from app.services.openrouter_api_service import OpenRouterApiService

logger = logging.getLogger(__name__)


def build_report_llm_service(settings: Any) -> Any:
    """Build the LLM service that writes GitHub report narratives.

    * ``GITHUB_LLM_PROVIDER=openrouter`` (or ``"OpenRouter"``/``"OPENROUTER"``)
      with a non-empty ``OPENROUTER_API_KEY`` -> OpenRouter synthesis.
    * Anything else (including unset) -> the default Mistral synthesis client.
    """
    provider = (settings.github_llm_provider or "").strip().lower()
    if provider == "openrouter" and (settings.openrouter_api_key or "").strip():
        or_client = OpenRouterClient(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url or "https://openrouter.ai/api/v1",
            timeout_seconds=settings.openrouter_timeout_seconds,
            retries=settings.openrouter_retries,
            model=settings.github_llm_model or "openrouter/free",
            max_tokens=settings.github_analysis_max_tokens,
            temperature=settings.github_analysis_temperature,
        )
        logger.info(
            "GitHub report synthesis uses OpenRouter (model=%s)",
            settings.github_llm_model or "openrouter/free",
        )
        return OpenRouterApiService(or_client)
    if provider == "openrouter":
        logger.warning(
            "GITHUB_LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is unset; "
            "falling back to the default Mistral synthesis client."
        )

    # Default path: a dedicated Mistral synthesis client with a longer read timeout
    # and a single retry so writing the full report has room while a slow call
    # cannot stall the export for minutes.
    client = MistralClient(
        api_key=settings.mistral_api_key,
        base_url=settings.mistral_base_url,
        timeout_seconds=90.0,
        retries=1,
        chat_model=settings.mistral_chat_model,
        embedding_model=settings.mistral_embedding_model,
        ocr_model=settings.mistral_ocr_model,
    )
    return MistralApiService(client)
