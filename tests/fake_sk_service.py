"""Scripted Semantic Kernel chat service for agent tests.

The auto-invocation loop lives in the base ``ChatCompletionClientBase``
(``get_chat_message_contents``), so a fake only needs to override
``_inner_get_chat_message_contents` and return proper
:class:`ChatMessageContent` objects. Behaviours:

* ``plan`` - a list of tool calls to emit, in order:
  ``(plugin_name, function_name, arguments_dict)``. The loop invokes each on the
  real Kernel (plugins are executed), feeds the result back, and calls again.
* ``answerer`` - ``callable(ChatHistory) -> str`` invoked once the plan is
  exhausted to produce the final assistant text. Defaults to joining the tool
  results that were surfaced so far.
* ``delay`` - optional sleep before each response (for timeout tests).

The fake records every chat history snapshot in ``histories`` so tests can assert
on the tool call/argument parsing and on session isolation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from pydantic import PrivateAttr
from semantic_kernel.connectors.ai.chat_completion_client_base import ChatCompletionClientBase
from semantic_kernel.connectors.ai.open_ai.prompt_execution_settings.open_ai_prompt_execution_settings import (
    OpenAIChatPromptExecutionSettings,
)
from semantic_kernel.contents import (
    AuthorRole,
    ChatHistory,
    ChatMessageContent,
    FunctionCallContent,
    FunctionResultContent,
)
from semantic_kernel.functions.function_result import FunctionResult


def tool_results(history: ChatHistory) -> list[str]:
    """Return the plain-text results surfaced to the model so far."""
    results: list[str] = []
    for message in history.messages:
        for item in getattr(message, "items", []) or []:
            if not isinstance(item, FunctionResultContent):
                continue
            result = getattr(item, "result", None)
            if isinstance(result, FunctionResult):
                value = result.value
            else:
                value = result
            results.append("" if value is None else str(value))
    return results


def _default_answer(history: ChatHistory) -> str:
    results = tool_results(history)
    return "\n".join(results) or "Synthesized answer"


def _count_tool_calls(history: ChatHistory) -> int:
    count = 0
    for message in history.messages:
        for item in getattr(message, "items", []) or []:
            if isinstance(item, FunctionCallContent):
                count += 1
    return count


class ScriptedChatCompletion(ChatCompletionClientBase):
    """Chat service that executes a scripted tool-call plan deterministically."""

    SUPPORTS_FUNCTION_CALLING = True

    _plan: list[tuple[str, str, dict[str, Any]]] = PrivateAttr(default_factory=list)
    _answerer: Callable[[ChatHistory], str] = PrivateAttr(default=_default_answer)
    _delay: float = PrivateAttr(default=0.0)
    _histories: list[ChatHistory] = PrivateAttr(default_factory=list)

    def __init__(
        self,
        plan: list[tuple[str, str, dict[str, Any]]] | None = None,
        answerer: Callable[[ChatHistory], str] | None = None,
        delay: float = 0.0,
    ) -> None:
        super().__init__(ai_model_id="fake-llm", service_id="default")
        self.ai_model_id = "fake-llm"
        self.service_id = "default"
        # Avoid pydantic's private-attribute machinery (it binds callables); keep
        # these plain instance values in __dict__ instead.
        object.__setattr__(self, "_plan", list(plan or []))
        object.__setattr__(self, "_answerer", answerer or _default_answer)
        object.__setattr__(self, "_delay", delay)
        object.__setattr__(self, "_histories", [])

    @property
    def histories(self) -> list[ChatHistory]:
        """Chat history snapshots recorded on each model call (for assertions)."""
        return self._histories

    def get_prompt_execution_settings_class(self):  # type: ignore[no-untyped-def]
        return OpenAIChatPromptExecutionSettings

    async def _inner_get_chat_message_contents(self, chat_history, settings):  # type: ignore[no-untyped-def, override]
        self._histories.append(chat_history)
        if self._delay:
            await asyncio.sleep(self._delay)

        tool_calls_so_far = _count_tool_calls(chat_history)
        if tool_calls_so_far < len(self._plan):
            plugin_name, function_name, arguments = self._plan[tool_calls_so_far]
            call = FunctionCallContent(
                id=f"call_{tool_calls_so_far}",
                name=f"{plugin_name}-{function_name}",
                arguments=json.dumps(arguments, ensure_ascii=False),
            )
            return [ChatMessageContent(role=AuthorRole.ASSISTANT, items=[call])]
        return [
            ChatMessageContent(
                role=AuthorRole.ASSISTANT,
                content=self._answerer(chat_history),
            )
        ]
