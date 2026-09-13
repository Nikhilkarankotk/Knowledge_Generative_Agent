# Knowedege Generative agent— Python/FastAPI Backend

FastAPI RAG backend migrated from the original Spring Boot 3.5 (Java 21) chatbot.
The chat UI lives in [`frontend/`](frontend/README.md); this page covers running the
backend on its own.

## Prerequisites

- Python 3.11+
- PostgreSQL running locally on port **5433** with a database named `Knowledge_Gen_Agent`
  (the app creates/updates tables itself on startup)
- A [Mistral AI](https://mistral.ai/) API key for chat, embeddings and OCR

## Setup (first time)

```powershell
# 1. Create and activate the virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install the package with dev tools
pip install -e ".[dev]"

# 3. Create your local env file (never commit the real .env)
copy .env.example .env

# 4. Edit .env and paste your Mistral key:
#      MISTRAL_API_KEY=sk-xxxxxxxxxxxxxxxx
```

Linux/macOS equivalents: `python3 -m venv .venv`, `source .venv/bin/activate`,
`cp .env.example .env`.

## Run the backend

The backend serves on **port 8000** and needs three things to answer with document
context: the Postgres DB (DB name `Knowledge_Gen_Agent`), a valid `MISTRAL_API_KEY`,
and a running Mistral connection.

**Option A — one-command launcher (Windows):**

```powershell
.\start.ps1
```

`start.ps1` pins the database name, frees port 8000 from stale servers, and starts
Uvicorn at `http://127.0.0.1:8000`.

**Option B — plain Uvicorn:**

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Verify it is up:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health   # -> {"status":"ok"}
```

Interactive API docs: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## Run tests / linters

```powershell
.\.venv\Scripts\python.exe -m pytest          # full suite (132 tests)
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m mypy app
```

## Docker

```powershell
docker build -t knowledge_generative_agent .
docker run -p 8000:8000 `
  -e MISTRAL_API_KEY=your_key `
  -e DATABASE_URL=postgresql+psycopg2://postgres:postgres@db:5432/Knowledge_Gen_Agent `
  knowledge_generative_agent
```

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `MISTRAL_API_KEY` | *required* | Mistral AI API key |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5433/Knowledge_Gen_Agent` | Full SQLAlchemy URL (overrides `DB_*`) |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `localhost` / `5433` / `Knowledge_Gen_Agent` / `postgres` / `postgres` | Individual DB settings |
| `MISTRAL_BASE_URL` | `https://api.mistral.ai/v1` | Mistral API base |
| `MISTRAL_CHAT_MODEL` | `open-mistral-nemo` | Chat model |
| `MISTRAL_EMBEDDING_MODEL` | `mistral-embed` | Embedding model |
| `MISTRAL_OCR_MODEL` | `mistral-ocr-latest` | OCR model |
| `MISTRAL_TIMEOUT_SECONDS` | `30` | HTTP timeout (seconds) |
| `MISTRAL_RETRIES` | `3` | Retry count |
| `RAG_CHUNK_SIZE` | `500` | Text chunk size (chars) |
| `RAG_TOP_K` | `5` | RAG retrieval count |
| `CONVERSATION_MAX_HISTORY` | `10` | In-memory context window |
| `CORS_ORIGINS` | `["http://localhost:4200"]` | Allowed frontend origins (JSON array) |
| `LOG_LEVEL` | `INFO` | Logging level |

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/chat` | Send message (JSON or multipart) |
| `GET` | `/api/history` | Chat history for session |
| `GET` | `/api/history/sessions` | Recent sessions |
| `DELETE` | `/api/history/sessions/{sessionId}` | Delete session (messages + documents + chunks) |
| `POST` | `/api/rag/ingest` | Upload a document for RAG |
| `GET` | `/api/rag/documents` | List attached documents for the session |
| `DELETE` | `/api/rag/documents/{documentId}` | Remove one document + its chunks |
| `POST` | `/api/rag/query` | RAG query |
| `POST` | `/api/mistral/upload` | Upload a file via Mistral API (OCR) |
| `POST` | `/api/feedback` | Submit feedback |
| `GET` | `/health` | Health check |

All chat/RAG/document endpoints are scoped by the optional `X-Session-ID` header
(defaults to `default-session`).

## Supported document formats

PDF, DOCX, XLSX, PPTX, TXT/Markdown (`.txt`, `.md`, `.log`, `.rst`), CSV/TSV, JSON,
HTML (`.html`, `.htm`), and images (`.png`, `.jpg`, ...) via Mistral OCR.
Unsupported types return HTTP 400.
