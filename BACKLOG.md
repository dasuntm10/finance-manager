# Backlog

Work that is identified and scoped but not yet built. Ordered by priority within
each section. Items marked **blocker** prevent other work or prevent the system
from running at all.

Delivered in the current pass and therefore not listed here: recurring charge
detection, budget alerts, email ingestion, monthly reports. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how those fit together.

## 1. Blockers

These stop the project from building, deploying, or persisting anything.

| Item | Detail |
|------|--------|
| **No real persistence** | `db.py` only ships `InMemoryRepository`; all data is lost on restart, and because the Streamlit UI invokes the graph in-process rather than calling the API, the UI and API each hold a separate store. Implement the Postgres repository behind the existing `FinanceRepository` interface (`psycopg` is already a dependency) and add Alembic migrations. This is the single highest-value change in the backlog. |
| **Missing `uv.lock`** | `Dockerfile.api` and `Dockerfile.ui` both run `uv sync --frozen`, which fails without a lockfile. All three deployment paths (Fly, Render, docker compose) are broken until `uv lock` is committed. |
| **Missing `.env.example`** | `README.md` instructs `cp .env.example .env`, but the file does not exist. |
| **No authentication** | Every endpoint takes `user_id` straight from the client payload, so any caller can read or write any user's transactions and budgets. Needed before this is deployed anywhere reachable. Pairs with the `allow_origins=["*"]` plus `allow_credentials=True` CORS combination in `api/main.py`, which browsers reject and which signals intent to allow credentialed cross-origin calls to an unauthenticated API. |

## 2. Ingestion quality

| Item | Detail |
|------|--------|
| **SMS parser still uses the naive regex** | `_simple_amount_parser` in `graph.py` takes the first number in the message, which is often a date, card suffix or account number, and it records no transaction date, so every SMS transaction is stamped with the ingestion time. That destroys monthly and rolling analytics for any historical import. The email path already uses the better parser in `finance_manager/ingestion/parsing.py`; point the SMS node at `parse_money`, `parse_direction`, `parse_merchant` and add SMS date parsing. |
| **SMS ingestion has no debit or credit distinction** | Refunds add to spend instead of netting out. `parse_direction` already exists and is used by the email path. |
| **SMS re-ingestion duplicates rows** | Dedupe keys on the timestamp, which for SMS is always `now()` at microsecond resolution, so the fingerprint never matches. Hash the raw message text into `source_doc_id` the way the email path uses `Message-ID`. |
| **PDF extraction is non-functional without DocumentAI** | `pdf_ingestion` calls `read_text()` on a binary PDF and then makes a transaction out of every line containing a digit. Add `pypdf` or `pdfplumber` for real text extraction, and parse statement tables rather than lines. |
| **Salary slip and income entries** | The `Document` type includes `salary_slip` but no pipeline produces income entries. |
| **`Document` is never persisted** | `pdf_ingestion` builds raw dicts and the repository has no document methods, so the `Document` model is unused. |

## 3. LLM usage

The provider switching in `config/llm.yaml` works, but the model is called in
only two places: research Q&A and the monthly report narrative.

| Item | Detail |
|------|--------|
| **LLM fallback for SMS parsing** | Use a structured-output call for messages the regex cannot parse confidently, keeping the regex as the fast path. |
| **LLM categorization fallback** | `_categorize` is an eight-keyword dictionary. Route unknown merchants to the model and cache the merchant-to-category result so each merchant is paid for once. |
| **Ground the research answer in its sources** | `make_research_node` fetches search results and then never passes them to the model, so the cited sources and the answer are unrelated. |
| **Real embeddings** | Qdrant upserts use all-zero 768-dimension vectors, so cosine similarity is undefined and semantic search is meaningless. Add an embedding call and unlock question answering over stored statements. |
| **Retry policy** | `tenacity` is a declared dependency and is never used; LLM and search calls have no retry. |

## 4. Performance

| Item | Detail |
|------|--------|
| **Analytics rolling average is O(n squared)** | `make_analytics_node` rescans the whole sorted list for every transaction. Replace with a two-pointer sliding window, or push the aggregation into SQL once Postgres exists. |
| **Blocking HTTP inside an async node** | The DocumentAI call uses synchronous `httpx.post` inside the async PDF node and can stall the event loop for up to 30 seconds. Switch to `httpx.AsyncClient`. |
| **Serial file and search handling** | PDF ingestion loops over files one at a time; search calls are also serial. Use `asyncio.gather`. |
| **No caching layer** | `redis` is a declared dependency and `settings.redis_url` is never read. Search results and LLM responses are recomputed every time. |
| **Streamlit event loop churn** | `ui/app.py` calls `asyncio.run` per interaction, creating and tearing down a loop each time. |

## 5. Features not yet built

| Item | Detail |
|------|--------|
| **Transactions tab backed by the repository** | The tab currently shows only the most recent ingest from session state, not stored history. Add filtering, search, and inline category editing; corrections can later feed the categorizer as few-shot examples. |
| **Anomaly detection** | Flag transactions far outside a merchant's or category's historical distribution. A z-score is enough before reaching for anything heavier. |
| **True PDF export for reports** | Reports render as standalone HTML with print styles, so browser "Save as PDF" works. A server-side PDF needs WeasyPrint or a headless browser; WeasyPrint's GTK dependencies are awkward on Windows. |
| **Scheduled report and alert delivery** | Generate the monthly report on a schedule and deliver budget alerts by email or push instead of only on request. |
| **Per-transaction price comparison** | The spec describes a "Compare prices" action per transaction row; only the standalone endpoint exists. |
| **Settings page** | Specified in `finance_manager.md`, not built. |
| **Coupon extraction** | `PriceCompareResult.coupons` is defined and never populated. |
| **WebSocket streaming** | Specified for long-running ingestion progress, not built. |
| **Multi-currency handling** | Amounts in different currencies are summed directly. Needs FX conversion to a base currency before totals mean anything for mixed-currency users. |

## 6. Security and privacy

| Item | Detail |
|------|--------|
| **PII masking** | `finance_manager.md` requires card-number masking, but full SMS and email text is stored in `description` and in the Qdrant payload, and is sent to the LLM unmasked. |
| **IMAP credential handling** | Credentials come from the environment as a plain password. Prefer provider app passwords, and consider OAuth for Gmail and Outlook. |
| **Uvicorn dev settings in the entry point** | `api/main.py:run()` hardcodes `reload=True` and `port=8000`, ignoring `$PORT`. Only affects `uv run finance-manager-api`, since the Dockerfiles invoke uvicorn directly. |
| **Windows tempfile handling** | `/ingest/pdf` uses `NamedTemporaryFile(delete=False)` without closing the handle before the graph reads it, which fails on Windows. |

## 7. Hygiene

| Item | Detail |
|------|--------|
| **No CI** | Tests exist now but nothing runs them automatically. Add a GitHub Actions workflow running pytest on push. |
| **No linter or formatter** | No ruff, black or mypy configuration, and no pre-commit hooks. |
| **Unused dependencies** | `beautifulsoup4` is now used by the email HTML fallback, but `playwright`, `tenacity`, and `redis` remain declared and unused, and `psycopg` stays unused until the Postgres repository lands. |
| **Deprecated APIs** | `datetime.utcnow()` throughout, Pydantic v1 `class Config` in `config.py`, `recreate_collection` and Pydantic v1 `.dict()` in `vector_store.py`. |
| **Dead setting** | `settings.llm_model` is no longer read after the `llm.yaml` refactor, and its default lacks the `gemini/` liteLLM prefix, so following the README and setting `LLM_MODEL` produces a call that fails. |
| **Config documentation mismatch** | `ARCHITECTURE.md` documents `DEFAULT_CATEGORIES` and `DEFAULT_BANK_SENDERS` as environment variables, but those fields have no alias and are typed `List[str]`, so they need lowercase names and JSON-encoded values. |
| **Committed bytecode** | `__pycache__` directories hold Python 3.9 bytecode while the project requires 3.10 or newer. They are gitignored but still present on disk. |
