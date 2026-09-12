"""Tests for :mod:`app.rag.converters` (FloatArrayConverter.java)."""

from app.rag.converters import db_to_float_array, float_array_to_db


def test_to_db_formats_like_arrays_tostring() -> None:
    assert float_array_to_db([1.25, 2.5, 3.0]) == "[1.25, 2.5, 3.0]"


def test_to_db_none_and_empty() -> None:
    assert float_array_to_db(None) is None
    assert float_array_to_db([]) is None


def test_from_db_round_trip() -> None:
    data = "[1.25, 2.5, 3.0]"
    assert db_to_float_array(data) == [1.25, 2.5, 3.0]


def test_from_db_null_and_empty() -> None:
    assert db_to_float_array(None) == []
    assert db_to_float_array("") == []


def test_from_db_tolerates_spacing_and_missing_brackets() -> None:
    assert db_to_float_array("[1, 2]") == [1.0, 2.0]
