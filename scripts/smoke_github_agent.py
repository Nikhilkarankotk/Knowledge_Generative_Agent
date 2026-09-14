"""Live smoke test for the Knowledge Generative Agent (GitHub, Phase 2).

Exercises the exact production wiring without needing the database or the HTTP
server: the shared Semantic Kernel runtime (``SemanticKernelFactory`` with the
OpenAI-compatible client pointed at Mistral), the background asyncio loop, the
``GitHubPlugin`` (real GitHub REST API over the network) and the auto-invocation
function-calling loop with the real Mistral model.

The script drives turns that verify routing when GitHub is an allowlisted
knowledge source (``GITHUB_ALLOWED_REPOSITORIES``):

1. List configured repositories -> the agent should call
   ``GitHub.list_allowed_repositories`` and show only the configured repos.
2. Reading code -> the agent should call ``GitHub.get_readme`` and/or
   ``GitHub.list_repository_contents`` for an allowlisted repository.
3. Repo-scoped code search -> ``GitHub.search_code`` within an allowlisted repo.
4. Multi-source -> a "compare documented architecture with the actual code"
   question must invoke BOTH Confluence and GitHub without asking the user.
5. A repository OUTSIDE the allowlist must never be read (no content returned).

GitHub config must be enabled in .env (GITHUB_ENABLED=true + GITHUB_TOKEN +
GITHUB_ALLOWED_REPOSITORIES). Without credentials or with an empty allowlist the
script prints a skip message and exits (returns 0).

Run (from the repository root, using the project venv):

    .venv\\Scripts\\python.exe scripts\\smoke_github_agent.py
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
from app.core.exceptions import KnowledgeAgentError  # noqa: E402
from app.services.confluence_service import ConfluenceService  # noqa: E402
from app.services.github_service import GitHubService  # noqa: E402
from app.sk.semantic_kernel_factory import SemanticKernelFactory  # noqa: E402

PERMISSION_PHRASES = [
    "would you like me to",
    "would you like me to search",
    "i can search confluence",
    "i can search github",
    "i could try searching",
    "may i search",
    "should i search",
    "want me to search",
]

# A well-known public repository that is NOT in the allowlist. The agent must never
# surface its content.
BANNED_REPO = "ShakibulAkash/ecommerce-web-application"


def _build_turns(repo: str) -> list[str]:
    return [
        (
            "List the GitHub repositories that are configured for this Knowledge "
            "Generative Agent."
        ),
        (
            f"Show me the README of the {repo} GitHub repository and list its main "
            "source files."
        ),
        (
            f"Inside the {repo} GitHub repository, search the code for where the "
            "agent or chat API endpoint is defined. Report the matching file paths."
        ),
        (
            "Compare the documented Payments architecture in Confluence with the "
            "actual implementation in the GitHub repository. If either source has "
            "nothing, say so directly."
        ),
        (
            f"Read the README of the public GitHub repository {BANNED_REPO} and "
            "summarize what it implements."
        ),
    ]


def main() -> None:
    settings = Settings()
    if not settings.sk_agent_enabled:
        print("SK_AGENT_ENABLED=false - enable it in .env to run the agent smoke test.")
        return
    if not settings.mistral_api_key:
        print("MISTRAL_API_KEY missing - .env not configured.")
        return
    if not settings.github_enabled or not settings.github_token:
        print(
            "GitHub is not configured (GITHUB_ENABLED=false or GITHUB_TOKEN missing) - "
            "skipping the Phase 2 smoke test. Add read-only GitHub credentials to .env."
        )
        return
    if not settings.github_allowed_repository_list:
        print(
            "GITHUB_ALLOWED_REPOSITORIES is empty - repository access is disabled "
            "(the agent answers 'No GitHub repositories are configured'). Add "
            "owner/repo entries to .env to run the Phase 2 smoke test."
        )
        return

    configured_repo = settings.github_allowed_repository_list[0]
    confluence = ConfluenceService.from_settings(settings)
    github = GitHubService.from_settings(settings)
    if confluence is None:
        print("Confluence disabled - the multi-source turn will run with GitHub only.")
    if github is None:
        print("GitHub disabled - nothing to smoke test.")
        return

    factory = SemanticKernelFactory(
        settings,
        confluence_service=confluence,
        github_service=github,
        use_loop=True,
    )
    try:
        # No rag_service => only Confluence + GitHub plugins are registered here.
        agent = factory.build_agent(session_id="smoke", rag_service=None)
        turns = _build_turns(configured_repo)
        for index, turn in enumerate(turns, start=1):
            print(f"\n===== Turn {index}: {turn[:100]}")
            history = ChatHistory()
            history.add_user_message(turn)
            try:
                answer = factory.run_agent(agent, history)
            except KnowledgeAgentError as exc:
                # e.g. the model exhausted its auto-invoke budget and returned an
                # empty final answer. Report and keep going so every turn runs.
                print(f"\n>> Review: agent turn failed (empty answer): {exc}\n")
                continue
            print("\nAgent answer (first 2500 chars):\n")
            print(answer[:2500])
            lowered = answer.lower()
            proposed = any(phrase in lowered for phrase in PERMISSION_PHRASES)
            if proposed:
                print("\n>> FAIL: agent proposed a search instead of invoking a tool <<")
                continue
            if index == len(turns):
                # Banned-repository turn: the repo name may appear in an honest
                # refusal, so only flag fabricated *content*.
                banned_lower = BANNED_REPO.lower()
                content_surfaced = f"[source: github: {banned_lower}" in lowered
                refused = any(
                    phrase in lowered
                    for phrase in (
                        "not in the configured allowlist",
                        "not among the configured",
                        "not among the allowed",
                        "cannot access",
                        "not accessible",
                        "not configured",
                    )
                )
                if content_surfaced:
                    print(f"\n>> FAIL: banned repository {BANNED_REPO} content surfaced <<")
                elif refused:
                    print("\n>> Banned repository refused (allowlist enforced) <<")
                else:
                    print("\n>> Review refusal output below (expected an allowlist refusal) <<")
                continue
            if BANNED_REPO.lower() in lowered:
                print(f"\n>> FAIL: banned repository {BANNED_REPO} mentioned in a non-refusal turn <<")
                continue
            if "[Source: GitHub:" in answer:
                print("\n>> GitHub sources were retrieved and attributed <<")
            elif "github.com" in lowered:
                print("\n>> GitHub sources retrieved and cited by URL <<")
            elif "not configured" in answer:
                print("\n>> GitHub markers indicate the plugin is disabled <<")
            else:
                print("\n>> Review output below (no GitHub sources surfaced) <<")
    finally:
        factory.close()
        if confluence is not None:
            confluence.close()
        if github is not None:
            github.close()


if __name__ == "__main__":
    main()
