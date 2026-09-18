"""Tests for the Semantic Kernel KnowledgeGenerativeAgent runtime.

Verifies the full function-calling loop over a *scripted* chat service (no network):
tool selection, argument parsing, plugin execution, tool results in history, final
answer production, per-session isolation and the Python 3.14 compatibility patch.
"""

from __future__ import annotations

import pytest
from semantic_kernel.contents import ChatHistory

from app.agents.knowledge_generative_agent import AGENT_NAME, SYSTEM_INSTRUCTIONS
from app.core.config import Settings
from app.core.exceptions import KnowledgeAgentError
from app.models import DocumentChunk
from app.rag.rag_service import RagService
from app.rag.text_chunker import TextChunker
from app.repositories import DocumentChunkRepository
from app.services.mistral_api_service import MistralApiService
from app.sk.compat import apply_py314_compatibility_patch, drop_names_except_tool
from app.sk.semantic_kernel_factory import SemanticKernelFactory
from tests.conftest import FakeLLM
from tests.fake_sk_service import ScriptedChatCompletion, tool_results


class StaticEmbeddingService:
    def __init__(self, query_vector: list[float]) -> None:
        self.query_vector = query_vector

    def generate_embedding(self, text: str) -> list[float]:
        return self.query_vector


class StubConfluence:
    def __init__(self, *, enabled: bool = True, fail: bool = False) -> None:
        self._enabled = enabled
        self._fail = fail
        self.searched: list[str] = []
        self.page_requests: list[str] = []
        self._search_output = (
            "[Source: Confluence: Roadmap (space: Eng)]\n"
            "Page id: 42\n"
            "URL: https://wiki.example.com/spaces/Eng/pages/42\n"
            "Excerpt: Q3 delivery plan"
        )
        self._page_output = (
            "[Source: Confluence: Roadmap]\n"
            "https://wiki.example.com/spaces/Eng/pages/42\n"
            "Q3 ships the payments module."
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def search(self, query: str, limit: int | None = None, space_key: str | None = None) -> str:
        if self._fail:
            from app.core.exceptions import ConfluenceApiError

            raise ConfluenceApiError("Confluence is down")
        self.searched.append(query)
        return self._search_output

    def list_spaces(self, limit: int = 50) -> str:
        return "Available Confluence spaces:\n[Source: Confluence] key=ENG, name=Engineering"

    def get_page(self, page_id: str) -> str:
        self.page_requests.append(page_id)
        return self._page_output

    def close(self) -> None:
        pass


class StubGitHub:
    def __init__(self, *, enabled: bool = True, fail: bool = False) -> None:
        self._enabled = enabled
        self._fail = fail
        self.searched: list[str] = []
        self.repo_requests: list[str] = []
        self._repo_output = (
            "[Source: GitHub: eng/payments]\n"
            "URL: https://github.com/eng/payments\n"
            "Language: Python\n"
            "Description: Implements the checkout module"
        )
        self._code_output = (
            "[Source: GitHub: eng/payments:src/api.py]\n"
            "URL: https://github.com/eng/payments/blob/main/src/api.py\n"
            "Snippet: def charge_payment"
        )
        self._readme_output = (
            "[Source: GitHub: eng/payments]\n"
            "README of eng/payments\n"
            "Implements the checkout module."
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def list_allowed_repositories(self) -> str:
        if self._fail:
            from app.core.exceptions import GitHubApiError

            raise GitHubApiError("GitHub is down")
        self.searched.append("list-allowed")
        return self._repo_output

    def get_repository(self, repo: str) -> str:
        self.repo_requests.append(f"repo:{repo}")
        return self._repo_output

    def get_readme(self, repo: str) -> str:
        if self._fail:
            from app.core.exceptions import GitHubApiError

            raise GitHubApiError("GitHub is down")
        self.repo_requests.append(f"readme:{repo}")
        return self._readme_output

    def list_repository_contents(self, repo: str, path: str = "") -> str:
        self.repo_requests.append(f"tree:{repo}:{path}")
        return "[Source: GitHub: eng/payments]/"

    def get_file_content(self, repo: str, path: str) -> str:
        self.repo_requests.append(f"file:{repo}:{path}")
        return "[Source: GitHub: eng/payments:src/api.py]\ndef charge_payment(): pass"

    def search_code(self, repository: str, query: str, limit: int | None = None) -> str:
        if self._fail:
            from app.core.exceptions import GitHubApiError

            raise GitHubApiError("GitHub is down")
        self.searched.append(f"code:{query}")
        return self._code_output

    def get_issue(self, repo: str, issue_number: int) -> str:
        self.repo_requests.append(f"issue:{repo}#{issue_number}")
        return "[Source: GitHub: eng/payments#1]\nTitle: Checkout fails"

    def close(self) -> None:
        pass


class StubSharePoint:
    """SharePointService stand-in that records calls and returns fixed content."""

    def __init__(self, *, enabled: bool = True, fail: bool = False) -> None:
        self._enabled = enabled
        self._fail = fail
        self.searched: list[str] = []
        self.read_requests: list[tuple[str, str]] = []
        self._search_output = (
            "[Source: SharePoint: Onboarding Process.docx]\n"
            "URL: https://knowledgegenagent.sharepoint.com/sites/KnowledgeGenAgent/Shared "
            "Documents/sharepoint-rag-knowledge-base/Onboarding Process.docx\n"
            "Drive id: b!drive\n"
            "Document id: 01onboard\n"
            "Size: 2048 bytes\n"
            "Modified: 2026-02-01T10:00:00Z\n"
            "Type: file"
        )
        self._content_output = (
            "[Source: SharePoint: Onboarding Process.docx]\n"
            "URL: https://knowledgegenagent.sharepoint.com/sites/KnowledgeGenAgent/Shared "
            "Documents/sharepoint-rag-knowledge-base/Onboarding Process.docx\n"
            "Drive id: b!drive\n"
            "Document id: 01onboard\n"
            "New hires are onboarded through the SharePoint site."
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def allowed_sites(self) -> list[str]:
        return ["knowledgegenagent.sharepoint.com,site,web"]

    def list_allowed_sites(self) -> str:
        return "[Source: SharePoint] Configured SharePoint sites: knowledgegenagent.sharepoint.com,site,web"

    def search(self, query: str, limit: int | None = None) -> str:
        if self._fail:
            from app.core.exceptions import SharePointApiError

            raise SharePointApiError("SharePoint is down")
        self.searched.append(query)
        return self._search_output

    def search_file_content(
        self, query: str, limit: int | None = None
    ) -> str:
        if self._fail:
            from app.core.exceptions import SharePointApiError

            raise SharePointApiError("SharePoint is down")
        self.searched.append(f"content:{query}")
        return self._content_output

    def list_files(self, limit: int | None = None) -> str:
        return "[Source: SharePoint: site kga]\nDocument library drive id: b!drive"

    def get_document_content(self, document_id: str, drive_id: str) -> str:
        self.read_requests.append((drive_id, document_id))
        return self._content_output

    def close(self) -> None:
        pass


class RecordingRag:
    """RagService stand-in that records calls and returns a fixed context."""

    def __init__(self, context: str = "", empty: bool = False) -> None:
        self._context = context
        self._empty = empty
        self.calls: list[tuple[str, str]] = []

    def is_knowledge_base_empty(self, session_id: str) -> bool:
        return self._empty

    def retrieve_context(self, query: str, session_id: str) -> str:
        self.calls.append((query, session_id))
        return self._context


def make_history(user_message: str) -> ChatHistory:
    history = ChatHistory()
    history.add_user_message(user_message)
    return history


def make_factory(plan, *, answerer=None, delay: float = 0.0) -> tuple[SemanticKernelFactory, ScriptedChatCompletion]:
    fake = ScriptedChatCompletion(plan=plan, answerer=answerer, delay=delay)
    return SemanticKernelFactory(Settings(), chat_service=fake, use_loop=False), fake


def run_turn(
    factory: SemanticKernelFactory,
    *,
    rag=None,
    session_id: str = "s1",
    confluence=None,
    github=None,
    sharepoint=None,
    user_message: str = "How do I do this?",
    timeout: float | None = None,
) -> str:
    agent = factory.build_agent(
        rag_service=rag,
        session_id=session_id,
        confluence_service=confluence,
        github_service=github,
        sharepoint_service=sharepoint,
    )
    return factory.run_agent(agent, make_history(user_message), timeout=timeout)


def test_plugins_registered_under_expected_names() -> None:
    factory, _ = make_factory([])
    agent = factory.build_agent(
        rag_service=RecordingRag(context="ctx"),
        confluence_service=StubConfluence(),
        github_service=StubGitHub(),
        sharepoint_service=StubSharePoint(),
    )
    plugins = agent.chat_agent.kernel.plugins
    assert set(plugins.keys()) == {"Knowledge", "Confluence", "GitHub", "SharePoint"}
    assert set(plugins["Knowledge"].functions.keys()) == {"search_knowledge"}
    assert set(plugins["Confluence"].functions.keys()) == {
        "search_pages",
        "get_page",
        "list_spaces",
    }
    assert set(plugins["GitHub"].functions.keys()) == {
        "list_allowed_repositories",
        "get_repository",
        "get_readme",
        "list_repository_contents",
        "get_file_content",
        "retrieve_repository_contents",
        "search_code",
        "get_issue",
    }
    assert set(plugins["SharePoint"].functions.keys()) == {
        "search_sharepoint",
        "search_sharepoint_content",
        "list_sharepoint_documents",
        "get_sharepoint_document",
    }
    assert agent.chat_agent.name == AGENT_NAME


def test_build_agent_wires_configured_max_auto_invoke_attempts() -> None:
    settings = Settings()
    factory, _ = make_factory([])
    agent = factory.build_agent(
        rag_service=RecordingRag(context="ctx"),
        confluence_service=StubConfluence(),
    )
    attempts = agent.chat_agent.function_choice_behavior.maximum_auto_invoke_attempts
    assert attempts == settings.sk_max_auto_invoke_attempts
    assert settings.sk_max_auto_invoke_attempts > 0


def test_system_instructions_mandate_automatic_invocation_and_no_permission_asking() -> None:
    assert "authorized to call your tools automatically" in SYSTEM_INSTRUCTIONS
    assert "NEVER ask the user for" in SYSTEM_INSTRUCTIONS
    assert "permission to search a knowledge source" in SYSTEM_INSTRUCTIONS
    # Architecture/design/technical questions fan out to BOTH documentation
    # sources in the same turn - SharePoint is a co-primary source, not a fallback.
    assert "invoke ConfluencePlugin AND" in SYSTEM_INSTRUCTIONS
    assert "SharePointPlugin (search_sharepoint_content) in the same turn" in SYSTEM_INSTRUCTIONS
    assert "Do not claim that Confluence or the uploaded documents contain no information" in (
        SYSTEM_INSTRUCTIONS
    )
    assert "Answer, then stop." in SYSTEM_INSTRUCTIONS
    assert "broaden the search and search again automatically" in SYSTEM_INSTRUCTIONS


def test_system_instructions_route_sharepoint_to_content_search_not_site_listing() -> None:
    """The agent used to call list_sharepoint_documents (which lists SITES in
    tenant-wide mode) and concluded SharePoint had nothing; it must search content."""
    assert "start with search_sharepoint_content" in SYSTEM_INSTRUCTIONS
    assert "Do NOT use list_sharepoint_documents to look for a document" in SYSTEM_INSTRUCTIONS
    assert "lists SharePoint *sites*, not files" in SYSTEM_INSTRUCTIONS
    assert "retry search_sharepoint_content with the core subject" in SYSTEM_INSTRUCTIONS


def test_system_instructions_have_no_hardcoded_routing_map() -> None:
    assert "->" not in SYSTEM_INSTRUCTIONS
    assert 'if "' not in SYSTEM_INSTRUCTIONS.lower()


def test_routing_architecture_question_invokes_confluence() -> None:
    confluence = StubConfluence()
    factory, _ = make_factory(
        [("Confluence", "search_pages", {"query": "Payments Application architecture"})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        confluence=confluence,
        user_message="What are the main components and architecture of the Payments Application?",
    )

    assert confluence.searched == ["Payments Application architecture"]
    assert "[Source: Confluence: Roadmap" in answer


def test_routing_deployment_question_invokes_confluence() -> None:
    confluence = StubConfluence()
    factory, _ = make_factory(
        [("Confluence", "search_pages", {"query": "Payments application deployment"})]
    )

    run_turn(
        factory,
        rag=RecordingRag(),
        confluence=confluence,
        user_message="How is the Payments application deployed?",
    )

    assert confluence.searched == ["Payments application deployment"]


def test_routing_api_authentication_question_invokes_confluence() -> None:
    confluence = StubConfluence()
    factory, _ = make_factory(
        [("Confluence", "search_pages", {"query": "Payments API authentication"})]
    )

    run_turn(
        factory,
        rag=RecordingRag(),
        confluence=confluence,
        user_message="How does authentication work for the Payments APIs?",
    )

    assert confluence.searched == ["Payments API authentication"]


def test_routing_nested_documentation_question_includes_application_in_query() -> None:
    confluence = StubConfluence()
    confluence._search_output = (
        "[Source: Confluence: API Documentation (space: PAY)]\n"
        "Page id: DOC1\n"
        "URL: https://wiki.example.com/spaces/PAY/pages/DOC1\n"
        "Parent: Payments Application\n"
        "Excerpt: REST endpoint reference for the Payments APIs."
    )
    factory, _ = make_factory(
        [
            (
                "Confluence",
                "search_pages",
                {"query": "Payments application API documentation"},
            )
        ]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        confluence=confluence,
        user_message="Where is the API documentation for the Payments Application?",
    )

    assert confluence.searched == ["Payments application API documentation"]
    assert "[Source: Confluence: API Documentation" in answer
    assert "Parent: Payments Application" in answer


def test_system_instructions_include_application_in_nested_page_queries() -> None:
    assert "Payments application API documentation" in SYSTEM_INSTRUCTIONS
    assert "let search_pages resolve the page hierarchy" in SYSTEM_INSTRUCTIONS
    assert "nested under an application's own page" in SYSTEM_INSTRUCTIONS


def test_system_instructions_forbid_generic_fallback_when_sources_empty() -> None:
    assert "Do not substitute a generic industry" in SYSTEM_INSTRUCTIONS
    assert "say explicitly that no matching information could be retrieved" in SYSTEM_INSTRUCTIONS
    assert "do not claim a page or document does not exist" in SYSTEM_INSTRUCTIONS


def test_routing_uploaded_pdf_question_invokes_knowledge() -> None:
    rag = RecordingRag(context="[Source: report.pdf]\nreport content")
    factory, _ = make_factory(
        [("Knowledge", "search_knowledge", {"query": "what does the uploaded PDF contain"})]
    )

    run_turn(
        factory,
        rag=rag,
        session_id="s1",
        user_message="Summarize my uploaded PDF?",
    )

    assert [query for query, _ in rag.calls] == ["what does the uploaded PDF contain"]


def test_routing_compare_question_invokes_both_tools() -> None:
    rag = RecordingRag(context="[Source: arch.pdf]\narchitecture content")
    confluence = StubConfluence()
    factory, _ = make_factory(
        [
            ("Knowledge", "search_knowledge", {"query": "architecture"}),
            ("Confluence", "search_pages", {"query": "architecture"}),
        ]
    )

    run_turn(
        factory,
        rag=rag,
        confluence=confluence,
        user_message="Compare my uploaded architecture document with the Confluence documentation.",
    )

    assert [query for query, _ in rag.calls] == ["architecture"]
    assert confluence.searched == ["architecture"]


def test_compat_patch_is_idempotent() -> None:
    apply_py314_compatibility_patch()
    apply_py314_compatibility_patch()
    # Rebuilding constructs a ChatCompletionAgent on top of the patched decorator.
    make_factory([])


def test_single_knowledge_tool_call_with_attribution_and_arguments() -> None:
    rag = RecordingRag(context="[Source: payments.pdf]\nthe payment policy is 30 days")
    factory, _ = make_factory([("Knowledge", "search_knowledge", {"query": "payment policy"})])

    answer = run_turn(factory, rag=rag, session_id="s1", user_message="What is the payment policy?")

    # Plugin received the session-bound call with the parsed query argument.
    assert rag.calls == [("payment policy", "s1")]
    # The surfaced tool result preserved attribution and reached the final answer.
    assert "[Source: payments.pdf]" in answer
    assert "30 days" in answer


def test_multi_tool_round_combines_knowledge_and_confluence() -> None:
    rag = RecordingRag(context="[Source: portfolio.md]\nproject summary")
    confluence = StubConfluence()
    factory, fake = make_factory(
        [
            ("Knowledge", "search_knowledge", {"query": "portfolio"}),
            ("Confluence", "search_pages", {"query": "roadmap"}),
        ]
    )

    answer = run_turn(
        factory,
        rag=rag,
        session_id="s1",
        confluence=confluence,
        user_message="Summarize the portfolio against the roadmap.",
    )

    assert "[Source: portfolio.md]" in answer
    assert "[Source: Confluence: Roadmap" in answer
    assert confluence.searched == ["roadmap"]
    assert len(tool_results(fake.histories[-1])) == 2


def test_confluence_get_page_follow_up_receives_page_id() -> None:
    confluence = StubConfluence()
    factory, _ = make_factory(
        [
            ("Confluence", "search_pages", {"query": "roadmap"}),
            ("Confluence", "get_page", {"page_id": "42"}),
        ]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        session_id="s1",
        confluence=confluence,
        user_message="What is in the roadmap page?",
    )

    assert confluence.searched == ["roadmap"]
    assert confluence.page_requests == ["42"]
    assert "Q3 ships the payments module" in answer


def test_empty_knowledge_base_marker_reaches_final_answer() -> None:
    factory, _ = make_factory([("Knowledge", "search_knowledge", {"query": "x"})])
    answer = run_turn(factory, rag=RecordingRag(empty=True), session_id="s1", user_message="anything")
    assert "No documents have been uploaded" in answer


def test_system_instructions_mention_github_as_first_class_source() -> None:
    assert "GitHubPlugin" in SYSTEM_INSTRUCTIONS
    assert "authoritative source for how systems are actually" in SYSTEM_INSTRUCTIONS
    assert "invoke GitHubPlugin immediately" in SYSTEM_INSTRUCTIONS
    assert "compare the\n  documented architecture with the actual code" in SYSTEM_INSTRUCTIONS or (
        "compare the documented architecture with the actual code" in SYSTEM_INSTRUCTIONS
    )
    assert "Do not claim that GitHub contains no relevant code" in SYSTEM_INSTRUCTIONS
    assert "[Source: GitHub: <owner/repo>:<path>]" in SYSTEM_INSTRUCTIONS
    assert "list_allowed_repositories first" in SYSTEM_INSTRUCTIONS
    assert "retrieve_repository_contents" in SYSTEM_INSTRUCTIONS
    assert "GITHUB_ALLOWED_REPOSITORIES" in SYSTEM_INSTRUCTIONS
    assert "search_repositories" not in SYSTEM_INSTRUCTIONS


def test_system_instructions_forbid_fabrication_on_retrieval_failure() -> None:
    # G. A repository-specific question must not fall back to a generic
    # "typical architecture from common practices" answer when GitHub retrieval
    # fails; the model must say what could not be retrieved.
    assert "do not fabricate" in SYSTEM_INSTRUCTIONS
    assert "could not be retrieved" in SYSTEM_INSTRUCTIONS
    assert "State clearly and explicitly which information could not be retrieved" in SYSTEM_INSTRUCTIONS
    assert "answer only what was actually retrieved" in SYSTEM_INSTRUCTIONS


def test_system_instructions_contain_grounding_policy() -> None:
    assert "GROUNDING POLICY" in SYSTEM_INSTRUCTIONS
    assert "answer only using information retrieved from the configured knowledge sources" in SYSTEM_INSTRUCTIONS
    assert "Do not infer the application's architecture or functionality from its name" in SYSTEM_INSTRUCTIONS
    assert "Do not substitute a generic industry architecture" in SYSTEM_INSTRUCTIONS
    assert "Never fabricate repository contents" in SYSTEM_INSTRUCTIONS
    assert "Never use information from an unrelated public repository" in SYSTEM_INSTRUCTIONS
    assert "Never treat a repository mentioned in previous conversation history as\n   authorized unless it is present in GITHUB_ALLOWED_REPOSITORIES" in SYSTEM_INSTRUCTIONS or (
        "never treat a repository mentioned in previous conversation history" in SYSTEM_INSTRUCTIONS.lower()
    )
    assert "I couldn't retrieve sufficient information from the configured <repository>" in SYSTEM_INSTRUCTIONS
    assert "answer this accurately" in SYSTEM_INSTRUCTIONS


def test_system_instructions_forbid_guessing_repo_names_and_existence_claims() -> None:
    # The reported bug: the agent answered about "E-commerce-Application" (hyphen,
    # a guessed name) and, on failure, produced a "likely tech stack" from general
    # knowledge. The instructions must forbid both behaviors.
    assert "Never construct or guess a GitHub repository name" in SYSTEM_INSTRUCTIONS
    assert "Never state that a repository does not exist" in SYSTEM_INSTRUCTIONS
    assert "proof that the repository is absent" in SYSTEM_INSTRUCTIONS
    assert "do not answer with a" in SYSTEM_INSTRUCTIONS.lower()
    assert "estimated architecture" in SYSTEM_INSTRUCTIONS.lower()
    assert "not on repository code" in SYSTEM_INSTRUCTIONS
    assert "Repository names passed to GitHub functions MUST be exactly the owner/name" in SYSTEM_INSTRUCTIONS


def test_routing_implementation_question_invokes_github() -> None:
    github = StubGitHub()
    factory, _ = make_factory(
        [("GitHub", "list_allowed_repositories", {})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        github=github,
        user_message="Which GitHub repositories are configured for this agent?",
    )

    assert github.searched == ["list-allowed"]
    assert "[Source: GitHub: eng/payments]" in answer


def test_routing_source_code_question_invokes_github_code_search() -> None:
    github = StubGitHub()
    factory, _ = make_factory(
        [("GitHub", "search_code", {"repository": "eng/payments", "query": "charge_payment"})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        github=github,
        user_message="Where is the charge_payment function implemented in the Payments repository?",
    )

    assert github.searched == ["code:charge_payment"]
    assert "[Source: GitHub: eng/payments:src/api.py]" in answer


def test_routing_compare_architecture_with_code_invokes_both() -> None:
    confluence = StubConfluence()
    github = StubGitHub()
    factory, _ = make_factory(
        [
            ("Confluence", "search_pages", {"query": "Payments architecture"}),
            ("GitHub", "list_allowed_repositories", {}),
        ]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        confluence=confluence,
        github=github,
        user_message="Compare the documented Payments architecture with the actual code.",
    )

    assert confluence.searched == ["Payments architecture"]
    assert github.searched == ["list-allowed"]
    assert "[Source: Confluence: Roadmap" in answer
    assert "[Source: GitHub: eng/payments]" in answer


def test_system_instructions_mention_sharepoint_as_first_class_source() -> None:
    assert "SharePointPlugin" in SYSTEM_INSTRUCTIONS
    assert "approved SharePoint knowledge base" in SYSTEM_INSTRUCTIONS
    assert "invoke SharePointPlugin immediately" in SYSTEM_INSTRUCTIONS
    assert "clear and explicit attribution" in SYSTEM_INSTRUCTIONS
    assert "A SharePoint result exists only if you actually searched" in SYSTEM_INSTRUCTIONS
    assert "Do not claim that SharePoint contains no relevant information" in SYSTEM_INSTRUCTIONS
    assert "[Source: SharePoint: <filename>]" in SYSTEM_INSTRUCTIONS
    assert "SHAREPOINT_ALLOWED_SITES" in SYSTEM_INSTRUCTIONS
    assert "arbitrary site id, drive id or folder" in SYSTEM_INSTRUCTIONS


def test_routing_onboarding_question_invokes_sharepoint_search() -> None:
    sharepoint = StubSharePoint()
    factory, _ = make_factory(
        [("SharePoint", "search_sharepoint", {"query": "onboarding process"})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        sharepoint=sharepoint,
        user_message="Where is the onboarding document for new hires?",
    )

    assert sharepoint.searched == ["onboarding process"]
    assert "[Source: SharePoint: Onboarding Process.docx]" in answer


def test_routing_sharepoint_content_question_invokes_search_content() -> None:
    sharepoint = StubSharePoint()
    factory, _ = make_factory(
        [("SharePoint", "search_sharepoint_content", {"query": "vacation policy"})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        sharepoint=sharepoint,
        user_message="What does the vacation policy document on SharePoint say?",
    )

    assert sharepoint.searched == ["content:vacation policy"]
    assert "New hires are onboarded through the SharePoint site." in answer


def test_sharepoint_get_document_follow_up_receives_drive_and_document_ids() -> None:
    sharepoint = StubSharePoint()
    factory, _ = make_factory(
        [
            ("SharePoint", "search_sharepoint", {"query": "onboarding"}),
            ("SharePoint", "get_sharepoint_document", {"document_id": "01onboard", "drive_id": "b!drive"}),
        ]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        sharepoint=sharepoint,
        user_message="Summarize the onboarding document.",
    )

    assert sharepoint.read_requests == [("b!drive", "01onboard")]
    assert "New hires are onboarded through the SharePoint site." in answer


def test_sharepoint_failure_becomes_marker_not_exception() -> None:
    sharepoint = StubSharePoint(fail=True)
    factory, _ = make_factory(
        [("SharePoint", "search_sharepoint", {"query": "onboarding"})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        sharepoint=sharepoint,
        user_message="Where is the onboarding document?",
    )

    assert "currently unavailable" in answer


def test_unconfigured_sharepoint_returns_marker() -> None:
    factory, _ = make_factory([("SharePoint", "search_sharepoint", {"query": "x"})])
    answer = run_turn(factory, rag=RecordingRag(), user_message="sharepoint?")
    assert "not configured" in answer


def test_github_get_readme_follow_up_receives_repo() -> None:
    github = StubGitHub()
    factory, _ = make_factory(
        [
            ("GitHub", "list_allowed_repositories", {}),
            ("GitHub", "get_readme", {"repo": "eng/payments"}),
        ]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        github=github,
        user_message="What does the README of the Payments repository say?",
    )

    assert github.repo_requests == ["readme:eng/payments"]
    assert "Implements the checkout module." in answer


def test_github_failure_becomes_marker_not_exception() -> None:
    github = StubGitHub(fail=True)
    factory, _ = make_factory(
        [("GitHub", "list_allowed_repositories", {})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        github=github,
        user_message="Which GitHub repositories are configured?",
    )

    assert "Could not list GitHub repositories" in answer


def test_github_404_becomes_marker_in_answer_not_fabricated_generic_answer() -> None:
    class NotFoundGitHub(StubGitHub):
        def get_readme(self, repo: str) -> str:  # type: ignore[override]
            from app.core.exceptions import GitHubApiError

            raise GitHubApiError(f"GitHub resource not found (HTTP 404) at repos/{repo}")

    github = NotFoundGitHub()
    factory, _ = make_factory(
        [("GitHub", "list_allowed_repositories", {}), ("GitHub", "get_readme", {"repo": "eng/payments"})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        github=github,
        user_message="What is the architecture of the E-Commerce Application?",
    )

    # The 404 is surfaced as an honest marker; the answer must not present a
    # fabricated "typical architecture" as if it came from the repository.
    assert "Could not retrieve the README of eng/payments" in answer
    assert "HTTP 404" in answer


def test_unconfigured_github_returns_marker() -> None:
    factory, _ = make_factory(
        [("GitHub", "list_allowed_repositories", {})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        user_message="Which GitHub repositories are configured?",
    )

    assert "not configured" in answer


def test_confluence_failure_becomes_marker_not_exception() -> None:
    confluence = StubConfluence(fail=True)
    factory, _ = make_factory([("Confluence", "search_pages", {"query": "x"})])
    answer = run_turn(
        factory,
        rag=RecordingRag(),
        session_id="s1",
        confluence=confluence,
        user_message="roadmap?",
    )
    assert "currently unavailable" in answer


def test_unconfigured_confluence_returns_marker() -> None:
    factory, _ = make_factory([("Confluence", "search_pages", {"query": "x"})])
    answer = run_turn(factory, rag=RecordingRag(), session_id="s1", user_message="wiki?")
    assert "not configured" in answer


def test_agent_timeout_raises_knowledge_agent_error() -> None:
    factory, _ = make_factory([], delay=0.5)
    with pytest.raises(KnowledgeAgentError, match="timed out"):
        run_turn(factory, rag=RecordingRag(), user_message="slow?", timeout=0.05)


def test_empty_answer_raises_knowledge_agent_error() -> None:
    factory, _ = make_factory([], answerer=lambda history: "   ")
    with pytest.raises(KnowledgeAgentError, match="empty"):
        run_turn(factory, rag=RecordingRag(), user_message="hi")


def test_loop_path_runs_on_shared_background_loop() -> None:
    """Production path: agents run on the process-wide asyncio loop thread."""
    fake = ScriptedChatCompletion(
        plan=[("Knowledge", "search_knowledge", {"query": "x"})],
        answerer=lambda history: "answer from loop",
    )
    factory = SemanticKernelFactory(Settings(), chat_service=fake, use_loop=True)
    try:
        assert factory._loop is not None and factory._loop_thread is not None  # type: ignore[attr-defined]
        answer = run_turn(factory, rag=RecordingRag(context="ctx"), session_id="s1", user_message="q")
        assert answer == "answer from loop"
        # The model call executed on the background loop thread.
        assert len(fake.histories) == 2
    finally:
        factory.close()
    assert factory._loop is None  # type: ignore[attr-defined]


def test_session_isolation_between_sessions(db_session) -> None:
    """Two sessions with different documents must never leak context."""
    rag = _real_rag(db_session)
    save_chunk(db_session, "session-a", "[Source: payments.pdf]\ncredit card fees are 3%", 10.0)
    save_chunk(db_session, "session-b", "[Source: hiring.md]\nvacation policy is 25 days", 20.0)

    factory, _ = make_factory([("Knowledge", "search_knowledge", {"query": "policy"})])

    answer_a = run_turn(factory, rag=rag, session_id="session-a", user_message="policy?")
    answer_b = run_turn(factory, rag=rag, session_id="session-b", user_message="policy?")

    assert "[Source: payments.pdf]" in answer_a
    assert "credit card fees are 3%" in answer_a
    assert "[Source: payments.pdf]" not in answer_b
    assert "[Source: hiring.md]" in answer_b
    assert "vacation policy is 25 days" in answer_b


def _real_rag(db_session) -> RagService:
    return RagService(
        document_parser=None,  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0, 0.0]),
        chunk_repo=DocumentChunkRepository(db_session),
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        top_k=5,
    )


def save_chunk(db_session, session_id: str, text: str, vector_value: float) -> None:
    repo = DocumentChunkRepository(db_session)
    repo.save(
        DocumentChunk(
            text=text,
            session_id=session_id,
            source_filename="doc.txt",
            embedding=[vector_value, 0.0, 0.0],
        )
    )


def test_drop_names_except_tool_strips_message_names() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "instr", "name": "KnowledgeGenerativeAgent"},
        {"role": "user", "content": "hi", "name": "someone"},
        {"role": "assistant", "content": None, "name": "KnowledgeGenerativeAgent", "tool_calls": []},
        {"role": "tool", "tool_call_id": "1", "name": "Confluence-search_pages", "content": "[]"},
    ]
    drop_names_except_tool(messages)
    assert messages[0] == {"role": "system", "content": "instr"}
    assert messages[1] == {"role": "user", "content": "hi"}
    assert messages[2] == {"role": "assistant", "content": None, "tool_calls": []}
    assert messages[3]["name"] == "Confluence-search_pages"


def test_drop_names_except_tool_keeps_tool_names() -> None:
    messages: list[dict[str, object]] = [
        {"role": "tool", "tool_call_id": "9", "name": "Knowledge-search_documents", "content": "[]"}
    ]
    assert drop_names_except_tool(messages)[0]["name"] == "Knowledge-search_documents"


class _EmptySecondSearchConfluence(StubConfluence):
    def __init__(self) -> None:
        super().__init__()
        self._calls = 0

    def search(self, query: str, limit: int | None = None, space_key: str | None = None) -> str:
        self._calls += 1
        if self._calls > 1:
            return (
                "No Confluence pages matched this query. This is not proof that "
                "Confluence has no relevant documentation."
            )
        return super().search(query, limit, space_key)


def test_confluence_retrieval_state_reaches_final_answer() -> None:
    confluence = StubConfluence()
    factory, _ = make_factory(
        [("Confluence", "search_pages", {"query": "roadmap"})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        session_id="s1",
        confluence=confluence,
        user_message="What is in the roadmap?",
    )

    assert "CONFLUENCE RETRIEVAL STATE" in answer
    assert '"evidence_found": true' in answer
    assert "Roadmap" in answer


def test_confluence_state_keeps_evidence_after_empty_search() -> None:
    confluence = _EmptySecondSearchConfluence()
    factory, _ = make_factory(
        [
            ("Confluence", "search_pages", {"query": "roadmap"}),
            ("Confluence", "search_pages", {"query": "roadmap architecture"}),
        ]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        session_id="s1",
        confluence=confluence,
        user_message="Find the roadmap and its architecture.",
    )

    assert "Roadmap (space: Eng)" in answer
    assert '"evidence_found": true' in answer
    assert '"last_search_results": 0' in answer


def test_system_instructions_require_evidence_preservation() -> None:
    assert "EVIDENCE PRESERVATION AND FINAL-ANSWER GROUNDING" in SYSTEM_INSTRUCTIONS
    assert "A later search that returns no" in SYSTEM_INSTRUCTIONS
    assert "CONFLUENCE RETRIEVAL STATE" in SYSTEM_INSTRUCTIONS
    assert "never say the documentation does not exist" in SYSTEM_INSTRUCTIONS
    assert "Never invent or guess a scope identifier" in SYSTEM_INSTRUCTIONS
    assert "GITHUB RETRIEVAL STATE" in SYSTEM_INSTRUCTIONS
    assert "According to the Confluence" in SYSTEM_INSTRUCTIONS
    assert "->" not in SYSTEM_INSTRUCTIONS
    assert 'if "' not in SYSTEM_INSTRUCTIONS.lower()



def test_github_retrieval_state_reaches_final_answer() -> None:
    github = StubGitHub()
    factory, _ = make_factory(
        [("GitHub", "search_code", {"repository": "eng/payments", "query": "charge_payment"})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        github=github,
        user_message="Where is charge_payment implemented in eng/payments?",
    )

    assert "GITHUB RETRIEVAL STATE" in answer
    assert '"source": "github"' in answer
    assert '"evidence_found": true' in answer


def test_multi_source_states_are_combined_in_final_answer() -> None:
    confluence = StubConfluence()
    github = StubGitHub()
    factory, _ = make_factory(
        [
            ("Confluence", "search_pages", {"query": "Payments architecture"}),
            ("GitHub", "search_code", {"repository": "eng/payments", "query": "charge_payment"}),
        ]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        confluence=confluence,
        github=github,
        user_message="Compare the documented Payments architecture with the actual code.",
    )

    assert "CONFLUENCE RETRIEVAL STATE" in answer
    assert "GITHUB RETRIEVAL STATE" in answer
    assert "[Source: Confluence: Roadmap" in answer
    assert "[Source: GitHub: eng/payments:src/api.py]" in answer
