"""Equivalent of the Java ``FloatArrayConverter`` / ``FloatArrayCollector`` helpers.

The Java converter serialized a ``float[]`` with ``Arrays.toString`` (e.g.
``[1.25, 2.5, 3.75]``) and parsed that representation back into a float array. The
collector was a helper used during parsing. These functions preserve exactly the same
serialization format so that any existing data written by the Java application can be
read back.
"""

from __future__ import annotations


def float_array_to_db(values: list[float] | None) -> str | None:
    """Equivalent of ``FloatArrayConverter.convertToDatabaseColumn``."""
    if values is None or len(values) == 0:
        return None
    return "[" + ", ".join(str(float(v)) for v in values) + "]"


def db_to_float_array(db_data: str | None) -> list[float]:
    """Equivalent of ``FloatArrayConverter.convertToEntityAttribute``."""
    if db_data is None or db_data == "":
        return []
    inner = db_data[1:-1]
    if inner.strip() == "":
        return []
    collected: list[float] = []
    for token in inner.split(","):
        token = token.strip()
        if token:
            collected.append(float(token))
    return collected
