"""LLM integration package.

Provides the HTTP client used to talk to the Mistral AI API. The Java application
built a ``WebClient`` with a base URL of ``https://api.mistral.ai/v1`` and a bearer
token; the Python counterpart wraps :mod:`httpx` with the identical base URL, headers,
timeout and retry behaviour (30s timeout, up to 3 retries).
"""

from app.llm.client import MistralClient
from app.llm.factory import build_rag_answer_service, build_report_llm_service
from app.llm.openrouter import OpenRouterClient

__all__ = [
    "MistralClient",
    "OpenRouterClient",
    "build_rag_answer_service",
    "build_report_llm_service",
]
