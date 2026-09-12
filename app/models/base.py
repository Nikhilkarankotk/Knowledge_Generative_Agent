"""Declarative base shared by all models."""

from sqlalchemy import BigInteger, Integer
from sqlalchemy.orm import DeclarativeBase

# Identical to Java's BIGINT identity primary keys; on SQLite it must map to INTEGER
# (which is 64-bit) so that autoincrement works during tests.
BigIntIdentity = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass
