from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from uuid import uuid4

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from finance_manager.config import Settings, get_settings
from finance_manager.llm import complete as llm_complete
from finance_manager.db import FinanceRepository, get_repository
from finance_manager.logger import logger
from finance_manager.schemas import (
    AnalyticsResponse,
    Budget,
    FinanceState,
    PriceCompareResult,
    ResearchResult,
    Transaction,
)
from finance_manager.vector_store import VectorStore, get_vector_store


# ---------------------------
# Utility helpers
# ---------------------------

def _now() -> datetime:
    return datetime.utcnow()


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _ensure_user_id(state: FinanceState) -> str:
    if state.get("user_id"):
        return state["user_id"]
    state["user_id"] = "demo-user"
    return "demo-user"


def _ensure_request_id(state: FinanceState) -> str:
    rid = state.get("request_id")
    if rid:
        return rid
    rid = str(uuid4())
    state["request_id"] = rid
    return rid


def _log_step(step: str, event: str, state: FinanceState, **extra: Any) -> None:
    request_id = _ensure_request_id(state)
    logger.info("graph_step", request_id=request_id, step=step, event=event, **extra)


def _normalize_sender(sender: str) -> str:
    return sender.upper().strip()


def _simple_amount_parser(text: str) -> Optional[float]:
    match = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", text.replace(",", ""))
    return float(match.group(1)) if match else None


def _simple_currency_parser(text: str) -> Optional[str]:
    match = re.search(r"\b(USD|EUR|GBP|LKR|INR|AED)\b", text.upper())
    return match.group(1) if match else None


def _categorize(tx: Transaction, categories: List[str]) -> str:
    name = (tx.merchant_normalized or tx.merchant_name_raw or "").lower()
    rules = {
        "food": ["food", "cafe", "restaurant", "uber eats", "deliver"],
        "groceries": ["grocery", "market", "super", "mart"],
        "transport": ["uber", "bolt", "taxi", "cab", "bus", "train"],
        "utilities": ["utility", "power", "water", "electric", "bill"],
        "entertainment": ["movie", "cinema", "spotify", "netflix", "game"],
        "shopping": ["store", "shop", "fashion", "mall"],
        "healthcare": ["pharmacy", "clinic", "hospital", "doctor"],
        "rent": ["rent", "lease"],
    }
    for cat, keywords in rules.items():
        if any(k in name for k in keywords):
            return cat.title()
    return categories[0] if categories else "Other"


def _maybe_call_llm(prompt: str, settings: Optional[Settings] = None) -> Optional[str]:
    # Model and provider are resolved from config/llm.yaml (see finance_manager.llm).
    return llm_complete(prompt)


async def _search_via_http(settings: Settings, query: str, request_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    headers = {}
    if settings.scrapeless_api_key:
        headers["X-API-Key"] = settings.scrapeless_api_key
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.scrapeless.io/search", params={"q": query, "engine": "google", "num": limit}, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("results") or data.get("organic_results") or []
        return [
            {"title": hit.get("title"), "url": hit.get("link"), "snippet": hit.get("snippet")}
            for hit in hits[:limit]
        ]


async def _search_via_mcp(settings: Settings, query: str, request_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    mcp_url = settings.mcp_server_url
    if not mcp_url and settings.tavily_api_key:
        # use Tavily remote MCP server if api key is present
        mcp_url = f"https://mcp.tavily.com/mcp/?tavilyApiKey={settings.tavily_api_key}"
    if not mcp_url:
        raise RuntimeError("MCP server URL not configured")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            mcp_url,
            json={"query": query, "limit": limit},
            headers={"X-Request-ID": request_id},
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("results") or data.get("organic_results") or data.get("data") or []
        return [
            {"title": hit.get("title"), "url": hit.get("url") or hit.get("link"), "snippet": hit.get("snippet")}
            for hit in hits[:limit]
        ]


async def perform_search(settings: Settings, query: str, request_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    mode = (settings.search_mode or "http").lower()
    if mode == "mcp":
        try:
            return await _search_via_mcp(settings, query, request_id=request_id, limit=limit)
        except Exception as err:
            logger.info("mcp_search_fallback", request_id=request_id, error=str(err))
    # fallback to http
    try:
        return await _search_via_http(settings, query, request_id=request_id, limit=limit)
    except Exception as err:
        logger.warning("http_search_failed", request_id=request_id, error=str(err))
        return []


# ---------------------------
# Agent node factories
# ---------------------------

def make_router_node(settings: Settings) -> Callable[[FinanceState], Dict[str, Any]]:
    def router(state: FinanceState) -> Dict[str, Any]:
        _log_step("router", "start", state)
        task = state.get("task_type") or state.get("input_type")
        if task not in {"sms_batch", "pdf_upload", "analytics", "price_compare", "research"}:
            raise ValueError(f"Unsupported task_type: {task}")
        _log_step("router", "end", state, route=task)
        return {"task_type": task}

    return router


def make_sms_ingestion_node(settings: Settings) -> Callable[[FinanceState], FinanceState]:
    def sms_ingestion(state: FinanceState) -> FinanceState:
        _log_step("sms_ingestion", "start", state)
        user_id = _ensure_user_id(state)
        raw = state.get("raw_input") or {}
        messages: List[Dict[str, str]] = raw.get("messages") or []
        allowed_senders = {_normalize_sender(s) for s in (raw.get("senders") or settings.default_bank_senders)}
        parsed: List[Dict[str, Any]] = []
        for msg in messages:
            text = msg["text"] if isinstance(msg, dict) else str(msg)
            sender = _normalize_sender(msg.get("sender", "")) if isinstance(msg, dict) else ""
            if sender and allowed_senders and sender not in allowed_senders:
                continue
            amount = _simple_amount_parser(text) or 0.0
            currency = _simple_currency_parser(text) or settings.default_currency
            parsed.append(
                {
                    "user_id": user_id,
                    "timestamp": _now(),
                    "amount": amount,
                    "currency": currency,
                    "merchant_name_raw": text[:80],
                    "source": "sms",
                    "description": text,
                }
            )
        state["parsed_transactions"] = parsed
        _log_step("sms_ingestion", "end", state, parsed=len(parsed))
        return state

    return sms_ingestion


def make_pdf_ingestion_node(settings: Settings, vector_store: Optional[VectorStore]) -> Callable[[FinanceState], FinanceState]:
    def pdf_ingestion(state: FinanceState) -> FinanceState:
        _log_step("pdf_ingestion", "start", state, files=len(state.get("files") or []))
        user_id = _ensure_user_id(state)
        files = state.get("files") or []
        docs: List[Dict[str, Any]] = []
        for path in files:
            p = Path(path)
            if not p.exists():
                logger.warning("pdf_missing", request_id=_ensure_request_id(state), path=str(p))
                continue
            extracted_text: Optional[str] = None
            if settings.doc_ai_endpoint:
                try:
                    with open(p, "rb") as fh:
                        resp = httpx.post(
                            settings.doc_ai_endpoint,
                            headers={"Authorization": f"Bearer {settings.doc_ai_api_key}"} if settings.doc_ai_api_key else {},
                            files={"file": (p.name, fh, "application/pdf")},
                            timeout=30,
                        )
                    resp.raise_for_status()
                    extracted_text = resp.json().get("text") or resp.text
                except Exception as err:  # pragma: no cover - external service
                    logger.warning("doc_ai_failed", error=str(err))
            if extracted_text is None:
                try:
                    extracted_text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    extracted_text = ""
            doc = {
                "id": str(uuid4()),
                "user_id": user_id,
                "type": "bank_statement",
                "storage_path": str(p),
                "extracted_text": extracted_text,
                "parsed_metadata": {"filename": p.name},
            }
            docs.append(doc)
        state["parsed_documents"] = docs
        if vector_store and docs:
            # placeholder: in real app we would embed and upsert; here we store dummy vectors
            try:
                embeddings = []
                for doc in docs:
                    embeddings.append(
                        {
                            "id": doc["id"],
                            "vector": [0.0] * 768,
                            "payload": {"user_id": user_id, "text": doc.get("extracted_text", "")[:512]},
                        }
                    )
                vector_store.upsert_embeddings(embeddings)
            except Exception as err:  # pragma: no cover - Qdrant optional
                logger.warning("vector_upsert_failed", request_id=_ensure_request_id(state), error=str(err))
        _log_step("pdf_ingestion", "end", state, documents=len(docs))
        return state

    return pdf_ingestion


def make_extraction_node(settings: Settings) -> Callable[[FinanceState], FinanceState]:
    def extract(state: FinanceState) -> FinanceState:
        _log_step("extraction", "start", state)
        parsed: List[Dict[str, Any]] = state.get("parsed_transactions") or []
        docs: List[Dict[str, Any]] = state.get("parsed_documents") or []
        user_id = _ensure_user_id(state)
        txs: List[Transaction] = []

        # Use parsed transactions directly (SMS path)
        for tx_dict in parsed:
            txs.append(Transaction(**tx_dict))

        # Basic PDF parsing: look for lines with amount and merchant-ish words
        for doc in docs:
            text = doc.get("extracted_text") or ""
            for line in text.splitlines():
                amt = _simple_amount_parser(line)
                if amt is None:
                    continue
                currency = _simple_currency_parser(line) or settings.default_currency
                txs.append(
                    Transaction(
                        user_id=user_id,
                        timestamp=_now(),
                        amount=amt,
                        currency=currency,
                        merchant_name_raw=line[:80],
                        merchant_normalized=line.strip()[:80],
                        category=None,
                        source="pdf",
                        description=line.strip()[:200],
                        source_doc_id=doc.get("id"),
                    )
                )
        state["parsed_transactions"] = [tx.model_dump() for tx in txs]
        _log_step("extraction", "end", state, transactions=len(txs))
        return state

    return extract


def make_categorization_node(settings: Settings) -> Callable[[FinanceState], FinanceState]:
    def categorize(state: FinanceState) -> FinanceState:
        _log_step("categorization", "start", state)
        txs = [Transaction(**tx) if isinstance(tx, dict) else tx for tx in state.get("parsed_transactions", [])]
        results: List[Transaction] = []
        for tx in txs:
            cat = tx.category
            if not cat:
                cat = _categorize(tx, settings.default_categories)
            tx = tx.copy(update={"category": cat})
            results.append(tx)
        state["parsed_transactions"] = [tx.model_dump() for tx in results]
        _log_step("categorization", "end", state, transactions=len(results))
        return state

    return categorize


def make_persistence_node(repo: FinanceRepository) -> Callable[[FinanceState], FinanceState]:
    async def persist(state: FinanceState) -> FinanceState:
        _log_step("persistence", "start", state)
        txs = [Transaction(**tx) if isinstance(tx, dict) else tx for tx in state.get("parsed_transactions", [])]
        saved = await repo.upsert_transactions(txs)
        state["parsed_transactions"] = [tx.model_dump() for tx in saved]
        _log_step("persistence", "end", state, saved=len(saved))
        return state

    return persist


def make_budget_node(repo: FinanceRepository) -> Callable[[FinanceState], FinanceState]:
    async def budget(state: FinanceState) -> FinanceState:
        # Best-effort budget summary, piggybacked on analytics
        _log_step("budget", "start", state)
        user_id = _ensure_user_id(state)
        month = _month_key(_now())
        budget = await repo.get_budget(user_id=user_id, month=month)
        totals = defaultdict(float)
        txs = await repo.list_transactions(user_id=user_id)
        for tx in txs:
            totals[tx.category or "Uncategorized"] += tx.amount
        summary = {"month": month, "budget": budget.model_dump() if budget else None, "spend_by_category": dict(totals)}
        state["analytics_result"] = summary
        _log_step("budget", "end", state, categories=len(totals))
        return state

    return budget


def make_analytics_node(repo: FinanceRepository) -> Callable[[FinanceState], FinanceState]:
    async def analytics(state: FinanceState) -> FinanceState:
        _log_step("analytics", "start", state)
        user_id = _ensure_user_id(state)
        raw = state.get("raw_input") or {}
        start = raw.get("start_date")
        end = raw.get("end_date")
        txs = await repo.list_transactions(user_id=user_id, start=start, end=end)
        monthly_totals: Dict[str, float] = defaultdict(float)
        category_totals: Dict[str, float] = defaultdict(float)
        # rolling average over last 30 days
        window = timedelta(days=30)
        for tx in txs:
            key = _month_key(tx.timestamp)
            monthly_totals[key] += tx.amount
            category_totals[tx.category or "Uncategorized"] += tx.amount
        rolling: List[Dict[str, Any]] = []
        txs_sorted = sorted(txs, key=lambda t: t.timestamp)
        for idx, tx in enumerate(txs_sorted):
            window_start = tx.timestamp - window
            window_items = [t.amount for t in txs_sorted if window_start <= t.timestamp <= tx.timestamp]
            rolling.append({"timestamp": tx.timestamp.isoformat(), "average": sum(window_items) / len(window_items)})
        state["analytics_result"] = AnalyticsResponse(
            monthly_spend=[{"month": k, "total": v} for k, v in sorted(monthly_totals.items())],
            category_distribution=dict(category_totals),
            rolling_average=rolling,
        ).model_dump()
        _log_step("analytics", "end", state, transactions=len(txs))
        return state

    return analytics


def make_price_compare_node(settings: Settings) -> Callable[[FinanceState], FinanceState]:
    async def price_compare(state: FinanceState) -> FinanceState:
        _log_step("price_compare", "start", state)
        request_id = _ensure_request_id(state)
        query = ""
        raw = state.get("raw_input") or {}
        if isinstance(raw, dict):
            query = raw.get("query") or raw.get("transaction_id") or ""
        if not query:
            query = "best price for groceries"
        result = PriceCompareResult()
        try:
            hits = await perform_search(settings, query=query, request_id=request_id, limit=5)
            result.competitors = hits
        except Exception as err:  # pragma: no cover - external
            logger.info("price_compare_fallback", request_id=_ensure_request_id(state), error=str(err))
            result.competitors = [{"title": "Example Store", "url": "https://example.com", "snippet": "Sample price"}]
        state["price_compare_result"] = result.model_dump()
        _log_step("price_compare", "end", state, competitors=len(result.competitors))
        return state

    return price_compare


def make_research_node(settings: Settings) -> Callable[[FinanceState], FinanceState]:
    async def research(state: FinanceState) -> FinanceState:
        _log_step("research", "start", state)
        raw = state.get("raw_input") or {}
        query = raw.get("query") if isinstance(raw, dict) else str(raw)
        request_id = _ensure_request_id(state)
        result = ResearchResult()
        try:
            result.sources = await perform_search(settings, query=query, request_id=request_id, limit=5)
        except Exception as err:  # pragma: no cover
            logger.info("research_fallback", request_id=_ensure_request_id(state), error=str(err))
            result.sources = [{"title": "Example Source", "url": "https://example.com", "snippet": "Placeholder"}]

        llm_answer = _maybe_call_llm(f"Provide a short answer for: {query}", settings)
        result.answer = llm_answer or f"Top sources for '{query}' are listed."
        state["research_answer"] = result.answer
        state["research_sources"] = result.sources
        _log_step("research", "end", state, sources=len(result.sources))
        return state

    return research


def make_error_handler() -> Callable[[FinanceState, Exception], FinanceState]:
    def handle(state: FinanceState, err: Exception) -> FinanceState:
        logger.error("graph_error", request_id=_ensure_request_id(state), error=str(err))
        errors = state.get("errors") or []
        errors.append(str(err))
        state["errors"] = errors
        return state

    return handle


# ---------------------------
# Graph builder and runtime
# ---------------------------

class FinanceGraphRunner:
    """Convenience wrapper to build and invoke the LangGraph pipeline."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        repo: Optional[FinanceRepository] = None,
        vector_store: Optional[VectorStore] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = repo or get_repository()
        self.vector_store = vector_store or get_vector_store(self.settings)
        self.graph = self._build_graph()

    def _build_graph(self):
        g = StateGraph(FinanceState)

        g.add_node("router", make_router_node(self.settings))
        g.add_node("sms_ingestion", make_sms_ingestion_node(self.settings))
        g.add_node("pdf_ingestion", make_pdf_ingestion_node(self.settings, self.vector_store))
        g.add_node("extraction", make_extraction_node(self.settings))
        g.add_node("categorization", make_categorization_node(self.settings))
        g.add_node("persistence", make_persistence_node(self.repo))
        g.add_node("budget", make_budget_node(self.repo))
        g.add_node("analytics", make_analytics_node(self.repo))
        g.add_node("price_compare", make_price_compare_node(self.settings))
        g.add_node("research", make_research_node(self.settings))

        g.add_edge(START, "router")
        g.add_conditional_edges(
            "router",
            lambda state: state["task_type"],
            {
                "sms_batch": "sms_ingestion",
                "pdf_upload": "pdf_ingestion",
                "analytics": "analytics",
                "price_compare": "price_compare",
                "research": "research",
            },
        )

        # SMS / PDF ingestion chain
        g.add_edge("sms_ingestion", "extraction")
        g.add_edge("pdf_ingestion", "extraction")
        g.add_edge("extraction", "categorization")
        g.add_edge("categorization", "persistence")
        g.add_edge("persistence", "budget")
        g.add_edge("budget", END)

        # Direct edges
        g.add_edge("analytics", END)
        g.add_edge("price_compare", END)
        g.add_edge("research", END)

        g.set_error_handler(make_error_handler())
        return g.compile()

    async def arun(
        self,
        task_type: str,
        raw_input: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        files: Optional[List[str]] = None,
    ) -> FinanceState:
        request_id = None
        if raw_input and isinstance(raw_input, dict):
            request_id = raw_input.get("request_id")
        request_id = request_id or str(uuid4())
        state: FinanceState = {
            "task_type": task_type,
            "raw_input": raw_input or {},
            "files": files or [],
            "user_id": user_id or raw_input.get("user_id") if raw_input else None,
            "request_id": request_id,
            "messages": [],
            "parsed_transactions": [],
            "parsed_documents": [],
        }
        return await self.graph.ainvoke(state)

    def run(
        self,
        task_type: str,
        raw_input: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        files: Optional[List[str]] = None,
    ) -> FinanceState:
        return asyncio.run(self.arun(task_type=task_type, raw_input=raw_input, user_id=user_id, files=files))


def get_graph_runner() -> FinanceGraphRunner:
    return FinanceGraphRunner()



