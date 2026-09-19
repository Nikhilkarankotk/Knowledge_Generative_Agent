# Ask PNC — Demo Guide (Evidence-Based)

> Goal: a rehearsed 5-minute demo where every moment is backed by a real, working feature of the
> application. Ordering is deliberate: it takes the audience from "polished product" to "trusted,
> governance-first knowledge agent."

## 1. The pitch (say this verbatim, 30s)

    PNC knowledge is spread across policies, decks, meeting notes, and scanned records.
    Pilots spend hours digging for answers. Ask PNC turns that into an answer engine:
    upload the documents, ask in plain English, and get answers grounded in source material,
    with memory, governance, and an enterprise-grade experience.

## 0. If the slot is 60 seconds (elevator demo)

1. Open the app on the pre-built session (30s): "This is PNC's Enterprise Brain — answers are grounded in documents, not made up."
2. Ask one live question against a loaded doc (20s): "What are the top operational risks in this document?"
3. Close (10s): "Grounded answers, conversation memory, feedback governance, share to Teams/Outlook — and a roadmap to transcript and full-document summarization. Thank you."

The elevator relies on a pre-loaded session and exactly one live question — no navigation.

---

## 2. The evidence map — every demo moment and its proof

| # | Moment | Real proof in the app | Audience feeling | Line to say |
|---|--------|----------------------|------------------|-------------|
| 0 | Brand identity | PNC favicon (crop from `src/assets/pnc-bank-logo.png`), `pnc-mark-crop` header, title "PNC Vault IQ", dark Outfit UI | "This is a product" | — |
| 1 | Premium landing | Animated shimmer "Welcome" heading, "KNOWLEDGE WORKSPACE" kicker, Knowledge Overview | Polished | "This is PNC's Enterprise Brain." |
| 2 | Attach a document | Paperclip + attach flow (`chat-interface.html`), supports PDF / DOCX / XLSX / PPTX / HTML / JSON / CSV / TXT / MD / LOG / RST / images | "Even scans work" | "Even scanned documents are understood." |
| 3 | Immediate grounded answer | RAG retrieval (`app/services/chat_service.py` top-K chunks), answer with pulsing live-dot + "Trusted knowledge" label | Magic | "That answer came from the document I just uploaded, stored as embeddings in PostgreSQL (pgvector)." |
| 4 | Follow-up chain | Conversation memory (`app/services/conversation_memory_service.py`) | "It remembers" | Ask a question that only makes sense given the previous answer. |
| 5 | Honest refusal | Ask something outside the documents -> genuine "I don't know" | Trust | "No hallucination theater — it answers only from its evidence." |
| 6 | Share to Teams / Outlook | Share popover with brand icons and hover interaction | Delight | "Knowledge flows straight into the tools you already work in." |
| 7 | Export | `exportConversation()` downloads the transcript as `.txt` | Workflow | — |
| 8 | Feedback loop | Thumbs up/down + "Suggest correction" -> `POST /api/feedback` | Governance | "Human in the loop: corrections feed back, not just chat." |
| 9 | Resume sessions | Sidebar Recent Chats (`getChatSessions`), session restore/delete | Persistence | "Sessions survive; you resume where you stopped." |
| 10 | Knowledge operations | Notifications badge (2), Knowledge Health (89%, 1,248 sources), Recent Changes, Top Experts | Intelligence | "The agent tells you when knowledge goes stale." |
| 11 | Micro-interactions | Input focus glow, send-button pulse, card reveal, export/share shine sweeps | "It's alive" | — |

---

## 3. The script (5 acts, ~5 min)

### Act 0 — Enter (0:00-0:20)
- Browser at 1920x1080, freshly loaded with a pre-built session (docs ingested, 2-3 prior turns).
- Nothing clicked yet. Let the UI sit: dark theme, PNC branding, insights collapsed.
- Line: "This is PNC's Enterprise Brain — every answer is grounded in documents, not made up."

### Act 1 — Transform (0:20-1:30)
1. Open Knowledge Overview: hover reveals Alerts / Health / Recent Changes / Top Experts.
   - Line: "Two new alerts — the agent flags when knowledge is stale."
2. Click New Inquiry (or continue existing thread).
3. Attach a sample document — prefer a scanned image to show OCR, plus a PDF.
   - Line: "Even scanned documents are understood."
4. Ask a natural question: "What are the top 3 operational risks in this document?"
5. Answer arrives with the live-dot and Trusted knowledge label; pause.
   - Line: "That answer came from the document I just uploaded — persisted as embeddings so it answers again next time."

### Act 2 — Think (1:30-2:30)
1. Follow-up: "Which of those risks is the most time-critical and why?"
   - Shows conversation memory across turns.
2. Ask something the docs cannot answer: "Who headed treasury in 1999?"
   - Its honest refusal is intentional and valuable.
   - Line: "No hallucination theater — it only grounds in what it knows."

### Act 3 — Govern (2:30-3:30)
1. Hover Share -> Teams and Outlook popover.
   - Line: "Knowledge flows straight into the tools you already work in."
2. Export the conversation (download .txt).
3. Thumbs up, then "Suggest correction" on an answer.
   - Line: "Human in the loop — corrections feed back into the system, not just chat."

### Act 4 — Operate (3:30-4:30)
1. Sidebar: point at Recent Chats; explain sessions persist and resume.
2. Reopen the Knowledge Overview: Health meter (89% / 1,248 sources), Recent Changes (governance, operations, finance).
   - Line: "This isn't a FAQ bot — it's a knowledge-health product."

### Act 5 — Vision (4:30-5:00)
- One-line architecture: Angular UI -> FastAPI -> Mistral (LLM + OCR) -> PostgreSQL / pgvector.
- Roadmap line: "Next milestone: meeting-transcript and full-document summarization, and collaborative knowledge editing."
- Leave a single slide with that architecture and the roadmap.

---

## 4. Prep checklist

- [ ] Sample doc set ready:
  - a scanned image (clean, high contrast) -> shows OCR;
  - a banking policy PDF (2-4 pages) -> the "operational risks" question;
  - optional transcript TXT -> roadmap mention.
- [ ] A pre-built session with 3-4 turns of history for immediate resume.
- [ ] Canned question list in case live choices stumble (3 grounded + 1 "I don't know").
- [ ] Browser: 1920x1080, no zoom, dev tools closed.
- [ ] Backend running (uvicorn, port 8000), PostgreSQL on 5433, UI running (port 4200).
- [ ] 90s pre-recorded walkthrough video as fallback.
- [ ] Validate the long-doc question once on the exact sample used (answers depend on the document content).

## 5. Judge-answer crib sheet

- How do you prevent hallucination? -> Answers are grounded in retrieved chunks; the agent explicitly refuses when nothing matches.
- What's the stack? -> Angular frontend, FastAPI backend, Mistral for LLM/OCR, PostgreSQL with pgvector for embeddings.
- What formats? -> PDF, DOCX, XLSX, PPTX, HTML, JSON, CSV, TXT/MD/LOG/RST, and images via OCR.
- How does memory work? -> Per-session conversation context; sessions persist and resume from the sidebar.
- What governance exists? -> Ratings, "suggest correction", export, share to Teams/Outlook, session management.
- Why vector search? -> Semantic retrieval of the most relevant evidence (top-K), not keyword matching.

## 6. Honesty guardrails (do NOT claim)

- Full-document summarization (current RAG returns top-K chunks, not the whole document).
- Video/transcript summarization (not yet implemented).
- Editing source documents (not yet implemented).
- Multilingual answers (pipeline is standardized to English today).

Frame these as the roadmap, not as features, to protect credibility.