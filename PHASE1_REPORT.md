# Phase 1 Report - Knowledge Generative Agent (Semantic Kernel integration)

Goal of Phase 1: integrate Semantic Kernel into the existing Python/FastAPI app to
power a Knowledge Generative Agent that answers from uploaded documents (session RAG)
and Confluence, with **no behavioural changes** to the existing pipeline.

## Summary

* Semantic Kernel 1.44.1 integrated as a shared, process-wide runtime.
* One agent: **KnowledgeGenerativeAgent**, with two capability tools behind the SK
  function-calling loop (auto-invocation).
* `ChatService` routes user turns through the agent when enabled, with a
  transparent fallback to the exact legacy augmented-prompt path on any agent
  failure (environment toggle `SK_AGENT_ENABLED`).
* PostgreSQL `chat_message` stays the source of truth; SK chat history is rebuilt
  from the DB every turn. Sessions are isolated by construction (per-request agents
  bound to a session id).
* 192 tests pass (132 pre-existing + 60 new), `ruff check` and `mypy` clean.
* Live-validated end-to-end: real Mistral + real Confluence (see **Live routing
  validation** below).

## Files

### New production modules
| File | Responsibility |
| --- | --- |
| `app/services/confluence_service.py` | Confluence REST client (CQL search with low-recall broadening + optional `space_key` scoping, `list_spaces`, page fetch, HTML->text, auth, error mapping to `ConfluenceApiError`) |
| `app/plugins/knowledge_plugin.py` | `KnowledgePlugin.search_knowledge(query)` -> session `RagService` |
| `app/plugins/confluence_plugin.py` | `ConfluencePlugin.search_pages(query, limit, space_key)` / `get_page(page_id)` / `list_spaces()` |
| `app/agents/knowledge_generative_agent.py` | `KnowledgeGenerativeAgent` + system instructions (no hard-coded keyword->tool maps) |
| `app/sk/semantic_kernel_factory.py` | Shared `Kernel`/LLM chat service + background asyncio loop thread + per-request agent builder + `run_agent` |
| `app/sk/compat.py` | Python 3.14 + SK 1.44 `_parse_parameter` monkeypatch (idempotent) |
| `app/sk/agent_runner.py` | `collect_agent_answer` - final assistant text from an agent turn |
| `app/sk/chat_history_builder.py` | Translate `chat_message` rows into SK `ChatHistory` |

### Modified
`app/api/dependencies.py` (agent/confluence singletons + wiring), `app/core/config.py`
(SK + Confluence settings), `app/core/exceptions.py` (`ConfluenceApiError`,
`KnowledgeAgentError`), `app/services/chat_service.py` (agent path + fallback),
`pyproject.toml` (`semantic-kernel>=1.44.0`, mypy target 3.14), `.env.example`
(`SK_*`, `CONFLUENCE_*`), `tests/conftest.py` (`SK_AGENT_ENABLED=false`).

### Tests (new)
`tests/fake_sk_service.py` (scripted `ChatCompletionClientBase`), `test_confluence_service.py`,
`test_knowledge_plugin.py`, `test_confluence_plugin.py`, `test_knowledge_generative_agent.py`,
`test_chat_service_agent.py` (agent path, DB-fed history, legacy fallback, session isolation,
API-level override).

## Live routing validation (real Mistral + real Confluence)

`scripts/smoke_knowledge_agent.py` drives two turns with no DB/server:

1. **Space discovery** - "What Confluence spaces are available to me?" -> the model
   calls `Confluence.list_spaces` and answers with the real space keys/names.
2. **Scoped search** - "In the Confluence space with key MFS, find the 'Meeting notes'
   template page and summarize what that template covers." -> the model routes to
   Confluence, scopes `search_pages(space_key="MFS")` and summarizes the real template.

This exercises exactly the routing behaviour needed when a wiki holds hundreds of pages:
discover the authoritative space first, then scope, then (optionally) drill in with
`get_page` - the model never has to guess which space a topic lives in. The system
prompt forbids claiming a space/page does not exist without an actual current-turn
search, which stops the model from answering from assumptions.

## Architecture

```
POST /api/chat
  -> ChatService.process_user_message          (user msg saved once to chat_message)
     if sk_agent_enabled and factory present:
        KnowledgeGenerativeAgent turn (try/except fallback to legacy path below)
           build_agent_chat_history(from chat_message rows)
           SemanticKernelFactory.run_agent(agent, history)   [shared asyncio loop]
              ChatCompletionAgent.invoke(FunctionChoiceBehavior.Auto())
                 KnowledgePlugin.search_knowledge -> RagService.retrieve_context(query, session_id)
                 ConfluencePlugin.search_pages/get_page -> ConfluenceService
           collect_agent_answer -> final text
        save assistant msg + exchange in memory
     else fallback: legacy augmented-prompt path (unchanged, exact Java-equivalent prompt)
```

* `SemanticKernelFactory` owns the expensive OpenAI-compatible chat service
  (`AsyncOpenAI` -> `Mistral_BASE_URL`, `OpenAIChatCompletion`) and a single asyncio
  loop thread; agents run via `run_coroutine_threadsafe`. `confluence_service` is a
  shutdown-injected client; `close()` stops the loop.
* Per request a cheap `Kernel()` + session-bound plugins + agent are built.
* Credentials never reach the model; failures become honest markers in tool results.
* Attribution preserved (`[Source: filename]`, `[Source: Confluence: <title>]` + URL).

## Integration points
* `app/services/chat_service.py::process_user_message` (agent path with fallback).
* `app/api/dependencies.py::_build_singletons` (factory + ConfluenceService when enabled).
* New settings (`.env`): `SK_AGENT_ENABLED`, `SK_MAX_AUTO_INVOKE_ATTEMPTS`,
  `SK_AGENT_TIMEOUT_SECONDS`, `CONFLUENCE_ENABLED`, `CONFLUENCE_BASE_URL`,
  `CONFLUENCE_API_TOKEN`, `CONFLUENCE_USERNAME`, `CONFLUENCE_LIMIT`,
  `CONFLUENCE_TIMEOUT_SECONDS`, `CONFLUENCE_PAGE_CHAR_LIMIT`.

## Tests / results
* `pytest`: **180 passed** (132 pre-existing regression suite on the legacy path +
  48 new). Function-calling loop, argument parsing, tool results, multi-tool rounds,
  session isolation (real RAG over SQLite), failures, attribution, legacy fallback
  and the shared-loop production path are covered.
* `ruff check app tests scripts`: clean. `mypy app`: clean (57 source files).

## Issues
1. **Python 3.14 + SK 1.44.1**: `_parse_parameter` crashes iterating `member_descriptor`
   `__args__` on `typing.Union`/`types.UnionType`. Worked around with an idempotent
   patch (`app/sk/compat.py`) applied before any `@kernel_function` class is defined
   (import time). Track upstream SK 3.14 support.
2. **numpy stubs require Python >= 3.12** (SK transitive dep): mypy's
   `python_version` bumped 3.11 -> 3.14.
3. Pydantic `validate_assignment` quirks on the test fake (solved with
   `PrivateAttr`/`object.__setattr__`) - test-only, no production impact.
4. **Mistral + SK message names**: `ChatCompletionAgent` stamps the agent name onto the
   system message and every authored assistant message; the Mistral API rejects the
   extra `name` field on `system` messages (`extra_forbidden`, HTTP 422). Fixed by
   monkey-patching the service's `_prepare_chat_history_for_request` (the single
   request-serialization point) to drop `name` from all non-`tool` messages
   (`apply_mistral_compat` in `app/sk/compat.py`). Verified live end-to-end
   (`scripts/smoke_knowledge_agent.py`): real Mistral selected `Confluence-search_pages`
   and answered with real page links.
5. **Agent proposing Confluence searches instead of invoking the tool**: with
   auto-invocation already enabled (`FunctionChoiceBehavior.Auto()`), the model
   could still answer "I can search Confluence... would you like me to proceed?"
   as plain text. Fixed without touching the RAG or Confluence APIs:
   * System prompt now explicitly authorises automatic tool calls and forbids
     asking permission or proposing searches.
   * `search_pages`/`search_knowledge`/`list_spaces` descriptions expanded so the
     model sees the exact technical/enterprise topics and the automatic-invocation
     mandate.
   * The configured `sk_max_auto_invoke_attempts` (10) is now actually passed to
     `FunctionChoiceBehavior.Auto(maximum_auto_invoke_attempts=...)`; the SK default
     was 5, which could end a progressive search mid-loop.
   * Progressive search: the low-recall broaden now also matches page titles
     (`title ~ "..."`), so page hierarchies ("Payments Application" -> subpages)
     resolve without hard-coded application names.
   * INFO logging in both plugins records every tool invocation/result count (no
     credentials) so a run shows exactly which tool the agent chose.
   Live-validated: for "What are the main components and architecture of the
   Payments Application?" the agent invoked `Confluence.search_pages` three times
   (scoped phrase -> scoped broad -> unscoped broad) automatically, with no
   permission prompt; when no matching page existed it said so honestly.
6. Repo is public on GitHub; `.env` is gitignored and must stay out before any push.

---

# Phase 2 Report - GitHubPlugin (read-only GitHub knowledge source)

Goal of Phase 2: add GitHub as a first-class knowledge source for the same
Knowledge Generative Agent - source code, READMEs, repository files and issues -
**read-only**, preserving all Phase 1 behaviour. Routing stays semantic and
agent-driven (Semantic Kernel function calling); no hard-coded keyword maps.

## Summary

* New `GitHubService` (owns GitHub REST API communication) + `GitHubPlugin`
  (exposes semantic functions), following the exact Confluence-Phase-1 pattern.
* Seven read-only tools: `list_allowed_repositories`, `get_repository`, `get_readme`,
  `list_repository_contents`, `get_file_content`, `search_code`, `get_issue`.
* Repository access is locked to an allowlist (`GITHUB_ALLOWED_REPOSITORIES`),
  enforced in `GitHubService` *before* any API call: the unrestricted
  `/search/repositories` and unqualified `/search/code` endpoints were removed,
  `search_code` is scoped to a single allowlisted repo (with a `repo:owner/name`
  qualifier), and lists of repos show only the configured repositories. An empty
  allowlist disables repository access with a controlled "No GitHub repositories
  are configured for this Knowledge Generative Agent" message - no fallback to
  global or arbitrary public repositories.
* Registered as the `GitHub` plugin on every agent; system instructions now list
  GitHub alongside KnowledgePlugin/ConfluencePlugin with routing rules
  ("how is this implemented?" -> GitHub, "compare docs with code" -> Confluence +
  GitHub). No per-request secrets, no write operations anywhere.
* Auth lives in the service only (`Authorization: Bearer <token>`), never surfaced
  to the LLM or logs; failure markers keep the auto-invocation loop alive.
* 250 tests pass (192 pre-existing + 58 new incl. allowlist security tests), `ruff check`
  and `mypy` clean.
* Live-validated end-to-end (real Mistral + real GitHub REST API + real Confluence,
  credentials kept in the gitignored `.env`): the agent automatically invoked
  `GitHub.list_allowed_repositories` / `get_readme` / `list_repository_contents`,
  and on a
  "compare documented architecture with the actual code" question invoked BOTH
  Confluence and GitHub and synthesized a comparison - with no permission prompts.
  `scripts/smoke_github_agent.py`, degrades to a clean skip when credentials are
  absent.
* Live allowlist enforcement: asking about `ShakibulAkash/ecommerce-web-application`
  (not in the allowlist) produced `GitHub request rejected:
  repository='ShakibulAkash/ecommerce-web-application' reason=not_in_allowlist`
  with no API call and the agent answered with an explicit refusal; a
  model-hallucinated repo name (`...Knowledge_Generative_AAgent`) was also
  rejected before any request.
* Attribution wording hardened: tool results must be cited with the verbatim
  `[Source: GitHub: <owner/repo>[:<path>]]` / `[Source: Confluence: <title>]` /
  `[Source: <filename>]` tags (not hyperlinks only), verified live.

## Files

### New production modules
| File | Responsibility |
| --- | --- |
| `app/services/github_service.py` | Read-only GitHub REST client: allowlist-locked repo listing (`list_allowed_repositories`), repo metadata, base64 README+file decode, contents listing, allowlist-scoped code search, issues; Bearer auth; allowlist enforcement before every request; error mapping to `GitHubApiError`/`GitHubRepositoryNotAllowedError`; cap/truncation |
| `app/plugins/github_plugin.py` | `GitHubPlugin` with 7 `@kernel_function`s behind the auto-invocation loop; "not configured"/failure/allowlist-rejection markers; INFO invocation/result logging |

### Modified
`app/core/config.py` (`github_*` settings, disabled by default; new
`GITHUB_ALLOWED_REPOSITORIES` allowlist setting + normalization),
`app/core/exceptions.py` (`GitHubApiError` + new `GitHubRepositoryNotAllowedError`),
`app/agents/knowledge_generative_agent.py` (GitHubPlugin in instructions +
routing rules), `app/sk/semantic_kernel_factory.py` (GitHubPlugin registration +
`github_service` param), `app/api/dependencies.py` (`GitHubService` singleton +
factory wiring), `.env.example`, `PHASE1_REPORT.md`

### Tests added
* `tests/test_github_service.py` - MockTransport: allowlist enforcement
  (unconfigured repo rejected *before* any API call, `ShakibulAkash/...` never
  returned, empty-allowlist controlled message, case-insensitive + URL/`.git`
  normalization, multi-repo scoping), `list_allowed_repositories` never hits the
  network, `search_code` is repo-qualified only, auth headers, attributed
  formatting, base64 decode + truncation, rate-limit/error mapping, `from_settings`.
* `tests/test_config_github.py` - `GITHUB_ALLOWED_REPOSITORIES` parsing: trimming,
  case-insensitive de-duplication, empty-entry handling, URL/`.git` normalization.
* `tests/test_github_plugin.py` - argument forwarding (`list_allowed_repositories`
  no args, `search_code` repository+query), disabled/failure markers, allowlist
  wording in every description, invocation logging.
* `tests/test_knowledge_generative_agent.py` - GitHub registered under expected
  name/functions (no `search_repositories`); routing "how is Payments implemented"
  -> `list_allowed_repositories`, repo-scoped code-search routing, compare-architecture
  -> Confluence + GitHub, get_readme follow-up, GitHub failure/unconfigured markers,
  instructions require the allowlist wording and forbid global search.

### Allowlist enforcement (security fix)
Root cause: Phase 2 exposed two GitHub endpoints scoped globally by the API
(`GET /search/repositories`, `GET /search/code`) plus arbitrary per-repo reads, so a
fine-grained PAT restricted to selected repos could still be used to read *any*
public repository (e.g. `ShakibulAkash/ecommerce-web-application`).

Fix (defense in depth, authorisation before every request):
* `GITHUB_ALLOWED_REPOSITORIES` (comma-separated `owner/repo`, normalized/deduped)
  is enforced in `GitHubService._require_allowed` before any HTTP request; rejected
  repos raise `GitHubRepositoryNotAllowedError` with the reason and never hit the API.
* Global repo search removed; `list_allowed_repositories` returns only the configured
  repos (no network call). Empty allowlist <-> "No GitHub repositories are configured
  for this Knowledge Generative Agent." and every repo operation is refused.
* `search_code` now takes `(repository, query)`; the repo must be allowlisted and the
  GitHub query always carries a `repo:owner/name` qualifier.
* Plugin/instructions/descriptions only advertise allowlisted repos; safe logs like
  `GitHub request rejected: repository=... reason=not_in_allowlist` contain no token.

## Next steps
* Rotate the GitHub fine-grained PAT (it was pasted in chat) via GitHub ->
  Settings -> Developer settings; update `GITHUB_TOKEN` in the gitignored `.env`.
  Never commit `.env`.
* Pagination/rate-limit backoff on GitHub repository/code search at scale.
* GitHub Enterprise base-URL validation tests.
* Phase 3: SharePointPlugin - after GitHub is validated live with credentials.
* Later phases: WebSearchPlugin, DeploymentPlugin (per roadmap).