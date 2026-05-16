# Agentic Finance Manager

A personal finance assistant that processes SMS messages and PDF bank documents, categorizes transactions, tracks budgets, compares prices, and answers finance research questions - all powered by an LLM-orchestrated agent pipeline built with Langgraph.

## Features

- **SMS & PDF Ingestion** - parse bank SMS alerts and PDF statements into structured transactions
- **Auto-categorization** - rule-based + LLM classification (Food, Transport, Shopping, etc.)
- **Budget Tracking** - set monthly limits by category; dashboard shows spend vs. limit
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

See `ARCHITECTURE.md` for the full configuration reference and system design.

## Project Structure

```
finance_manager/
├── config.py         # Pydantic settings
├── schemas.py        # Data models (Transaction, Budget, Document, FinanceState)
├── graph.py          # LangGraph pipeline (all agent nodes)
├── db.py             # Repository abstraction (in-memory stub)
├── vector_store.py   # Qdrant wrapper + NullVectorStore fallback
├── api/main.py       # FastAPI REST endpoints
└── ui/app.py         # Streamlit web UI
```

## Deployment

Deployment information for Fly.io, Render, and self-hosted Docker are in [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Status

Core pipeline and UI are functional with an in-memory repository. Swap `get_repository()` in `db.py` for a Postgres-backed implementation before running in production.
