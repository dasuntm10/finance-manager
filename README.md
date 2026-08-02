# Agentic Finance Manager

A personal finance assistant that processes SMS messages and PDF bank documents, categorizes transactions, tracks budgets, compares prices, and answers finance research questions - all powered by an LLM-orchestrated agent pipeline built with Langgraph.

## Features

- **SMS, Email & PDF Ingestion** - parse bank SMS alerts, IMAP bank emails, and PDF statements into structured transactions
- **Auto-categorization** - rule-based classification (Food, Transport, Shopping, etc.)
- **Budget Alerts** - per-category and total limits, with over-budget warnings and an end-of-month projection based on the current pace
- **Subscription Detection** - finds recurring charges, their cadence and monthly cost, flags price increases and lapsed series
- **Monthly Reports** - month-over-month breakdown with a written summary, downloadable as standalone HTML
- **Analytics Dashboard** - monthly trends, category breakdown, 30-day rolling averages
- **Price Comparison** - web search for competitor prices and coupons
- **Finance Research** - Q&A with web search summaries and cited sources; supports audio input

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + uvicorn |
| Agent Orchestration | LangGraph |
| LLM Router | liteLLM (default: `gemini-2.5-flash`) |
| Frontend | Streamlit + Plotly |
| Vector DB | Qdrant (optional) |
| Web Search | Scrapeless (HTTP) or Tavily (MCP) |
| Speech-to-Text | Whisper or Qwen 2.5B |
| Packaging | uv (`pyproject.toml`) |

## Quick Start

**Prerequisites:** Python 3.10+, [uv](https://docs.astral.sh/uv/)

```bash
# Install dependencies
uv sync

# Copy and configure environment
cp .env.example .env   # edit LLM_MODEL, API keys, etc.

# Start API server
uv run finance-manager-api        # http://localhost:8000

# Start UI (separate terminal)
uv run finance-manager-ui         # http://localhost:8501
```

**Full local stack with Docker:**

```bash
docker compose -f docker-compose.selfhost.yml up -d
# API:      http://localhost:8000
# UI:       http://localhost:8501
# Postgres: localhost:5432
# Qdrant:   http://localhost:6333
```

## Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MODEL` | `gemini-2.5-flash` | LLM for categorization & summaries |
| `DATABASE_URL` | (in-memory) | Postgres connection string |
| `QDRANT_URL` | `http://localhost:6333` | Vector DB for document embeddings |
| `SEARCH_MODE` | `http` | `http` (Scrapeless) or `mcp` (Tavily) |
| `SCRAPELESS_API_KEY` | - | Required for HTTP search mode |
| `TAVILY_API_KEY` | - | Required for MCP search mode |
| `STT_PROVIDER` | `whisper` | `whisper` or `qwen` for audio input |
| `OPENAI_API_KEY` | - | For Whisper transcription |
| `HF_TOKEN` | - | For Qwen 2.5B transcription |
| `IMAP_HOST` | - | Mail server for email ingestion |
| `IMAP_USERNAME` | - | Mailbox user; use an app password where the provider offers one |
| `IMAP_PASSWORD` | - | Mailbox password |
| `BUDGET_WARN_THRESHOLD` | `0.8` | Fraction of a limit that triggers a warning |
| `RECURRING_MIN_OCCURRENCES` | `3` | Charges needed before a merchant counts as recurring |

See `ARCHITECTURE.md` for the full configuration reference and system design.

## Email Ingestion

Bank emails carry a real transaction date in the `Date` header and a stable
`Message-ID`, so they date transactions correctly and re-fetching the same window
does not create duplicates. Mailboxes are opened read-only, so ingestion never
marks mail as seen.

```bash
export IMAP_HOST=imap.gmail.com
export IMAP_USERNAME=you@example.com
export IMAP_PASSWORD=your-app-password

curl -X POST localhost:8000/ingest/email \
  -H 'content-type: application/json' \
  -d '{"user_id": "demo-user", "since_days": 30}'
```

Restrict which senders can produce transactions by setting
`email_allowed_senders` (addresses or bare domains). An empty list accepts all
senders. Messages that contain no recognizable amount are skipped.

## Reports

```bash
# JSON
curl -X POST localhost:8000/reports/monthly \
  -H 'content-type: application/json' \
  -d '{"user_id": "demo-user", "month": "2026-08"}'

# Standalone HTML, print to PDF from a browser
curl 'localhost:8000/reports/monthly.html?user_id=demo-user&month=2026-08' -o report.html
```

Every figure in a report is computed from stored transactions before the LLM is
called. The model only writes prose over numbers that are already fixed, and a
deterministic summary is used when no model is reachable, so an outage changes
the wording and never the figures.

## Project Structure

```
finance_manager/
├── config.py         # Pydantic settings
├── schemas.py        # Data models (Transaction, Budget, Document, FinanceState)
├── graph.py          # LangGraph pipeline (all agent nodes)
├── db.py             # Repository abstraction (in-memory stub)
├── vector_store.py   # Qdrant wrapper + NullVectorStore fallback
├── analysis/         # Recurring charge detection, budget alerting
├── ingestion/        # Shared bank-message parsing, IMAP email client
├── reports/          # Monthly report builder, narrative, HTML renderer
├── api/main.py       # FastAPI REST endpoints
└── ui/app.py         # Streamlit web UI
```

## Tests

```bash
uv sync --extra dev
uv run pytest
```

## Deployment

Deployment guides for Fly.io, Render, and self-hosted Docker are in [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Status

Core pipeline and UI are functional with an in-memory repository. Swap `get_repository()` in `db.py` for a Postgres-backed implementation before running in production.

Known gaps and planned work are tracked in [`BACKLOG.md`](BACKLOG.md).
