"""Helpers that drive a Semantic Kernel ``ChatCompletionAgent`` session."""

from __future__ import annotations

import logging

from semantic_kernel.agents import ChatHistoryAgentThread
from semantic_kernel.contents import AuthorRole, ChatHistory, ChatMessageContent
from semantic_kernel.functions import KernelArguments

logger = logging.getLogger(__name__)


async def collect_agent_answer(
    agent: object,
    chat_history: ChatHistory,
) -> str:
    """Run one full agent turn and return the final assistant text.

    The agent may perform several auto-invoked tool rounds before the final answer;
    every assistant message the agent yields is considered and the last non-empty
    text wins. Falls back to the last assistant text recorded in ``chat_history``
    (Semantic Kernel auto-invocations append tool rounds there).
    """
    last_text = ""
    async for response_item in agent.invoke(  # type: ignore[attr-defined]
        thread=ChatHistoryAgentThread(chat_history=chat_history),
        arguments=KernelArguments(),
    ):
        text = _text_of(response_item.content)
        if text:
            last_text = text

    if not last_text:
        for message in reversed(chat_history.messages):
            if message.role != AuthorRole.ASSISTANT:
                continue
            text = _text_of(message)
            if text:
                last_text = text
                break
    logger.info("Agent turn complete; final answer is %d characters.", len(last_text))
    return last_text


def _text_of(message: ChatMessageContent) -> str:
    if message.content is None:
        return ""
    return message.content.strip()
