````markdown
# Agentic Personal Finance Manager – Minimal Spec (SMS + PDFs + Analytics + Research)

## 1. Product Overview

This app is a **personal finance assistant** focused on:

1. **Ingestion**
   - Get spending data from **SMS** for specified bank contacts.
   - Get **salary slips / bank statements / bills** from PDFs.
   - Optionally auto-ingest relevant PDFs from **email inbox** based on sender/title.

2. **Core Logic**
   - User sets a **monthly budget**.
   - System **auto-categorizes** spending from SMS/PDFs based on **user-defined categories**.
   - **Analytics Agent** generates charts & summaries (monthly spend, category distributions, rolling averages).

3. **External Intelligence**
   - **PriceComparer Agent**: compares merchants/items/subscription prices & finds coupons/promo codes.
   - **Researcher Agent**: answers arbitrary user questions using web search, with **sources**.

Orchestration is done with **LangGraph**, using **litellm** to route LLM calls (default model: `gemini-2.5-flash`), and **Qdrant** as the vector DB for retrieval.

---

## 2. Technology Stack

### Core

- **Language**: Python 3.x
- **Backend**: FastAPI (REST + WebSockets for streaming)
- **Agent Orchestration**: LangGraph
- **LLM Orchestration**: litellm  
  - Default: `gemini-2.5-flash`  
  - Override via `LLM_MODEL` environment variable

### Retrieval & Web

- **Web search & scraping**: Scrapeless MCP Server (SERP + generic web search)
- **Dynamic pages**: Playwright (headless browser)
- **HTML parsing**: beautifulsoup4 (fallback)
- **Vector store**: Qdrant (embeddings for documents & user queries)

### Data & Storage

- **Primary DB**: PostgreSQL (users, transactions, budgets, documents)
- **Vector DB**: Qdrant
- **Cache**: Redis (optional, for sessions/cache)

### Documents & PDFs

- **OCR / document understanding**: Mistral DocumentAI (or equivalent OCR API)
- **(Optional) Reports later**: Jinja2 + HTML → PDF (WeasyPrint), but **not required** for this minimal scope.

### UI Layer

- **Front-end**: Streamlit (dashboard + chat)
  - Tabs:
    - Dashboard
    - Transactions
    - Budgets
    - Analytics (charts)
    - Research / Chat
    - Settings

---

## 3. Data Model (Minimal)

### 3.1 User

- `id`
- `email`
- `name`
- `default_currency`
- `created_at`
- `settings` (JSON; e.g., list of bank SMS senders, email rules)

### 3.2 Transaction

- `id`
- `user_id`
- `timestamp`
- `amount`
- `currency`
- `merchant_name_raw`
- `merchant_normalized`
- `category`             // One of user-defined categories
- `source`               // "sms" | "pdf" | "email"
- `description`
- `source_doc_id`        // Link to Document (if any)
- `tags` (JSON array)    // e.g. ["subscription", "food", "utility"]

### 3.3 Budget

- `id`
- `user_id`
- `month`                // e.g. "2025-12"
- `total_limit`
- `per_category_limits` (JSON {category: limit})

### 3.4 Document

- `id`
- `user_id`
- `type`                 // "bank_statement" | "salary_slip" | "bill"
- `storage_path`
- `extracted_text`
- `parsed_metadata` (JSON)
- `embedding_id` (Qdrant)

---

## 4. Automatic Ingestion Design

### 4.1 SMS Ingestion

**Goal**: Get spending data from SMS from specific bank senders.

- **User config**:
  - In Settings, user provides:
    - List of **bank SMS senders** (e.g., “BOC”, “HNB BANK”, phone numbers).
- **Ingestion flow options** (depending on platform):
  - Mobile app/companion service that exports SMS as a .json/.csv and uploads to backend.
  - Or periodic manual export (MVP).

**Parsing**:
- Regex-based patterns per bank (if known), fallback to LLM extraction:
  - amount, currency, merchant, timestamp, balance, transaction type (debit/credit).
- Map parsed SMS to `Transaction` records.

### 4.2 PDF Ingestion (Salary Slips / Bank Statements / Bills)

**Manual ingestion (MVP)**:
- User uploads PDFs through Streamlit:
  - Salary slips
  - Bank statements
  - Utility/other bills

**Processing**:
- Use Mistral DocumentAI via httpx to extract text + structure.
- Use heuristics + LLM to:
  - Identify document type (statement vs salary slip vs bill).
  - Extract transactions (for bank statements/bills).
  - Extract income entries for salary slips.

**Result**:
- Convert to `Transaction` records linked to a `Document` entry.

### 4.3 Suggested Auto-ingestion from Email Inbox

**Goal**: Automatically ingest PDFs from email inbox based on **sender/title rules**.

Possible design:

- Use a separate **Email Connector Service** (outside LangGraph) with:
  - Gmail / IMAP integration (e.g., OAuth2 + Gmail API).
- User defines rules in Settings:
  - Allowed senders (e.g., `alerts@mybank.com`, `billing@utility.com`)
  - Subject filters (e.g., contains “Statement”, “Salary Slip”, “Invoice”).
- The Email Connector:
  - Periodically polls inbox using Gmail API/IMAP.
  - For matching mails:
    - Downloads attached PDFs.
    - Saves to object storage / local path.
    - Calls backend `/ingest/pdf` endpoint with file path, user_id.

LangGraph then handles the **same PDF ingestion flow** as manual uploads.

---

## 5. LangGraph Architecture

### 5.1 Shared State

```python
from typing import List, Dict, Any, Optional
from typing_extensions import TypedDict

class FinanceState(TypedDict, total=False):
    user_id: str
    user_profile: Dict[str, Any]

    # Raw input
    input_type: str          # "sms_batch", "pdf_upload", "analytics_query", "price_compare", "research"
    raw_input: Any           # text, query params, transaction id, etc.
    files: List[str]         # file paths, for pdf ingestion

    # Parsed artifacts
    parsed_transactions: List[Dict[str, Any]]
    parsed_documents: List[Dict[str, Any]]

    # Outputs
    analytics_result: Optional[Dict[str, Any]]
    price_compare_result: Optional[Dict[str, Any]]
    research_answer: Optional[str]
    research_sources: Optional[List[Dict[str, Any]]]

    # Control
    task_type: str
    messages: List[Dict[str, Any]]
````

### 5.2 Agents (Nodes)

1. **RouterAgent**

   * Classifies `task_type` from input:

     * `"sms_batch"`, `"pdf_upload"`, `"analytics"`, `"price_compare"`, `"research"`.
   * Sets which agent(s) to call next.

2. **SMSIngestionAgent**

   * Input: batch of SMS strings (from user export or mobile companion).
   * Logic:

     * Filter by allowed bank senders from `user_profile.settings`.
     * Apply regex per known bank; fallback to LLM extraction.
   * Output: `parsed_transactions`.

3. **PDFIngestionAgent**

   * Input: file paths.
   * Logic:

     * Call Mistral DocumentAI via httpx.
     * Store `Document` row.
     * Extract suitable text/sections for downstream parsing.
   * Output: `parsed_documents`.

4. **TransactionExtractionAgent**

   * From SMS / PDF text → structured `Transaction` objects.
   * For statements/bills: create multiple `Transaction` records.

5. **CategorizationAgent**

   * Auto-categorizes transactions using:

     * User-defined category list.
     * Rules (e.g., merchant contains “Uber Eats” → “Food Delivery”).
     * LLM classification prompt with allowed categories.

6. **PersistenceAgent**

   * Writes `Transaction` and `Document` objects to Postgres.
   * Creates embeddings for relevant text chunks in Qdrant if needed (e.g., for future search).
   * Ensures idempotency (e.g., hash-based uniqueness).

7. **BudgetAgent** (simple)

   * Reads user’s budget for current month.
   * Returns summary of spend vs budget by category & overall.

8. **AnalyticsAgent**

   * Input: `user_id`, optional filters (date range, category).
   * Computes:

     * Monthly spend totals.
     * Category distribution (pie/bar data).
     * Rolling averages (e.g., 30-day moving average).
   * Formats output as:

     * Aggregated JSON ready for charts (Streamlit).

9. **PriceComparerAgent**

   * Input: specific transaction or merchant/item description.
   * Steps:

     * Identify intent: item, subscription, merchant type.
     * Use Scrapeless MCP (SERP) & Playwright for web search:

       * “<item/merchant> price”, “<service> subscription price”, etc.
     * Parse HTML with BeautifulSoup as fallback.
   * Output:

     * List of competitor options, approximate prices, URLs.
     * Any coupon/promo pages found.

10. **ResearcherAgent**

    * General-purpose research.
    * Input: natural language query from user.
    * Steps:

      * Use Scrapeless MCP for web search.
      * For complex topics, possibly use Playwright for JS-heavy sites.
      * Summarize with LLM (litellm) including:

        * Bullet-point answer.
        * List of sources (title, URL, 1-line description).
    * Output:

      * `research_answer`, `research_sources`.

11. **ErrorHandlerAgent**

    * Catches exceptions from other agents.
    * Logs error & outputs user-friendly message.

---

## 6. Core Workflows

### 6.1 Ingest SMS Batch

1. User exports SMS (or mobile app pushes them) → backend `/ingest/sms`.
2. Start LangGraph run:

   * `task_type = "sms_batch"`
   * `input_type = "sms_batch"`
   * `raw_input = { "messages": [...], "senders": [...] }`
3. Flow:

   * `RouterAgent → SMSIngestionAgent → TransactionExtractionAgent → CategorizationAgent → PersistenceAgent`
4. Output:

   * Count of new transactions imported.
   * Optional analytics refresh via `AnalyticsAgent` for immediate UI update.

### 6.2 Ingest PDF(s)

1. User uploads PDF(s) → `/ingest/pdf`.
2. Start LangGraph run:

   * `task_type = "pdf_upload"`
   * `files = [paths...]`
3. Flow:

   * `RouterAgent → PDFIngestionAgent → TransactionExtractionAgent → CategorizationAgent → PersistenceAgent`
4. Output:

   * Number of new transactions + (if salary slip) new income entries (optional extension).

### 6.3 Monthly Budget Input & Summary

1. User sets monthly budget in UI:

   * Total limit + per-category limits.
2. Backend writes `Budget` row directly (no graph needed).
3. For summary:

   * Call `BudgetAgent` + `AnalyticsAgent` to produce:

     * Current spend vs budget by category.
     * Text summary (LLM optional).

### 6.4 On-demand Analytics

1. User requests: “Show my last 3 months spend by category.”
2. Frontend calls backend `/analytics` which starts a LangGraph run:

   * `task_type = "analytics"`
   * `raw_input = { "range": "last_3_months" }`
3. Flow:

   * `RouterAgent → AnalyticsAgent`
4. Output:

   * JSON with:

     * `monthly_spend`: list[month, total]
     * `category_distribution`: {category: amount}
     * `rolling_average`: data for line plot
5. Streamlit renders charts:

   * Line chart (monthly / rolling average)
   * Bar/pie chart (category distribution)

### 6.5 Price Comparison for a Spending

1. User selects a transaction in UI and clicks “Compare prices”.
2. Backend starts LangGraph run:

   * `task_type = "price_compare"`
   * `raw_input = { "transaction_id": "<id>" }`
3. Flow:

   * `RouterAgent → PriceComparerAgent`
4. Output:

   * `price_compare_result` with:

     * competitors, approximate prices, URLs, coupons.
5. UI shows a recommendations card with estimated savings.

### 6.6 Research Query

1. User asks: “What’s the difference between index funds and ETFs?”
2. Backend starts run:

   * `task_type = "research"`
   * `raw_input = { "query": "..." }`
3. Flow:

   * `RouterAgent → ResearcherAgent`
4. Output:

   * `research_answer`: well-structured explanation.
   * `research_sources`: list of sources (title + url).
5. UI:

   * Chat interface shows answer and a “Sources” section with clickable links.

---

## 7. UI Design (Streamlit)

### 7.1 Pages

* **Dashboard**

  * Total monthly spend vs budget.
  * Key charts from AnalyticsAgent.
* **Transactions**

  * Filterable table of transactions.
  * Per-row actions:

    * “Edit category”
    * “Compare prices”
* **Budgets**

  * Set / edit budgets.
  * Summary of progress.
* **Analytics**

  * Query panel:

    * Select date range, visualization type.
  * Plots generated on demand via `/analytics`.
* **Research**

  * Chat-like box bound to ResearcherAgent.
  * Shows answer + sources.
* **Settings**

  * Bank SMS sender list.
  * Email auto-ingest rules (senders + subject filters).

---

## 8. Configuration

Environment variables (examples):

* `LLM_MODEL` (default: `gemini-2.5-flash`)
* `DATABASE_URL` (PostgreSQL)
* `QDRANT_URL`, `QDRANT_API_KEY`
* `REDIS_URL` (optional)
* `DOC_AI_ENDPOINT` / DocumentAI credentials
* `SCRAPELESS_API_KEY` / SERP API key
* `PLAYWRIGHT_BROWSER` (chromium/firefox/webkit)
* `EMAIL_PROVIDER` (e.g., `gmail`)
* `EMAIL_CLIENT_ID`, `EMAIL_CLIENT_SECRET` (for OAuth where used)

---

## 9. Security Notes (Minimal)

* Mask card numbers / sensitive IDs before storing or sending to LLM.
* Store only the minimum needed from SMS and emails.
* User-scoped access control on every DB query.
* Never store email passwords; use OAuth tokens with least privileges needed.

```
```
