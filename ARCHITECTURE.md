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
│         ├──► PDF path                        │
│         │    pdf_ingestion → extraction      │
│         │    → categorization → persistence  │
│         │    → budget → END                 │
│         │                                   │
│         ├──► analytics → END                │
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
| Speech-to-text | Whisper (OpenAI) or Qwen 2.5B (HF) | `finance_manager/graph.py` |
| Logging | structlog (JSON) | `finance_manager/logger.py` |
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
| `DEFAULT_CURRENCY` | `USD` | Fallback currency for transactions |
| `DEFAULT_CATEGORIES` | Food, Transport, … | Transaction category list |
| `DEFAULT_BANK_SENDERS` | BOC, HNB, … | Allowed SMS sender names |

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
| `pdf_upload` | `pdf_ingestion → extraction → categorization → persistence → budget` |
| `analytics` | `analytics` |
| `price_compare` | `price_compare` |
| `research` | `research` |

### Node Responsibilities

| Node | Responsibility |
|------|---------------|
| **Router** | Dispatches by `task_type`; defaults to error handler on unknown type |
| **SMSIngestion** | Filters by `DEFAULT_BANK_SENDERS`; regex + LLM amount/currency parse |
| **PDFIngestion** | DocumentAI extraction (optional); plain-text fallback; Qdrant upsert |
| **Extraction** | Constructs `Transaction` objects from parsed SMS/PDF text |
| **Categorization** | Heuristic rule assignment; falls back to LLM classification |
| **Persistence** | Upserts transactions to repository (in-memory stub) |
| **Budget** | Aggregates spend vs. stored `Budget`; returns summary |
| **Analytics** | Monthly totals, category distribution, 30-day rolling average |
| **PriceCompare** | Web search for competitor prices / coupons via MCP or HTTP |
| **Research** | Web search + optional LLM summarization; returns answer + sources |
| **ErrorHandler** | Catches exceptions; logs with `request_id`; populates `errors[]` |

Logging: structlog JSON; every node logs start/end with `request_id`.

## Search Modes

**HTTP (default):** Scrapeless endpoint — requires `SCRAPELESS_API_KEY`.

**MCP (Tavily):** Active when `SEARCH_MODE=mcp`. Uses `MCP_SERVER_URL` if set; otherwise derives `https://mcp.tavily.com/mcp/?tavilyApiKey=<TAVILY_API_KEY>`. Falls back to HTTP mode on failure.

## API Endpoints (FastAPI)

| Method | Path | Pipeline triggered |
|--------|------|--------------------|
| `GET` | `/health` | — |
| `POST` | `/ingest/sms` | `sms_batch` pipeline |
| `POST` | `/ingest/pdf` | `pdf_upload` pipeline |
| `POST` | `/analytics` | `analytics` node |
| `GET` / `POST` | `/budget` | Budget read / write |
| `POST` | `/price-compare` | `price_compare` node |
| `POST` | `/research` | `research` node |

## UI (Streamlit — `ui/app.py`)

Six tabs, all invoking `FinanceGraphRunner` directly:

| Tab | Content |
|-----|---------|
| **Dashboard** | Monthly spend line chart, category pie/bar, rolling average, budget progress gauge |
| **Transactions** | Searchable / filterable transaction table |
| **Ingest SMS** | Batch SMS text input form |
| **Ingest PDF** | File uploader for PDF bank statements |
| **Budget** | Per-category monthly limit form |
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
| **Email ingestion** | Specified in `finance_manager.md`; not yet implemented |
| **Salary slip income** | Data model exists; pipeline not wired |
| **Report generation** | Jinja2 + WeasyPrint planned; out of MVP scope |
| **Real embeddings** | Qdrant upsert uses dummy vectors by default |
