# Agentic Finance Manager – Architecture Overview

## Stack
- Language: Python 3.10+
- Backend: FastAPI (`finance_manager/api/main.py`)
- Orchestration: LangGraph (`finance_manager/graph.py`)
- LLM routing: liteLLM (default `gemini-2.5-flash`)
- UI: Streamlit + Plotly (`finance_manager/ui/app.py`)
- Vector DB: Qdrant (falls back to no-op)
- Search: HTTP Scrapeless or Tavily MCP (config switch)
- Speech-to-text (UI research input): Whisper (OpenAI) or Qwen 2.5B (HF), chosen via env
- Dependency manager: uv (`pyproject.toml`)

## Config (env vars)
- `LLM_MODEL`, `DATABASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `REDIS_URL`
- `DOC_AI_ENDPOINT`, `DOC_AI_API_KEY`
- `SCRAPELESS_API_KEY`
- `PLAYWRIGHT_BROWSER`
- `SEARCH_MODE` (`http`|`mcp`, default `http`)
- `MCP_SERVER_URL` (optional); `TAVILY_API_KEY` (auto-builds Tavily MCP URL)
- `STT_PROVIDER` (`whisper`|`qwen`, default `whisper`), `OPENAI_API_KEY` (Whisper), `HF_TOKEN` (Qwen)
- Defaults: categories, bank senders, currency

## Data Models (Pydantic)
- `Transaction`, `Budget`, `Document`
- `AnalyticsRequest/Response`, `PriceCompareResult`, `ResearchResult`
- `FinanceState` (graph state: user_id, request_id, raw_input, files, parsed_transactions/documents, analytics_result, price_compare_result, research_answer/sources, messages, errors)

## LangGraph Design
- Router: selects path by `task_type` (`sms_batch`, `pdf_upload`, `analytics`, `price_compare`, `research`).
- SMSIngestion: sender filter, simple amount/currency parse.
- PDFIngestion: optional DocumentAI extract; text fallback; optional Qdrant upsert (dummy vectors by default).
- Extraction: build `Transaction` objects from SMS/PDF text.
- Categorization: heuristic/rule-based category assignment with defaults.
- Persistence: repo upsert (in-memory stub; replace with Postgres).
- Budget: summary against stored budget.
- Analytics: monthly totals, category distribution, 30-day rolling average.
- PriceCompare: web search via MCP/HTTP helper; returns competitor snippets.
- Research: web search via MCP/HTTP; optional LLM summary.
- UI research tab supports audio upload → speech-to-text (Whisper/Qwen) → query
- ErrorHandler: captures/logs errors.
- Logging: structlog; per-request `request_id` logged at node start/end.

## Search Modes
- HTTP (default): Scrapeless endpoint using `SCRAPELESS_API_KEY` when present.
- MCP (Tavily): if `SEARCH_MODE=mcp`, uses `MCP_SERVER_URL`; if absent but `TAVILY_API_KEY` set, auto-derives `https://mcp.tavily.com/mcp/?tavilyApiKey=<key>`. Falls back to HTTP on failure.

## API Endpoints (FastAPI)
- `GET /health`
- `POST /ingest/sms` → sms pipeline
- `POST /ingest/pdf` → pdf pipeline
- `POST /analytics` → analytics node
- `POST /budget` / `GET /budget`
- `POST /price-compare`
- `POST /research`

## UI (Streamlit)
- Tabs: Dashboard (monthly spend line, category pie/bar, rolling average line, budget progress), Transactions table, Ingest SMS, Ingest PDF, Budget form, Research Q&A with sources.
- Invokes LangGraph runner directly.

## Vector Store
- `VectorStore` wraps Qdrant collection `finance-documents`; `NullVectorStore` when unavailable.

## Notes
- Persistence currently in-memory; swap `get_repository()` for Postgres-backed repo for durability.
- DocumentAI, Scrapeless, Playwright, Tavily MCP, liteLLM are optional; flows degrade gracefully when unset.

