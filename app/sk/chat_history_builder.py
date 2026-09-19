"""Build the runtime ``ChatHistory`` Semantic Kernel uses for a session.

PostgreSQL ``chat_message`` rows remain the source of truth for the conversation;
this module only translates rows into Semantic Kernel's in-memory history for the
current agent turn.
"""

from __future__ import annotations

from semantic_kernel.contents import ChatHistory

from app.export.sources_footer import strip_sources_footer
from app.models import ChatMessage


def build_agent_chat_history(
    messages: list[ChatMessage],
    *,
    current_user_message: str | None = None,
    max_messages: int = 30,
) -> ChatHistory:
    """Translate ordered ``ChatMessage`` rows into a ``ChatHistory``.

    ``current_user_message`` is re-appended explicitly so the caller is never
    dependent on whether the latest row has been persisted yet.
    """
    history = ChatHistory()
    history_items = list(messages)
    if current_user_message is not None:
        # Drop a trailing row that duplicates the live message (already persisted
        # rows are the source of truth, but the live message is re-added below).
        if history_items and history_items[-1].content == current_user_message:
            history_items = history_items[:-1]
        history_items = history_items[-(max_messages - 1):]
    else:
        history_items = history_items[-max_messages:]

    for message in history_items:
        if message.role == "user":
            history.add_user_message(message.content or "")
        elif message.role == "assistant":
            # The "Sources" footer is presentation for the user; the model should
            # see the answer text only, or it starts copying old footers.
            history.add_assistant_message(strip_sources_footer(message.content or ""))

    if current_user_message is not None:
        history.add_user_message(current_user_message)
    return history
