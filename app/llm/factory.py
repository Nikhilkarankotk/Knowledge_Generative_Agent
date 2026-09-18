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


def build_rag_answer_service(settings: Any, mistral_api_service: Any) -> Any:
    """Build the LLM service used for final RAG *answer generation*.

    * ``RAG_LLM_PROVIDER=azure_openai`` (with key, endpoint and deployment set)
      -> an Azure OpenAI deployment (e.g. the ChatGPT ``luna`` deployment).
    * Anything else (including unset/``mistral``) -> the shared Mistral service,
      so behaviour is unchanged unless Azure OpenAI is explicitly configured.

    Only the RAG answer LLM is affected; embeddings and OCR always keep using the
    Mistral client.
    """
    provider = (getattr(settings, "rag_llm_provider", "") or "").strip().lower()
    if provider in {"azure_openai", "azureopenai", "azure-openai", "azure"}:
        if (settings.azure_openai_api_key or "").strip() and (
            settings.azure_openai_endpoint or ""
        ).strip():
            from app.llm.azure_openai import AzureOpenAIClient
            from app.services.azure_openai_api_service import AzureOpenAIApiService

            client = AzureOpenAIClient.from_settings(settings)
            logger.info(
                "RAG answer generation uses Azure OpenAI (deployment=%s)",
                settings.azure_openai_deployment,
            )
            return AzureOpenAIApiService(client)
        logger.warning(
            "RAG_LLM_PROVIDER=azure_openai but AZURE_OPENAI_API_KEY/ENDPOINT is "
            "unset; falling back to the Mistral answer client."
        )
    return mistral_api_service


def build_report_llm_service(settings: Any) -> Any:
    """Build the LLM service that writes GitHub report narratives.

    * ``GITHUB_LLM_PROVIDER=anthropic_foundry`` with a key + endpoint -> Claude on
      Azure AI Foundry (e.g. ``claude-opus-4-8``).
    * ``GITHUB_LLM_PROVIDER=openrouter`` with a non-empty ``OPENROUTER_API_KEY``
      -> OpenRouter synthesis.
    * Anything else (including unset) -> the default Mistral synthesis client.
    """
    provider = (settings.github_llm_provider or "").strip().lower()
    if provider in {"anthropic_foundry", "anthropic", "claude", "foundry_anthropic"}:
        if (settings.anthropic_foundry_api_key or "").strip() and (
            settings.anthropic_foundry_endpoint or ""
        ).strip():
            from app.llm.anthropic_foundry import AnthropicFoundryClient
            from app.services.anthropic_foundry_api_service import (
                AnthropicFoundryApiService,
            )

            anthropic_client = AnthropicFoundryClient.from_settings(settings)
            logger.info(
                "GitHub report synthesis uses Anthropic on Azure AI Foundry (model=%s)",
                settings.anthropic_foundry_model,
            )
            return AnthropicFoundryApiService(anthropic_client)
        logger.warning(
            "GITHUB_LLM_PROVIDER=anthropic_foundry but ANTHROPIC_FOUNDRY_API_KEY/"
            "ENDPOINT is unset; falling back to the default Mistral synthesis client."
        )

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
