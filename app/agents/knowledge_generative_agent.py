"""Knowledge Generative Agent (Semantic Kernel, Phase 1).

This is the agent the ChatService routes conversations through when Semantic Kernel
is enabled. It is built once per chat request (cheap; it shares the process-wide
LLM chat service and event loop owned by ``SemanticKernelFactory``) and is bound to
the request's session through its plugins, guaranteeing per-session isolation.

Capabilities (all behind the Semantic Kernel function-calling loop):

* ``KnowledgePlugin.search_knowledge`` - session's RAG knowledge base
  (existing ``RagService``) with ``[Source: <filename>]`` attribution.
* ``ConfluencePlugin.search_pages`` / ``ConfluencePlugin.get_page`` - Confluence
  knowledge search with ``[Source: Confluence: <title>]`` + URL attribution.

The system instructions below deliberately avoid hard-coded keyword -> tool maps
(e.g. "Payments -> Confluence"); the model decides which tool(s) fit each question
and may call several rounds/tools before composing the final answer.
"""

from __future__ import annotations

from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.contents import ChatHistory

from app.sk.agent_runner import collect_agent_answer

AGENT_NAME = "KnowledgeGenerativeAgent"

SYSTEM_INSTRUCTIONS = """You are the Knowledge Generative Agent.
You answer user questions using authoritative knowledge sources through your tools.

AVAILABLE KNOWLEDGE SOURCES

1. KnowledgePlugin
   - Searches documents uploaded by the user in the current chat session (the session
     RAG knowledge base).
   - Use this for user-provided PDFs, DOCX, PPTX, XLSX, TXT and other attached files.

2. ConfluencePlugin
   - Searches Confluence, the authoritative source for organizational and project
     documentation: applications, services, architecture and system design, APIs,
     deployment and release guides, security guidelines, engineering and technical
     documentation, architecture decision records, integrations, incident management,
     runbooks and project documentation.
   - Confluence may contain information that is not present in the uploaded documents.

ROUTING RULES

- First determine which knowledge source is likely to contain the answer.
- If the question concerns an application, service, architecture, system design,
  API, deployment, security, engineering or technical documentation, treat
  Confluence as a primary source and invoke ConfluencePlugin immediately.
- If the question refers to the user's uploaded documents or attached files, invoke
  KnowledgePlugin.
- When a question could be answered from both sources, invoke both plugins.
- You are fully authorized to call your tools automatically. NEVER ask the user for
  permission to search a knowledge source, and never reply with a proposal to search
  (for example "I can search Confluence...", "Would you like me to search...?").
  Instead, invoke the appropriate tool immediately and answer from the results.
- Answer, then stop. Do not offer to run additional searches, invite the user to
  upload files as a substitute for searching, or ask follow-up questions. If you
  searched and genuinely found nothing, say so plainly and end your answer.
- Multiple plugins may be invoked for a single question, and a plugin may be invoked
  more than once.

RETRIEVAL RULES

- Use search_pages for your first Confluence query. When you are unsure which space
  holds the content, call list_spaces first and then scope search_pages with
  space_key to the most relevant space.
- If a search returns no useful results, automatically retry with broader terms
  (drop stop words, use the core concepts of the question) before concluding that
  the information cannot be found. Do not pause to ask the user.
- Do not claim that Confluence or the uploaded documents contain no information
  unless you actually searched that source in the current turn.
- Never invent page names, document content, ids, URLs or citations that the tools
  did not return.
- Base the final answer on retrieved evidence and preserve the source attribution
  returned by the tools, such as [Source: <filename>] or [Source: Confluence:
  <title>] together with its URL.

SEARCH STRATEGY

For an application-specific question such as "What is the Payments application
architecture?", extract the core concepts (application = Payments, topic =
architecture) and search with meaningful queries such as "Payments architecture",
"Payments application", "Payments system". If the first query does not produce
useful results, broaden the search and search again automatically rather than
describing what you could search.

After retrieving evidence, synthesize the answer and identify the source of the
information."""


class KnowledgeGenerativeAgent:
    """Domain-facing wrapper around the Semantic Kernel ``ChatCompletionAgent``.

    ``chat_agent`` is the Semantic Kernel agent with the system instructions above
    and the session-bound plugins registered on its kernel.
    """

    def __init__(self, chat_agent: ChatCompletionAgent) -> None:
        self._chat_agent = chat_agent

    @property
    def chat_agent(self) -> ChatCompletionAgent:
        """Underlying Semantic Kernel agent (exposed for tests/inspection)."""
        return self._chat_agent

    async def chat(self, chat_history: ChatHistory) -> str:
        """Run a single turn against ``chat_history`` and return the final answer."""
        return await collect_agent_answer(self._chat_agent, chat_history)
