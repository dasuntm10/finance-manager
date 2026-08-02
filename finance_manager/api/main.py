from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from finance_manager.db import FinanceRepository, get_repository
from finance_manager.graph import FinanceGraphRunner
from finance_manager.logger import configure_logging, logger
from finance_manager.reports.render import render_report_html
from finance_manager.schemas import AnalyticsRequest, Budget, MonthlyReport
from uuid import uuid4


# ---------------------------
# Request models
# ---------------------------


class SMSIngestRequest(BaseModel):
    user_id: Optional[str] = None
    messages: List[Dict[str, str]]
    senders: Optional[List[str]] = None


class PriceCompareRequest(BaseModel):
    user_id: Optional[str] = None
    transaction_id: Optional[str] = None
    query: Optional[str] = None


class ResearchRequest(BaseModel):
    user_id: Optional[str] = None
    query: str


class EmailIngestRequest(BaseModel):
    user_id: Optional[str] = None
    since_days: Optional[int] = None
    limit: Optional[int] = None
    folder: Optional[str] = None
    # Supplying messages skips IMAP entirely, which is useful for replaying a
    # webhook payload or an exported mailbox.
    messages: Optional[List[Dict[str, Any]]] = None


class ReportRequest(BaseModel):
    user_id: Optional[str] = None
    month: Optional[str] = None
    include_narrative: bool = True


# ---------------------------
# Dependency helpers
# ---------------------------


@lru_cache(maxsize=1)
def _shared_runner() -> FinanceGraphRunner:
    # Building a runner compiles the graph and opens a Qdrant client, so it is
    # created once for the process instead of once per request.
    return FinanceGraphRunner()


def get_runner() -> FinanceGraphRunner:
    return _shared_runner()


# ---------------------------
# FastAPI setup
# ---------------------------

configure_logging()
app = FastAPI(title="Agentic Finance Manager", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/ingest/sms")
async def ingest_sms(payload: SMSIngestRequest, runner: FinanceGraphRunner = Depends(get_runner)):
    request_id = str(uuid4())
    state = await runner.arun(
        task_type="sms_batch",
        raw_input={"messages": payload.messages, "senders": payload.senders, "user_id": payload.user_id, "request_id": request_id},
        user_id=payload.user_id,
    )
    return {
        "request_id": request_id,
        "transactions": state.get("parsed_transactions"),
        "budget_status": state.get("budget_status"),
        "errors": state.get("errors", []),
    }


@app.post("/ingest/email")
async def ingest_email(payload: EmailIngestRequest, runner: FinanceGraphRunner = Depends(get_runner)):
    """Fetch bank emails over IMAP and ingest them as transactions.

    Non-transactional mail is skipped, and re-fetching the same window is
    idempotent because each transaction carries the email Message-ID.
    """
    request_id = str(uuid4())
    state = await runner.arun(
        task_type="email_batch",
        raw_input={
            "since_days": payload.since_days,
            "limit": payload.limit,
            "folder": payload.folder,
            "messages": payload.messages,
            "user_id": payload.user_id,
            "request_id": request_id,
        },
        user_id=payload.user_id,
    )
    transactions = state.get("parsed_transactions") or []
    return {
        "request_id": request_id,
        "emails_fetched": len(state.get("email_messages") or []),
        "transactions_parsed": len(transactions),
        "transactions": transactions,
        "budget_status": state.get("budget_status"),
        "errors": state.get("errors", []),
    }


@app.post("/ingest/pdf")
async def ingest_pdf(files: List[UploadFile] = File(...), runner: FinanceGraphRunner = Depends(get_runner)):
    request_id = str(uuid4())
    temp_paths: List[str] = []
    for f in files:
        suffix = Path(f.filename).suffix or ".pdf"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(await f.read())
        tmp.flush()
        temp_paths.append(tmp.name)
    try:
        state = await runner.arun(task_type="pdf_upload", files=temp_paths, raw_input={"request_id": request_id})
        return {
            "request_id": request_id,
            "documents": state.get("parsed_documents"),
            "transactions": state.get("parsed_transactions"),
            "budget_status": state.get("budget_status"),
            "errors": state.get("errors", []),
        }
    finally:
        for path in temp_paths:
            try:
                Path(path).unlink()
            except Exception:
                logger.info("temp_cleanup_failed", path=path)


@app.post("/analytics")
async def analytics(payload: AnalyticsRequest, runner: FinanceGraphRunner = Depends(get_runner)):
    request_id = str(uuid4())
    state = await runner.arun(task_type="analytics", raw_input={**payload.model_dump(), "request_id": request_id})
    return {"request_id": request_id, "analytics": state.get("analytics_result"), "errors": state.get("errors", [])}


@app.post("/budget")
async def set_budget(budget: Budget, repo: FinanceRepository = Depends(get_repository)):
    saved = await repo.set_budget(budget)
    return saved


@app.get("/budget")
async def get_budget(user_id: str, month: str, repo: FinanceRepository = Depends(get_repository)):
    budget = await repo.get_budget(user_id=user_id, month=month)
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    return budget


@app.get("/budget/status")
async def budget_status(
    user_id: str,
    month: Optional[str] = None,
    runner: FinanceGraphRunner = Depends(get_runner),
):
    """Spend against limits for a month, with alerts and end-of-month projection."""
    request_id = str(uuid4())
    state = await runner.arun(
        task_type="budget",
        raw_input={"month": month, "request_id": request_id},
        user_id=user_id,
    )
    return {
        "request_id": request_id,
        "budget_status": state.get("budget_status"),
        "errors": state.get("errors", []),
    }


@app.get("/recurring")
async def recurring(
    user_id: str,
    min_occurrences: Optional[int] = None,
    upcoming_days: Optional[int] = None,
    runner: FinanceGraphRunner = Depends(get_runner),
):
    """Detected recurring charges and subscriptions for a user."""
    request_id = str(uuid4())
    state = await runner.arun(
        task_type="recurring",
        raw_input={
            "min_occurrences": min_occurrences,
            "upcoming_days": upcoming_days,
            "request_id": request_id,
        },
        user_id=user_id,
    )
    return {
        "request_id": request_id,
        "summary": state.get("recurring_summary"),
        "series": state.get("recurring_series"),
        "errors": state.get("errors", []),
    }


@app.post("/reports/monthly")
async def monthly_report(payload: ReportRequest, runner: FinanceGraphRunner = Depends(get_runner)):
    """Build the monthly report as JSON."""
    request_id = str(uuid4())
    state = await runner.arun(
        task_type="report",
        raw_input={
            "month": payload.month,
            "include_narrative": payload.include_narrative,
            "request_id": request_id,
        },
        user_id=payload.user_id,
    )
    return {
        "request_id": request_id,
        "report": state.get("report_result"),
        "errors": state.get("errors", []),
    }


@app.get("/reports/monthly.html", response_class=HTMLResponse)
async def monthly_report_html(
    user_id: str,
    month: Optional[str] = None,
    include_narrative: bool = True,
    download: bool = False,
    runner: FinanceGraphRunner = Depends(get_runner),
):
    """Build the monthly report and return it as a standalone HTML document."""
    request_id = str(uuid4())
    state = await runner.arun(
        task_type="report",
        raw_input={
            "month": month,
            "include_narrative": include_narrative,
            "request_id": request_id,
        },
        user_id=user_id,
    )
    payload = state.get("report_result")
    if not payload:
        raise HTTPException(
            status_code=500,
            detail=state.get("errors") or "Report generation failed",
        )
    report = MonthlyReport(**payload)
    headers = {}
    if download:
        filename = f"finance-report-{report.user_id}-{report.month}.html"
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return HTMLResponse(content=render_report_html(report), headers=headers)


@app.post("/price-compare")
async def price_compare(payload: PriceCompareRequest, runner: FinanceGraphRunner = Depends(get_runner)):
    request_id = str(uuid4())
    state = await runner.arun(task_type="price_compare", raw_input={**payload.model_dump(), "request_id": request_id})
    return {"request_id": request_id, "price_compare": state.get("price_compare_result"), "errors": state.get("errors", [])}


@app.post("/research")
async def research(payload: ResearchRequest, runner: FinanceGraphRunner = Depends(get_runner)):
    request_id = str(uuid4())
    state = await runner.arun(task_type="research", raw_input={**payload.model_dump(), "request_id": request_id})
    return {
        "request_id": request_id,
        "answer": state.get("research_answer"),
        "sources": state.get("research_sources"),
        "errors": state.get("errors", []),
    }


def run() -> None:
    import uvicorn

    uvicorn.run("finance_manager.api.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    run()


