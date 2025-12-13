# Deployment Architecture & Steps

This guide covers low-cost deployment options for the Agentic Finance Manager using Fly.io (recommended), Render, or a self-hosted VM with Docker Compose, plus managed Postgres (Supabase/Neon) and optional Qdrant.

## Components
- API: FastAPI + LangGraph (`Dockerfile.api`, listens on `${PORT:-8000}`)
- UI: Streamlit + Plotly (`Dockerfile.ui`, listens on `${PORT:-8501}`), with audio upload → speech-to-text (Whisper/Qwen) for research input
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

## Fly.io (API and UI as separate apps)
Files: `Dockerfile.api`, `Dockerfile.ui`, `fly.api.toml`, `fly.ui.toml`

1) Install CLI: `curl -L https://fly.io/install.sh | sh`
2) Auth: `fly auth login`
3) API app:
   - Set app name/region in `fly.api.toml` (`app`, `primary_region`)
   - `fly launch --config fly.api.toml --no-deploy`
   - Set secrets, e.g.: `fly secrets set DATABASE_URL=... LLM_MODEL=...`
   - Deploy: `fly deploy --config fly.api.toml`
4) UI app:
   - Set app name/region in `fly.ui.toml`
   - `fly launch --config fly.ui.toml --no-deploy`
   - Set secrets if needed (e.g., `API_BASE_URL` pointing to API)
   - Deploy: `fly deploy --config fly.ui.toml`
5) Postgres: use Fly Postgres (`fly postgres create`) or external (Supabase/Neon). Provide `DATABASE_URL` to API.
6) Optional Qdrant: host on Qdrant Cloud or run a small Fly app; set `QDRANT_URL`.

## Render (free-tier friendly)
File: `render.yaml`

1) Connect repo to Render.
2) Render auto-detects `render.yaml`: two web services (api/ui) using `Dockerfile.api`/`Dockerfile.ui`.
3) Set env vars per service (at minimum `DATABASE_URL`; optionally search/LLM/Qdrant keys).
4) Deploy.

## Self-hosted VM (Docker Compose)
File: `docker-compose.selfhost.yml`

1) Provision a small VM (e.g., Hetzner CX11 / Lightsail).
2) Install Docker + Compose.
3) `docker compose -f docker-compose.selfhost.yml up -d`
4) Adjust env in the compose file (uncomment `DATABASE_URL`, `QDRANT_URL`, etc., or point to managed Postgres/Qdrant).
5) Put Caddy/NGINX in front if you need TLS/domains.

## Managed Postgres
- Supabase/Neon free tier: create DB, grab connection string, set `DATABASE_URL`.
- Fly Postgres: `fly postgres create` then `fly postgres attach` or copy the connection string as `DATABASE_URL`.

## Optional Qdrant
- Hosted: Qdrant Cloud free tier; set `QDRANT_URL`/`QDRANT_API_KEY`.
- Self-host: enable `qdrant` service in `docker-compose.selfhost.yml` or run a Fly/Render service with the official image.

## Notes
- Ports: API 8000, UI 8501 (overridable via `PORT` env; already wired in Docker CMD).
- Health check: `GET /health` on API.
- Logging: structlog JSON to stdout.
- For lowest cost: start with Render free tier or a single small VM; add managed Postgres (Supabase/Neon) and skip Qdrant unless needed.

