"""Live smoke test for the Knowledge Generative Agent (routing at scale).

Exercises the exact production wiring without needing the database or the HTTP
server: the shared Semantic Kernel runtime (``SemanticKernelFactory`` with the
OpenAI-compatible client pointed at Mistral), the background asyncio loop, the
``ConfluencePlugin`` (real Confluence over the network) and the auto-invocation
function-calling loop with the real Mistral model.

The script drives three turns that verify query routing against a (potentially
large) Confluence wiki:

1. Space discovery   -> the agent should call ``Confluence.list_spaces``.
2. Scoped search     -> the agent should call ``Confluence.search_pages`` with a
                        ``space_key`` (and possibly ``get_page``).
3. Acceptance       -> a technical "Payments application architecture" question
                        must invoke Confluence and then either answer from real
                        evidence or honestly report nothing found - it must NOT
                        ask "Would you like me to search Confluence?".

Run (from the repository root, using the project venv):

    .venv\\Scripts\\python.exe scripts\\smoke_knowledge_agent.py
"""

from __future__ import annotations

import logging
import sys

from semantic_kernel.contents import ChatHistory

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)

from app.core.config import Settings  # noqa: E402
from app.services.confluence_service import ConfluenceService  # noqa: E402
from app.sk.semantic_kernel_factory import SemanticKernelFactory  # noqa: E402

PERMISSION_PHRASES = [
    "would you like me to",
    "would you like me to search",
    "i can search confluence",
    "i could try searching",
    "may i search",
    "should i search",
    "want me to search",
]

TURNS = [
    "What Confluence spaces are available to me?",
    "In the Confluence space with key MFS, find the 'Meeting notes' template page and "
    "summarize what that template covers.",
    (
        "What are the main components and architecture of the Payments Application? "
        "If you searched and found nothing, say so directly."
    ),
]


def _marker(index: int, turn: str, answer: str) -> None:
    lowered = answer.lower()
    if turn.startswith("What Confluence spaces"):
        if any(key in answer for key in ("~7120", "MFS", "key=")):
            print("\n>> Turn 1: space discovery worked <<")
        else:
            print("\n>> Turn 1: no Confluence spaces surfaced - check Confluence config <<")
        return

    proposed = any(phrase in lowered for phrase in PERMISSION_PHRASES)
    if proposed:
        print("\n>> FAIL: agent proposed a search instead of invoking a tool <<")
        return

    if index == 2:
        if "[Source: Confluence:" in answer:
            print("\n>> Turn 2: scoped Confluence search used and attributed correctly <<")
        elif "MFS" in answer and any(
            word in lowered for word in ("meeting", "template", "action items", "notes")
        ):
            print(
                "\n>> Turn 2: routed to Confluence (scoped) and returned real page "
                "content, but the answer omitted the [Source:] marker <<"
            )
        elif "not find" in lowered or "no information" in lowered:
            print("\n>> Turn 2: agent honestly reported no results (broaden terms?). <<")
        else:
            print("\n>> Turn 2: unexpected answer shape (review output). <<")
        return

    if index == 3:
        if not proposed and (
            "payments" in lowered and ("architect" in lowered or "component" in lowered)
        ):
            print("\n>> Turn 3 PASS: answered honestly without proposing a search. <<")
        else:
            print("\n>> Turn 3: review output below. <<")


def main() -> None:
    settings = Settings()
    if not settings.sk_agent_enabled:
        print("SK_AGENT_ENABLED=false - enable it in .env to run the agent smoke test.")
        return
    if not settings.mistral_api_key:
        print("MISTRAL_API_KEY missing - .env not configured.")
        return

    confluence = ConfluenceService.from_settings(settings)
    if confluence is None:
        print("Confluence is disabled - agent will answer from general knowledge only.")

    factory = SemanticKernelFactory(settings, confluence_service=confluence, use_loop=True)
    try:
        # No rag_service => only the Confluence plugin is registered for this smoke test.
        agent = factory.build_agent(session_id="smoke", rag_service=None)
        for index, turn in enumerate(TURNS, start=1):
            print(f"\n===== Turn {index}: {turn[:100]}")
            history = ChatHistory()
            history.add_user_message(turn)
            answer = factory.run_agent(agent, history)
            print("\nAgent answer (first 2500 chars):\n")
            print(answer[:2500])
            _marker(index, turn, answer)
    finally:
        factory.close()
        if confluence is not None:
            confluence.close()


if __name__ == "__main__":
    main()
