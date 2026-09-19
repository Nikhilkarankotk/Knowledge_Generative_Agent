"""Tests for automatic knowledge-source routing (source planner + pre-retrieval).

Covers the 14 routing scenarios required by the agent-routing specification:
per-source selection, multi-source selection, explicit user overrides, evidence
merging with attribution, and resilience when one selected source is empty or
fails while another succeeds.
"""

from __future__ import annotations

import json

from semantic_kernel.contents import ChatHistory

from app.core.config import Settings
from app.core.exceptions import ConfluenceApiError, GitHubApiError, SharePointApiError
from app.sk.semantic_kernel_factory import SemanticKernelFactory
from app.sk.source_planner import (
    LLMSourcePlanner,
    NoOpSourcePlanner,
    detect_explicit_sources,
    heuristic_sources,
    parse_source_plan,
)
from app.sk.source_router import (
    SourceRouter,
    _parse_allowed_repositories,
    _repository_matches,
)
from tests.fake_sk_service import ScriptedChatCompletion, received_text

ALL_SOURCES = ["knowledge", "confluence", "sharepoint", "github"]


# --- GitHub pre-retrieval: repository listing + matching -------------------------

_BULLET_LISTING = (
    "[Source: GitHub] Configured repositories for this Knowledge Generative Agent:\n"
    "- nikhilkarankotk/E-commerce_Application\n"
    "- nikhilkarankotk/Job_Portal_Web_Application\n"
    "- nikhilkarankotk/express\n"
    "- nikhilkarankotk/n8n\n"
)


def test_parse_allowed_repositories_reads_bullet_listing() -> None:
    """Regression: the router parsed only '[Source: GitHub: owner/repo]' lines,
    so the real '- owner/repo' listing yielded 0 repositories and GitHub was
    silently skipped (missing GitHub/ folder in the export)."""
    assert _parse_allowed_repositories(_BULLET_LISTING) == [
        "nikhilkarankotk/E-commerce_Application",
        "nikhilkarankotk/Job_Portal_Web_Application",
        "nikhilkarankotk/express",
        "nikhilkarankotk/n8n",
    ]
    # The legacy per-repo attribution format still works.
    assert _parse_allowed_repositories("[Source: GitHub: acme/payments]\nURL: x") == ["acme/payments"]


def test_repository_matches_multi_word_names_split_on_separators() -> None:
    q = (
        "retrieve the CICD architecture of n8n from the document. and also retrieve job "
        "portal web application architecture, functionality workflow"
    )
    assert _repository_matches("nikhilkarankotk/Job_Portal_Web_Application", q)
    assert _repository_matches("nikhilkarankotk/n8n", q)
    assert not _repository_matches("nikhilkarankotk/E-commerce_Application", q)
    # Generic single-word repos must appear as a whole word, not a substring.
    assert not _repository_matches("nikhilkarankotk/express", "explain the expression parser")
    assert _repository_matches("nikhilkarankotk/express", "how does the express app route?")


class _ListingGitHubPlugin:
    def __init__(self) -> None:
        self.retrieved: list[str] = []

    def list_allowed_repositories(self) -> str:
        return _BULLET_LISTING

    def retrieve_repository_contents(self, repo: str) -> str:
        self.retrieved.append(repo)
        return f"[Source: GitHub: {repo}:README.md]\ncontents"


def test_router_retrieves_every_repository_named_in_the_question() -> None:
    plugin = _ListingGitHubPlugin()
    text = SourceRouter()._retrieve_github(
        plugin, "n8n CICD architecture and the job portal web application workflow"
    )
    assert plugin.retrieved == [
        "nikhilkarankotk/Job_Portal_Web_Application",
        "nikhilkarankotk/n8n",
    ]
    assert "[Source: GitHub: nikhilkarankotk/n8n:README.md]" in text


def test_router_does_not_guess_a_repository_when_none_is_named() -> None:
    plugin = _ListingGitHubPlugin()
    SourceRouter()._retrieve_github(plugin, "What is our deployment policy?")
    assert plugin.retrieved == []  # never export an unrelated first-alphabetical repo


def test_rendered_evidence_neutralizes_prompt_template_syntax() -> None:
    """Regression: a GitHub Actions workflow containing '${{ inputs.node-version }}'
    in the pre-fetched evidence made Semantic Kernel fail the whole turn with
    'Failed to tokenize code block'. The evidence must not be parsed as a template."""
    from app.sk.source_planner import SourceSelection
    from app.sk.source_router import PrefetchResult, neutralize_template_syntax, render_evidence

    raw = "node-version: ${{ inputs.node-version }}\nname: {{ .Release.Name }}"
    assert "{{" not in neutralize_template_syntax(raw)
    assert "}}" not in neutralize_template_syntax(raw)
    # The human-visible characters are unchanged (only a zero-width space is added).
    assert neutralize_template_syntax(raw).replace("\u200b", "") == raw

    result = PrefetchResult(evidence=[("github", raw)], used=["github"])
    rendered = render_evidence(result, [SourceSelection("github", "test")])
    assert "{{" not in rendered and "}}" not in rendered
    assert "inputs.node-version" in rendered


# --- stubs ---------------------------------------------------------------------


class RecordingRag:
    def __init__(self, context: str = "[Source: notes.md]\nuploaded content", *, fail: bool = False) -> None:
        self._context = context
        self._fail = fail
        self.calls: list[tuple[str, str]] = []

    def is_knowledge_base_empty(self, session_id: str) -> bool:
        return not self._context

    def retrieve_context(self, query: str, session_id: str) -> str:
        if self._fail:
            raise RuntimeError("rag is down")
        self.calls.append((query, session_id))
        return self._context


class StubConfluence:
    enabled = True

    def __init__(self, output: str | None = None, *, fail: bool = False) -> None:
        self._output = output if output is not None else (
            "[Source: Confluence: Payments Architecture (space: PAY)]\n"
            "Page id: DOC1\n"
            "URL: https://wiki.example.com/spaces/PAY/pages/DOC1\n"
            "Excerpt: The Payments application uses a services architecture."
        )
        self._fail = fail
        self.searched: list[str] = []

    def search(self, query: str, limit: int | None = None, space_key: str | None = None) -> str:
        if self._fail:
            raise ConfluenceApiError("Confluence is down")
        self.searched.append(query)
        return self._output

    def list_spaces(self, limit: int = 50) -> str:
        return "Available Confluence spaces:\n[Source: Confluence] key=PAY, name=Payments"

    def get_page(self, page_id: str) -> str:
        return f"[Source: Confluence: page {page_id}]\nhttps://wiki.example.com/{page_id}\ncontent"

    def close(self) -> None:
        pass


EMPTY_CONFLUENCE = (
    "No Confluence pages matched this query. This is not proof that Confluence has "
    "no relevant documentation."
)


class StubGitHub:
    enabled = True

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def list_allowed_repositories(self) -> str:
        if self._fail:
            raise GitHubApiError("GitHub is down")
        return "[Source: GitHub: acme/payments]\nURL: https://github.com/acme/payments\nLanguage: Python"

    def retrieve_repository_contents(
        self, repo: str, max_items: int = 60, max_chars: int = 25_000
    ) -> str:
        if self._fail:
            raise GitHubApiError("GitHub is down")
        return (
            f"[Source: GitHub: {repo}:src/pay.py]\n"
            f"URL: https://github.com/{repo}/blob/main/src/pay.py\n"
            "Snippet: def charge_payment"
        )

    def close(self) -> None:
        pass


class StubSharePoint:
    enabled = True

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.searched: list[str] = []

    def search_file_content(self, query: str, limit: int | None = None) -> str:
        if self._fail:
            raise SharePointApiError("SharePoint is down")
        self.searched.append(query)
        return (
            "[Source: SharePoint: Onboarding Process.docx]\n"
            "URL: https://contoso.sharepoint.com/onboarding.docx\n"
            "New hires are onboarded through the SharePoint site."
        )

    def close(self) -> None:
        pass


# --- planner fakes -------------------------------------------------------------


class QuestionPlannerCompletion:
    """Returns canned planner JSON keyed by a substring of the question."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._mapping = mapping

    def __call__(self, prompt: str) -> str:
        for question, sources in self._mapping.items():
            if question in prompt:
                return json.dumps({"sources": [{"type": s, "reason": question} for s in sources]})
        return '{"sources": []}'


def planning(sources: list[str]) -> LLMSourcePlanner:
    """A planner that always returns ``sources`` regardless of the question."""
    return LLMSourcePlanner(complete=lambda prompt: json.dumps({"sources": [{"type": s} for s in sources]}))


def make_factory(planner, *, plan=None):
    fake = ScriptedChatCompletion(plan=plan or [], answerer=received_text)
    return SemanticKernelFactory(Settings(), chat_service=fake, source_planner=planner, use_loop=False)


def evidence_only(history: ChatHistory) -> str:
    """Answerer that returns only the injected evidence block, not the instructions."""
    text = received_text(history)
    marker = "=== RETRIEVED EVIDENCE ==="
    end_marker = "=== END RETRIEVED EVIDENCE ==="
    start = text.find(marker)
    end = text.find(end_marker)
    if start == -1 or end == -1:
        return text
    return text[start : end + len(end_marker)]


def run_turn(factory, user_message, *, rag=None, confluence=None, github=None, sharepoint=None) -> str:
    history = ChatHistory()
    history.add_user_message(user_message)
    return factory.run_turn(
        user_message=user_message,
        chat_history=history,
        rag_service=rag,
        session_id="s1",
        confluence_service=confluence,
        github_service=github,
        sharepoint_service=sharepoint,
    )


# --- planner unit tests: per-source selection ----------------------------------


def test_planner_confluence_plan_also_searches_sharepoint() -> None:
    """Confluence and SharePoint hold DIFFERENT documents (e.g. CI/CD pipeline
    PDFs only exist in SharePoint), so a documentation plan that names only one
    of them is widened to both - otherwise SharePoint evidence is silently missed."""
    planner = planning(["confluence"])
    selections = planner.plan("Payments architecture?", ALL_SOURCES)
    assert [s.type for s in selections] == ["confluence", "sharepoint"]
    assert "either Confluence or SharePoint" in selections[1].reason


def test_planner_sharepoint_plan_also_searches_confluence() -> None:
    planner = planning(["sharepoint"])
    assert [s.type for s in planner.plan("Onboarding process?", ALL_SOURCES)] == [
        "sharepoint",
        "confluence",
    ]


def test_planner_confluence_only_when_sharepoint_not_registered() -> None:
    planner = planning(["confluence"])
    available = [s for s in ALL_SOURCES if s != "sharepoint"]
    assert [s.type for s in planner.plan("Payments architecture?", available)] == ["confluence"]


def test_planner_pairing_leaves_non_documentation_plans_alone() -> None:
    from app.sk.source_planner import SourceSelection, pair_documentation_sources

    only_code = [SourceSelection("github", "x")]
    assert pair_documentation_sources(only_code, ALL_SOURCES) == only_code
    assert pair_documentation_sources([], ALL_SOURCES) == []


def test_explicit_upload_reference_does_not_suppress_other_sources() -> None:
    """Regression: "retrieve the CI/CD architecture of the attached document, and
    also the Job Portal architecture ... across our connected knowledge source"
    matched the explicit 'knowledge' alias and short-circuited the planner, so
    only the upload was searched - the GitHub/ folder vanished from the export."""
    q = (
        "retrieve the CICD architecture of attached document. and also retrieve job "
        "portal web application architecture, functionality workflow, analysis report "
        "and relevant data available across our connected knowledge source"
    )
    planner = planning(["confluence", "sharepoint", "github"])  # what the model would add
    types = [s.type for s in planner.plan(q, ALL_SOURCES)]
    assert types[0] == "knowledge"  # the explicit upload is kept first
    assert set(types) == {"knowledge", "confluence", "sharepoint", "github"}


def test_pure_explicit_request_is_still_honored_verbatim() -> None:
    """A plain "Search GitHub for X" must not be widened by the model plan."""
    planner = planning(["confluence", "sharepoint"])  # model would (wrongly) add these
    assert [s.type for s in planner.plan("Search GitHub for the charge function", ALL_SOURCES)] == [
        "github"
    ]


def test_explicit_upload_plus_architecture_adds_github_even_if_model_omits_it() -> None:
    from app.sk.source_planner import _asks_beyond_explicit_sources, detect_explicit_sources

    q = "read the attached document and explain the job portal web application architecture"
    assert detect_explicit_sources(q, ALL_SOURCES) == ["knowledge"]
    assert _asks_beyond_explicit_sources(q, ["knowledge"])
    planner = planning(["confluence"])  # model forgot github
    types = [s.type for s in planner.plan(q, ALL_SOURCES)]
    assert "github" in types and "knowledge" in types


def test_simple_upload_question_stays_upload_only() -> None:
    from app.sk.source_planner import detect_explicit_sources

    q = "Read my uploaded document and summarize it"
    assert detect_explicit_sources(q, ALL_SOURCES) == ["knowledge"]
    planner = planning(["confluence", "github"])  # would be wrong to add
    assert [s.type for s in planner.plan(q, ALL_SOURCES)] == ["knowledge"]


def test_planner_selects_github_only() -> None:
    planner = planning(["github"])
    assert [s.type for s in planner.plan("How is charge implemented?", ALL_SOURCES)] == ["github"]


def test_planner_selects_uploaded_documents_only() -> None:
    planner = planning(["knowledge"])
    assert [s.type for s in planner.plan("Summarize my PDF", ALL_SOURCES)] == ["knowledge"]


def test_planner_selects_confluence_and_github() -> None:
    planner = planning(["confluence", "github"])
    # Confluence in the plan pulls in its documentation sibling, SharePoint.
    assert [s.type for s in planner.plan("Compare docs and code", ALL_SOURCES)] == [
        "confluence",
        "github",
        "sharepoint",
    ]


def test_planner_selects_confluence_and_sharepoint() -> None:
    planner = planning(["confluence", "sharepoint"])
    assert [s.type for s in planner.plan("Docs and onboarding", ALL_SOURCES)] == [
        "confluence",
        "sharepoint",
    ]


def test_planner_selects_github_and_sharepoint() -> None:
    planner = planning(["github", "sharepoint"])
    # SharePoint in the plan pulls in its documentation sibling, Confluence.
    assert [s.type for s in planner.plan("Code and policy", ALL_SOURCES)] == [
        "github",
        "sharepoint",
        "confluence",
    ]


def test_planner_selects_three_sources() -> None:
    planner = planning(["confluence", "sharepoint", "github"])
    assert [s.type for s in planner.plan("Full picture", ALL_SOURCES)] == [
        "confluence",
        "sharepoint",
        "github",
    ]


def test_planner_accepts_zero_sources() -> None:
    planner = planning([])
    assert planner.plan("Just chatting", ALL_SOURCES) == []


# --- planner unit tests: overrides, validation, fallback -----------------------


def test_explicit_github_request_overrides_model_plan() -> None:
    planner = LLMSourcePlanner(complete=lambda prompt: '{"sources": [{"type": "confluence"}]}')
    assert [s.type for s in planner.plan("Search GitHub for the payments code", ALL_SOURCES)] == [
        "github"
    ]


def test_explicit_multi_source_request_selects_both() -> None:
    planner = LLMSourcePlanner(complete=lambda prompt: '{"sources": []}')
    plan = planner.plan("Search Confluence and GitHub for the architecture", ALL_SOURCES)
    assert [s.type for s in plan] == ["confluence", "github"]


def test_detect_explicit_sources_requires_an_action() -> None:
    assert detect_explicit_sources("What is in GitHub?", ALL_SOURCES) == []


def test_planner_drops_unknown_and_unavailable_sources() -> None:
    assert parse_source_plan('{"sources": [{"type": "teams"}, {"type": "github"}]}', ALL_SOURCES) == [
        "github"
    ]
    assert parse_source_plan('{"sources": [{"type": "github"}]}', ["confluence"]) == []


def test_planner_parses_fenced_json_and_reason() -> None:
    raw = '```json\n{"sources": [{"type": "confluence", "reason": "architecture"}]}\n```'
    assert parse_source_plan(raw, ALL_SOURCES) == ["confluence"]


def test_planner_falls_back_to_keywords_on_invalid_output() -> None:
    planner = LLMSourcePlanner(complete=lambda prompt: "not json at all")
    assert [s.type for s in planner.plan("Where is the onboarding policy?", ALL_SOURCES)] == [
        "sharepoint"
    ]


def test_heuristic_defaults_to_uploaded_documents_when_nothing_matches() -> None:
    assert heuristic_sources("hello there", ALL_SOURCES) == ["knowledge"]


# --- end-to-end routing --------------------------------------------------------


def test_multi_source_prefetch_merges_evidence_with_attribution() -> None:
    confluence = StubConfluence()
    github = StubGitHub()
    factory = make_factory(planning(["confluence", "github"]))

    answer = run_turn(
        factory,
        "Compare the documented Payments architecture with the actual code.",
        confluence=confluence,
        github=github,
    )

    assert confluence.searched == ["Compare the documented Payments architecture with the actual code."]
    assert "[Source: Confluence: Payments Architecture" in answer
    assert "[Source: GitHub: acme/payments:src/pay.py]" in answer
    assert "SOURCES USED: confluence, github" in answer


def test_empty_source_does_not_suppress_successful_source() -> None:
    confluence = StubConfluence(EMPTY_CONFLUENCE)
    github = StubGitHub()
    factory = make_factory(planning(["confluence", "github"]))

    answer = run_turn(factory, "Compare docs and code", confluence=confluence, github=github)

    assert "[Source: GitHub: acme/payments:src/pay.py]" in answer
    assert "SOURCES USED: github" in answer
    assert "SOURCES WITH NO EVIDENCE: confluence" in answer


def test_failed_source_does_not_suppress_successful_source() -> None:
    confluence = StubConfluence(fail=True)
    github = StubGitHub()
    factory = make_factory(planning(["confluence", "github"]))

    answer = run_turn(factory, "Compare docs and code", confluence=confluence, github=github)

    assert "currently unavailable" in answer
    assert "[Source: GitHub: acme/payments:src/pay.py]" in answer
    assert "SOURCES USED: github" in answer
    assert "SOURCES WITH NO EVIDENCE: confluence" in answer


def test_sharepoint_only_routing_reaches_answer() -> None:
    sharepoint = StubSharePoint()
    factory = make_factory(planning(["sharepoint"]))

    answer = run_turn(factory, "Where is the onboarding document?", sharepoint=sharepoint)

    assert sharepoint.searched == ["Where is the onboarding document?"]
    assert "[Source: SharePoint: Onboarding Process.docx]" in answer
    assert "SOURCES USED: sharepoint" in answer


def test_uploaded_documents_routing_reaches_answer() -> None:
    rag = RecordingRag()
    factory = make_factory(planning(["knowledge"]))

    answer = run_turn(factory, "Summarize my uploaded document", rag=rag)

    assert rag.calls == [("Summarize my uploaded document", "s1")]
    assert "[Source: notes.md]" in answer
    assert "SOURCES USED: knowledge" in answer


def test_explicit_request_drives_retrieval_without_model_plan() -> None:
    confluence = StubConfluence()
    github = StubGitHub()
    factory = make_factory(planning([]))

    answer = run_turn(
        factory,
        "Search Confluence and GitHub for the architecture",
        confluence=confluence,
        github=github,
    )

    assert confluence.searched == ["Search Confluence and GitHub for the architecture"]
    assert "[Source: Confluence: Payments Architecture" in answer
    assert "[Source: GitHub: acme/payments:src/pay.py]" in answer


def test_multi_source_answer_is_single_and_never_asks_permission() -> None:
    confluence = StubConfluence()
    github = StubGitHub()
    fake = ScriptedChatCompletion(plan=[], answerer=evidence_only)
    factory = SemanticKernelFactory(
        Settings(),
        chat_service=fake,
        source_planner=planning(["confluence", "github"]),
        use_loop=False,
    )

    answer = run_turn(factory, "Compare docs and code", confluence=confluence, github=github)

    for phrase in ("Would you like me to search", "Should I search", "I can search"):
        assert phrase not in answer
    assert "[Source: Confluence: Payments Architecture" in answer
    assert "[Source: GitHub: acme/payments:src/pay.py]" in answer


def test_no_op_planner_keeps_raw_agent_loop() -> None:
    fake = ScriptedChatCompletion(plan=[], answerer=received_text)
    factory = SemanticKernelFactory(
        Settings(),
        chat_service=fake,
        source_planner=NoOpSourcePlanner(),
        use_loop=False,
    )
    answer = run_turn(factory, "hello", rag=RecordingRag())
    assert "=== RETRIEVED EVIDENCE ===" not in answer
    assert "SOURCES USED:" not in answer
