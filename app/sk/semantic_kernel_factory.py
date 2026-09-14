"""SemanticKernelFactory - owns the shared Semantic Kernel runtime.

Responsibilities:

* Apply the Python 3.14 compatibility patch (idempotent).
* Create and cache the expensive, shared chat LLM service: an OpenAI-compatible
  ``AsyncOpenAI`` client pointed at the configured ``MISTRAL_BASE_URL`` wrapped in
  Semantic Kernel's ``OpenAIChatCompletion`` connector. The same endpoint that
  ``MistralClient`` already talks to.
* Run agent turns on a single, process-wide asyncio event loop so the shared async
  client always operates on its own loop (thread-safe across FastAPI workers).
* Build per-request ``KnowledgeGenerativeAgent`` instances: a fresh (cheap) Kernel,
   the session-bound plugins (KnowledgePlugin -> RagService, ConfluencePlugin ->
   ConfluenceService, GitHubPlugin -> GitHubService, SharePointPlugin ->
   SharePointService) and the agent. No per-request network clients are created.

Failure of the LLM/tool loop raises :class:`~app.core.exceptions.KnowledgeAgentError`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any

from app.agents.knowledge_generative_agent import (
    AGENT_NAME,
    SYSTEM_INSTRUCTIONS,
    KnowledgeGenerativeAgent,
)
from app.core.config import Settings
from app.core.exceptions import KnowledgeAgentError
from app.plugins.confluence_plugin import ConfluencePlugin
from app.plugins.github_plugin import GitHubPlugin
from app.plugins.knowledge_plugin import KnowledgePlugin
from app.plugins.sharepoint_plugin import SharePointPlugin
from app.services.confluence_service import ConfluenceService
from app.services.github_service import GitHubService
from app.services.sharepoint_service import SharePointService

logger = logging.getLogger(__name__)


class SemanticKernelFactory:
    """Shared Semantic Kernel runtime (chat service + event loop) + agent builder."""

    def __init__(
        self,
        settings: Settings,
        *,
        chat_service: Any | None = None,
        confluence_service: ConfluenceService | None = None,
        github_service: GitHubService | None = None,
        sharepoint_service: SharePointService | None = None,
        use_loop: bool = True,
    ) -> None:
        from app.sk.compat import apply_py314_compatibility_patch

        apply_py314_compatibility_patch()

        self._settings = settings
        self._confluence_service = confluence_service
        self._github_service = github_service
        self._sharepoint_service = sharepoint_service
        self._chat_service = chat_service if chat_service is not None else self._build_chat_service(settings)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        if use_loop:
            self._start_loop()

    # -- construction -----------------------------------------------------------

    def _build_chat_service(self, settings: Settings) -> Any:
        if not settings.mistral_api_key:
            raise ValueError(
                "MISTRAL_API_KEY is required to enable the Semantic Kernel agent."
            )
        from openai import AsyncOpenAI
        from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion

        async_client = AsyncOpenAI(
            api_key=settings.mistral_api_key,
            base_url=settings.mistral_base_url,
            timeout=settings.mistral_timeout_seconds,
            max_retries=settings.mistral_retries,
        )
        service = OpenAIChatCompletion(
            ai_model_id=settings.mistral_chat_model,
            service_id="chat",
            async_client=async_client,
        )
        from app.sk.compat import apply_mistral_compat

        apply_mistral_compat(service)
        return service

    # -- event loop -------------------------------------------------------------

    def _start_loop(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="semantic-kernel-agent-loop",
            daemon=True,
        )
        self._loop_thread.start()

    def close(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
        self._loop = None
        self._loop_thread = None

    # -- agent building ---------------------------------------------------------

    def build_agent(
        self,
        *,
        rag_service: Any | None = None,
        session_id: str | None = None,
        confluence_service: ConfluenceService | None = None,
        github_service: GitHubService | None = None,
        sharepoint_service: SharePointService | None = None,
        instructions: str = SYSTEM_INSTRUCTIONS,
        plugin_registrations: list[tuple[str, Any]] | None = None,
    ) -> KnowledgeGenerativeAgent:
        """Build a session-bound KnowledgeGenerativeAgent (cheap, per request)."""
        from semantic_kernel import Kernel
        from semantic_kernel.agents import ChatCompletionAgent
        from semantic_kernel.connectors.ai.function_choice_behavior import FunctionChoiceBehavior

        if plugin_registrations is None:
            registrations: list[tuple[str, Any]] = []
            if rag_service is not None:
                registrations.append(("Knowledge", KnowledgePlugin(rag_service, session_id or "")))
            registrations.append(("Confluence", ConfluencePlugin(confluence_service or self._confluence_service)))
            registrations.append(("GitHub", GitHubPlugin(github_service or self._github_service)))
            registrations.append(("SharePoint", SharePointPlugin(sharepoint_service or self._sharepoint_service)))
            plugin_registrations = registrations

        kernel = Kernel()
        kernel.add_service(self._chat_service, overwrite=True)
        for plugin_name, plugin in plugin_registrations:
            kernel.add_plugin(plugin, plugin_name=plugin_name)

        chat_agent = ChatCompletionAgent(
            kernel=kernel,
            name=AGENT_NAME,
            instructions=instructions,
            function_choice_behavior=FunctionChoiceBehavior.Auto(
                maximum_auto_invoke_attempts=self._settings.sk_max_auto_invoke_attempts,
            ),
        )
        return KnowledgeGenerativeAgent(chat_agent)

    # -- execution --------------------------------------------------------------

    def run_agent(
        self,
        agent: KnowledgeGenerativeAgent,
        chat_history: Any,
        timeout: float | None = None,
    ) -> str:
        """Run one agent turn, returning the final assistant text.

        Execution happens on the shared asyncio event loop so the shared async LLM
        client keeps a stable loop affinity. When no loop was started (e.g. tests with
        a scripted chat service), a short-lived loop is created with ``asyncio.run``.
        """
        timeout = timeout if timeout is not None else self._settings.sk_agent_timeout_seconds
        coro = agent.chat(chat_history)

        if self._loop is not None:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            try:
                answer = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError as exc:
                raise KnowledgeAgentError(f"Knowledge agent timed out after {timeout}s.") from exc
            except Exception as exc:
                raise KnowledgeAgentError(f"Knowledge agent failed: {exc}") from exc
        else:
            try:
                answer = asyncio.run(asyncio.wait_for(coro, timeout=timeout))
            except TimeoutError as exc:
                raise KnowledgeAgentError(f"Knowledge agent timed out after {timeout}s.") from exc
            except Exception as exc:
                raise KnowledgeAgentError(f"Knowledge agent failed: {exc}") from exc

        if not answer or not answer.strip():
            raise KnowledgeAgentError("Knowledge agent returned an empty answer.")
        return answer
