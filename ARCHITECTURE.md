# Agentic Finance Manager – Architecture Overview

## System Components

```
┌─────────────────────────────────────────────┐
│              Streamlit UI (8501)             │
│  Dashboard | Transactions | Ingest | Budget  │
│            | Research Q&A                   │
└────────────────────┬────────────────────────┘
                     │ invokes directly
┌────────────────────▼────────────────────────┐
│           FastAPI REST API (8000)            │
│  /ingest/sms  /ingest/pdf  /analytics       │
│  /budget      /price-compare  /research     │
└────────────────────┬────────────────────────┘
                     │
┌────────────────────▼────────────────────────┐
│          LangGraph Pipeline (graph.py)       │
│                                              │
│  Router ──► SMS path                         │
│         │    sms_ingestion → extraction      │
│         │    → categorization → persistence  │
│         │    → budget → END                 │
│         │                                   │
│         ├──► Email path                      │
│         │    email_ingestion → extraction    │
│         │    → categorization → persistence  │
│         │    → budget → END                 │
│         │                                   │
│         ├──► PDF path                        │
│         │    pdf_ingestion → extraction      │
│         │    → categorization → persistence  │
│         │    → budget → END                 │
│         │                                   │
│         ├──► analytics → END                │
│         ├──► budget → END                   │
│         ├──► recurring → END                │
│         ├──► report → END                   │
│         ├──► price_compare → END            │
│         └──► research → END                 │
└──────┬──────────┬──────────────┬────────────┘
       │          │              │
  ┌────▼───┐ ┌───▼────┐  ┌──────▼──────┐
  │liteLLM │ │Qdrant  │  │Web Search   │
  │(LLM    │ │(Vector │  │Scrapeless / │
  │Router) │ │Store)  │  │Tavily MCP   │
  └────────┘ └────────┘  └─────────────┘
```

## Stack

| Component | Technology | File |
|-----------|-----------|------|
| Language | Python 3.10+ | — |
| Backend | FastAPI + uvicorn | `finance_manager/api/main.py` |
| Orchestration | LangGraph | `finance_manager/graph.py` |
| LLM routing | liteLLM; provider/model from `config/llm.yaml` | `finance_manager/llm.py`, `finance_manager/llm_config.py` |
| UI | Streamlit + Plotly | `finance_manager/ui/app.py` |
| Data models | Pydantic v2 | `finance_manager/schemas.py` |
| Repository | In-memory stub → Postgres | `finance_manager/db.py` |
| Vector DB | Qdrant (falls back to no-op) | `finance_manager/vector_store.py` |
| Search | HTTP Scrapeless or Tavily MCP | `finance_manager/graph.py` |
| Email ingestion | imaplib (stdlib), read-only IMAP | `finance_manager/ingestion/email_client.py` |
| Message parsing | Shared amount/currency/merchant/direction parsing | `finance_manager/ingestion/parsing.py` |
| Derived analysis | Recurring detection, budget alerting | `finance_manager/analysis/` |
| Reports | Jinja2, standalone HTML with print styles | `finance_manager/reports/` |
| Speech-to-text | Whisper (OpenAI) or Qwen 2.5B (HF) | `finance_manager/ui/app.py` |
| Logging | structlog (JSON) | `finance_manager/logger.py` |
| Tests | pytest + pytest-asyncio | `tests/` |
| Packaging | uv / `pyproject.toml` | `pyproject.toml` |

## Configuration (Environment Variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_PROVIDER` | `gemini` | Active provider key from `config/llm.yaml` (`gemini` or `anthropic`); overrides `active_provider` |
| `LLM_CONFIG_FILE` | `config/llm.yaml` | Path to the LLM provider config file |
| `LLM_MODEL` | (from config) | Overrides the active provider's model string |
| `GEMINI_API_KEY` | - | API key for the `gemini` provider |
| `ANTHROPIC_API_KEY` | - | API key for the `anthropic` provider |
| `DATABASE_URL` | `postgresql+psycopg://...` | Postgres connection string |
| `QDRANT_URL` | `http://localhost:6333` | Vector DB endpoint |
| `QDRANT_API_KEY` | — | Vector DB auth token |
| `REDIS_URL` | — | Optional session / cache store |
| `DOC_AI_ENDPOINT` | — | DocumentAI API endpoint (PDF extraction) |
| `DOC_AI_API_KEY` | — | DocumentAI API key |
| `SCRAPELESS_API_KEY` | — | HTTP search API key |
| `SEARCH_MODE` | `http` | `http` (Scrapeless) or `mcp` (Tavily) |
| `MCP_SERVER_URL` | — | Custom MCP server URL |
| `TAVILY_API_KEY` | — | Auto-derives Tavily MCP URL when set |
| `STT_PROVIDER` | `whisper` | `whisper` or `qwen` for audio input |
| `OPENAI_API_KEY` | — | Required for Whisper transcription |
| `HF_TOKEN` | — | Required for Qwen 2.5B transcription |
| `PLAYWRIGHT_BROWSER` | `chromium` | Browser engine for web scraping |
| `IMAP_HOST` | - | Mail server for email ingestion |
| `IMAP_PORT` | `993` | IMAP port |
| `IMAP_USERNAME` | - | Mailbox user |
| `IMAP_PASSWORD` | - | Mailbox password or provider app password |
| `IMAP_FOLDER` | `INBOX` | Folder to read |
| `IMAP_USE_SSL` | `true` | Use IMAP4_SSL rather than plain IMAP4 |
| `EMAIL_SINCE_DAYS` | `30` | How far back a fetch looks |
| `EMAIL_FETCH_LIMIT` | `200` | Cap on messages per fetch |
| `BUDGET_WARN_THRESHOLD` | `0.8` | Fraction of a limit that raises a warning |
| `RECURRING_MIN_OCCURRENCES` | `3` | Charges needed to qualify as a recurring series |
| `RECURRING_UPCOMING_DAYS` | `14` | Lookahead window for upcoming charges |
| `DEFAULT_CURRENCY` | `USD` | Fallback currency for transactions |
| `DEFAULT_CATEGORIES` | Food, Transport, … | Transaction category list |
| `DEFAULT_BANK_SENDERS` | BOC, HNB, … | Allowed SMS sender names |

`DEFAULT_CATEGORIES`, `DEFAULT_BANK_SENDERS` and `email_allowed_senders` are
list-typed fields without an alias, so they are set with lowercase names and
JSON-encoded values (for example `default_categories='["Food","Rent"]'`).

## LLM Provider Configuration

The LLM is provider-agnostic via liteLLM. Provider profiles and per-model
settings live in [`config/llm.yaml`](config/llm.yaml); secrets stay in the
environment.

```yaml
active_provider: gemini   # switch to "anthropic" to use Claude

providers:
  gemini:
    model: gemini/gemini-2.5-flash
    api_key_env: GEMINI_API_KEY
    params: { temperature: 0.2, max_tokens: 1024 }
  anthropic:
    model: anthropic/claude-haiku-4-5
    api_key_env: ANTHROPIC_API_KEY
    params: { temperature: 0.2, max_tokens: 1024 }
```

**To switch from Gemini to Anthropic:** set `active_provider: anthropic` in the
file, or set `LLM_PROVIDER=anthropic` in the environment, and provide
`ANTHROPIC_API_KEY`. Model strings use liteLLM's `<provider>/<model>` format.
Resolution order is `LLM_PROVIDER` (which profile) then `LLM_MODEL` (model
string override) then the file. `drop_params` is enabled, so settings a given
model rejects (for example `temperature` on Opus / Fable) are dropped rather
than erroring. Loader: `finance_manager/llm_config.py`; call helper:
`finance_manager/llm.py`.

## Data Models (Pydantic)

```
Transaction
  id, user_id, timestamp, amount, currency
  merchant_raw, merchant_normalized, category
  source (sms | pdf | email), tags[]

Budget
  user_id, month (YYYY-MM)
  total_limit, per_category_limits{}

Document
  id, user_id, type (bank_statement | salary_slip | bill)
  storage_path, extracted_text, metadata{}, embedding_id

FinanceState  ← LangGraph shared state
  user_id, request_id, task_type
  raw_input, files[]
  parsed_transactions[], parsed_documents[]
  analytics_result, price_compare_result
  research_answer, research_sources[]
  messages[], errors[]
```

**API models:** `AnalyticsRequest/Response`, `PriceCompareResult`, `ResearchResult`

## LangGraph Pipeline

### Task Types & Routing

| `task_type` | Pipeline |
|-------------|---------|
| `sms_batch` | `sms_ingestion → extraction → categorization → persistence → budget` |
| `email_batch` | `email_ingestion → extraction → categorization → persistence → budget` |
| `pdf_upload` | `pdf_ingestion → extraction → categorization → persistence → budget` |
| `analytics` | `analytics` |
| `budget` | `budget` |
| `recurring` | `recurring` |
| `report` | `report` |
| `price_compare` | `price_compare` |
| `research` | `research` |

### Node Responsibilities

| Node | Responsibility |
|------|---------------|
| **Router** | Dispatches by `task_type`; raises on an unknown type, which the API returns as a client error |
| **SMSIngestion** | Filters by `DEFAULT_BANK_SENDERS`; regex amount and currency parse (no LLM path yet, see `BACKLOG.md`) |
| **EmailIngestion** | Read-only IMAP fetch in a worker thread; parses amount, currency, direction, merchant and the real header date; skips non-transactional mail |
| **PDFIngestion** | DocumentAI extraction (optional); plain-text fallback; Qdrant upsert |
| **Extraction** | Constructs `Transaction` objects from parsed SMS/email/PDF text |
| **Categorization** | Heuristic keyword rules (no LLM path yet, see `BACKLOG.md`) |
| **Persistence** | Upserts transactions to repository (in-memory stub), deduplicating on a fingerprint that includes `source_doc_id` |
| **Budget** | Evaluates the month's spend against the stored `Budget`; emits `BudgetStatus` with per-category utilization, pace projection and alerts |
| **Recurring** | Groups history by normalized merchant, detects cadence, cost per month, price changes and lapsed series |
| **Report** | Assembles the monthly report and asks the LLM to write a summary over the fixed figures |
| **Analytics** | Monthly totals, category distribution, 30-day rolling average |
| **PriceCompare** | Web search for competitor prices / coupons via MCP or HTTP |
| **Research** | Web search + optional LLM summarization; returns answer + sources |

### Error Containment

LangGraph has no graph-level error hook, so containment is per node: every node
except the router is wrapped by `guard_node`, which catches an exception, logs it
with the `request_id`, and appends `"<node>: <message>"` to `errors[]`. The run
continues with whatever state it already has, and every endpoint returns
`errors` alongside its payload, so callers should check it rather than relying on
the HTTP status alone.

Logging: structlog JSON; every node logs start/end with `request_id`. The node
marker is logged as `phase`, since structlog reserves `event` for the message.

## Search Modes

**HTTP (default):** Scrapeless endpoint — requires `SCRAPELESS_API_KEY`.

**MCP (Tavily):** Active when `SEARCH_MODE=mcp`. Uses `MCP_SERVER_URL` if set; otherwise derives `https://mcp.tavily.com/mcp/?tavilyApiKey=<TAVILY_API_KEY>`. Falls back to HTTP mode on failure.

## API Endpoints (FastAPI)

| Method | Path | Pipeline triggered |
|--------|------|--------------------|
| `GET` | `/health` | — |
| `POST` | `/ingest/sms` | `sms_batch` pipeline |
| `POST` | `/ingest/email` | `email_batch` pipeline |
| `POST` | `/ingest/pdf` | `pdf_upload` pipeline |
| `POST` | `/analytics` | `analytics` node |
| `GET` / `POST` | `/budget` | Budget read / write |
| `GET` | `/budget/status` | `budget` node; spend, projection and alerts |
| `GET` | `/recurring` | `recurring` node |
| `POST` | `/reports/monthly` | `report` node, JSON payload |
| `GET` | `/reports/monthly.html` | `report` node, standalone HTML (`download=true` to attach) |
| `POST` | `/price-compare` | `price_compare` node |
| `POST` | `/research` | `research` node |

The three ingestion endpoints also return `budget_status`, so a client sees the
budget impact of what it just ingested without a second round trip.

`get_runner` returns a process-wide singleton, so the graph is compiled and the
Qdrant client opened once rather than per request.

## UI (Streamlit — `ui/app.py`)

Nine tabs, all invoking `FinanceGraphRunner` directly. A sidebar sets the active
`user_id` used for every run.

| Tab | Content |
|-----|---------|
| **Dashboard** | Monthly spend line chart, category pie/bar, rolling average, plus this month's budget status with alerts and projection |
| **Transactions** | Table of the most recent ingest (repository-backed history is in `BACKLOG.md`) |
| **Ingest SMS** | Batch SMS text input form |
| **Ingest Email** | IMAP connection status, folder and lookback controls, fetch and ingest, budget impact |
| **Ingest PDF** | File uploader for PDF bank statements |
| **Budget** | Per-category monthly limit form, persisted to the repository, with status and alerts |
| **Subscriptions** | Detected recurring charges, cadence, monthly and annual cost, upcoming charges, price increases |
| **Reports** | Monthly report with summary, highlights, category chart, and an HTML download |
| **Research** | Q&A text input + optional audio upload → STT → query; displays answer + cited sources |

## Vector Store

`VectorStore` wraps a Qdrant collection named `finance-documents`. When Qdrant is unavailable, `NullVectorStore` is used as a no-op fallback (document ingestion still works, semantic search is disabled).

## Deployment

Three supported paths (see [`DEPLOYMENT.md`](DEPLOYMENT.md)):

| Platform | Config file | Notes |
|----------|------------|-------|
| **Fly.io** | `fly.api.toml`, `fly.ui.toml` | Recommended; managed Postgres add-on |
| **Render** | `render.yaml` | Free tier; API + UI as separate services |
| **Self-hosted VM** | `docker-compose.selfhost.yml` | Postgres + Qdrant included |

**Containers:** `Dockerfile.api` (uvicorn, port 8000), `Dockerfile.ui` (Streamlit, port 8501), both based on `python:3.11-slim`.

## Known Stubs & Limitations

| Area | Status |
|------|--------|
| **Database** | `InMemoryRepository` used; swap `get_repository()` in `db.py` for Postgres |
| **DocumentAI** | Optional; falls back to plain-text extraction when endpoint unset |
| **SMS parsing** | Still the naive regex: takes the first number in the message and stamps the ingestion time rather than the transaction date. The better parser used by the email path lives in `ingestion/parsing.py` |
| **Salary slip income** | Data model exists; pipeline not wired |
| **Report export** | Standalone HTML with print styles; server-side PDF not implemented |
| **Real embeddings** | Qdrant upsert uses dummy vectors by default |
| **Authentication** | None; `user_id` is taken from the client payload on every endpoint |

Full backlog with priorities: [`BACKLOG.md`](BACKLOG.md).
