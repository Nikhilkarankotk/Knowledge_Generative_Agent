"""Tests for :mod:`app.rag.text_chunker` (TextChunker.java)."""

import pytest

from app.core.exceptions import IllegalArgumentException
from app.rag.text_chunker import TextChunker


@pytest.fixture
def chunker() -> TextChunker:
    return TextChunker()


def test_empty_text_yields_no_chunks(chunker: TextChunker) -> None:
    assert chunker.chunk_text("", 500) == []


def test_single_chunk_when_text_smaller_than_chunk_size(chunker: TextChunker) -> None:
    assert chunker.chunk_text("hello", 500) == ["hello"]


def test_exact_multiple_split(chunker: TextChunker) -> None:
    text = "abcdefghij"
    assert chunker.chunk_text(text, 5) == ["abcde", "fghij"]


def test_character_based_split_without_overlap(chunker: TextChunker) -> None:
    text = "abcdefghijk"
    assert chunker.chunk_text(text, 5) == ["abcde", "fghij", "k"]


def test_chunk_size_of_500_preserves_java_behavior(chunker: TextChunker) -> None:
    text = "a" * 1250
    chunks = chunker.chunk_text(text, 500)
    assert chunks == ["a" * 500, "a" * 500, "a" * 250]


def test_non_positive_chunk_size_raises(chunker: TextChunker) -> None:
    with pytest.raises(IllegalArgumentException):
        chunker.chunk_text("hello", 0)
    with pytest.raises(IllegalArgumentException):
        chunker.chunk_text("hello", -1)
