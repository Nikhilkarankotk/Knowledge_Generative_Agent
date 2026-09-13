"""Compatibility patches for Python 3.14 + Semantic Kernel 1.44.

``semantic_kernel.functions.kernel_function_decorator._parse_parameter`` recurses
into ``param.__origin__``/``param.__args__`` to describe annotations. On Python 3.14
the ``typing.Union`` and ``types.UnionType`` *classes* now expose ``__args__`` as a
``member_descriptor`` instead of raising AttributeError, so ``hasattr(param, "__args__")``
is True and the parser attempts to iterate the descriptor, throwing
``TypeError: 'member_descriptor' object is not iterable``. This breaks the internal
``@kernel_function`` decoration applied by ``ChatCompletionAgent`` during
``model_post_init``.

The patch guards those marker classes and delegates everything else to the original
implementation. It is idempotent and only replaces the private parser function.
"""

from __future__ import annotations

import functools
import types
import typing
from typing import Any

import semantic_kernel.functions.kernel_function_decorator as _decorator

_PATCHED = False


def apply_py314_compatibility_patch() -> None:
    """Apply the Python 3.14 / Semantic Kernel signature-parser workaround once."""
    global _PATCHED
    if _PATCHED:
        return

    original = _decorator._parse_parameter

    @functools.wraps(original)
    def _parse_parameter(name: str, param: object, default: object = None) -> dict[str, object]:
        if param is typing.Union or param is types.UnionType:
            return {
                "name": name,
                "type_": "Union",
                "type_object": param,
                "is_required": default is None,
            }
        return original(name, param, default)

    _decorator._parse_parameter = _parse_parameter
    _PATCHED = True


def drop_names_except_tool(
    messages: list[dict[str, Any]], role_key: str = "role"
) -> list[dict[str, Any]]:
    """Remove the ``name`` key from non-tool request messages, in place.

    ``ChatCompletionAgent`` stamps the agent name onto the system message and every
    authored assistant message. The Mistral API rejects the extra ``name`` field on
    ``system`` messages (``extra_forbidden``). Tool messages must keep their ``name``
    because it is the function name the tool result belongs to.
    """
    for message in messages:
        if message.get(role_key) != "tool":
            message.pop("name", None)
    return messages


def apply_mistral_compat(service: Any) -> None:
    """Monkey-patch an ``OpenAIChatCompletion`` service so per-message ``name``
    fields are omitted from the wire payload except on ``tool`` messages.

    The patch wraps ``_prepare_chat_history_for_request``, the single method used to
    build the ``messages`` list sent to the model (see
    ``OpenAIChatCompletionBase._inner_get_chat_message_contents``), so both the agent
    system prompt and any tool-calling follow-up rounds are covered.
    """
    original = service._prepare_chat_history_for_request

    def _prepare(
        self: Any,
        chat_history: Any,
        role_key: str = "role",
        content_key: str = "content",
    ) -> list[dict[str, Any]]:
        return drop_names_except_tool(original(chat_history, role_key, content_key), role_key)

    service._prepare_chat_history_for_request = types.MethodType(_prepare, service)
