"""Tests for :mod:`app.services.conversation_memory_service` (ConversationMemoryService.java)."""

from app.services.conversation_memory_service import ConversationMemoryService


def test_empty_context_for_unknown_session() -> None:
    memory = ConversationMemoryService(max_history=10)
    assert memory.get_context("unknown") == []


def test_add_exchange_records_user_assistant_timestamp() -> None:
    memory = ConversationMemoryService(max_history=10)
    memory.add_exchange("s1", "hello", "hi there")
    context = memory.get_context("s1")
    assert len(context) == 1
    assert context[0]["user"] == "hello"
    assert context[0]["assistant"] == "hi there"
    assert context[0]["timestamp"].isdigit()


def test_max_history_trims_oldest_exchanges() -> None:
    memory = ConversationMemoryService(max_history=3)
    for i in range(5):
        memory.add_exchange("s1", f"u{i}", f"a{i}")
    context = memory.get_context("s1")
    assert len(context) == 3
    assert context[0]["user"] == "u2"
    assert context[-1]["user"] == "u4"


def test_sessions_are_isolated() -> None:
    memory = ConversationMemoryService(max_history=5)
    memory.add_exchange("a", "u1", "a1")
    memory.add_exchange("b", "u2", "a2")
    assert memory.get_context("a")[0]["user"] == "u1"
    assert memory.get_context("b")[0]["user"] == "u2"


def test_get_context_returns_copies_not_mutable_state() -> None:
    memory = ConversationMemoryService(max_history=5)
    memory.add_exchange("s1", "u", "a")
    context = memory.get_context("s1")
    del context[:]
    assert len(memory.get_context("s1")) == 1
