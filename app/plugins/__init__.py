"""Semantic Kernel plugins exposing app capabilities to the Knowledge Generative Agent."""

from __future__ import annotations

from .confluence_plugin import ConfluencePlugin
from .knowledge_plugin import KnowledgePlugin

__all__ = ["ConfluencePlugin", "KnowledgePlugin"]
