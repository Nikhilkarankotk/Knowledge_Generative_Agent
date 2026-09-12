"""Logging configuration.

The Java application mostly relies on SLF4J logging plus a few ``System.out`` /
``System.err`` prints in the RAG pipeline. This module reproduces the same level of
observability using the standard :mod:`logging` module.
"""

from __future__ import annotations

import logging
import sys

from app.core.config import Settings

_CONFIGURED = False


def configure_logging(settings: Settings) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    )
    root.addHandler(handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
