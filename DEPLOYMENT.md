# Deployment

Low-cost deployment options for the Agentic Finance Manager: Fly.io (recommended), Render, or a self-hosted VM with Docker Compose, plus managed Postgres (Supabase/Neon) and optional Qdrant.

## Components
- API: FastAPI + LangGraph (`Dockerfile.api`, listens on `${PORT:-8000}`)
- UI: Streamlit + Plotly (`Dockerfile.ui`, listens on `${PORT:-8501}`), with audio upload -> speech-to-text (Whisper/Qwen) for research input
- Postgres: managed (Supabase/Neon) or self-hosted (compose)
- Qdrant (optional): hosted (Qdrant Cloud) or self-hosted (compose)
- Search: HTTP Scrapeless (default) or Tavily MCP (`SEARCH_MODE=mcp`, `TAVILY_API_KEY`)

## Environment Variables (common)
- `LLM_MODEL` (default `gemini-2.5-flash`)
- `DATABASE_URL` (Postgres connection string)
- `QDRANT_URL`, `QDRANT_API_KEY` (optional)
- `SCRAPELESS_API_KEY` (HTTP search)
- `SEARCH_MODE` = `http` | `mcp` (default `http`)
- `MCP_SERVER_URL` (optional); `TAVILY_API_KEY` (auto-builds Tavily MCP URL)
- `STT_PROVIDER` = `whisper` | `qwen` (default `whisper`)
- `OPENAI_API_KEY` (for Whisper), `HF_TOKEN` (for Qwen via Hugging Face Inference)
- `DOC_AI_ENDPOINT`, `DOC_AI_API_KEY` (optional)
- `PLAYWRIGHT_BROWSER` (optional)

## Fly.io
Files: `Dockerfile.api`, `Dockerfile.ui`, `fly.api.toml`, `fly.ui.toml`

Two separate Fly apps - one for the API, one for the UI. App name and region are configured in the respective `.toml` files. Secrets (e.g., `DATABASE_URL`, `LLM_MODEL`) are set via `fly secrets set`. Postgres can be a Fly-managed instance (`fly postgres create`) or an external provider (Supabase/Neon). Qdrant can be hosted on Qdrant Cloud or run as an additional Fly app.

## Render
File: `render.yaml`

Repo connected to Render; `render.yaml` defines two web services (api/ui) backed by `Dockerfile.api` and `Dockerfile.ui`. Env vars are set per service - at minimum `DATABASE_URL`, optionally search/LLM/Qdrant keys.

## Self-hosted VM
File: `docker-compose.selfhost.yml`

Targets a small VM (e.g., Hetzner CX11 / Lightsail) with Docker and Compose installed. The compose file includes Postgres and Qdrant services; env vars (e.g., `DATABASE_URL`, `QDRANT_URL`) are configured there. A reverse proxy (Caddy/NGINX) is needed for TLS/custom domains.

## Managed Postgres
- Supabase/Neon free tier: connection string set as `DATABASE_URL`.
- Fly Postgres: provisioned with `fly postgres create`, attached or referenced via `DATABASE_URL`.

## Optional Qdrant
- Hosted: Qdrant Cloud free tier; requires `QDRANT_URL` and `QDRANT_API_KEY`.
- Self-host: `qdrant` service in `docker-compose.selfhost.yml` or a dedicated Fly/Render service using the official image.

## Notes
- Ports: API 8000, UI 8501 (overridable via `PORT` env; already wired in Docker CMD).
- Health check: `GET /health` on API.
- Logging: structlog JSON to stdout.
- Lowest cost: Render free tier or a single small VM with managed Postgres (Supabase/Neon); Qdrant optional.
