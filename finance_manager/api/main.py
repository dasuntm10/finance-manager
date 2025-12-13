from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from finance_manager.config import Settings, get_settings
from finance_manager.db import FinanceRepository, get_repository
from finance_manager.graph import FinanceGraphRunner, get_graph_runner
from finance_manager.logger import configure_logging, logger
from finance_manager.schemas import AnalyticsRequest, Budget
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


# ---------------------------
# Dependency helpers
# ---------------------------


def get_runner(settings: Settings = Depends(get_settings), repo: FinanceRepository = Depends(get_repository)) -> FinanceGraphRunner:
    return FinanceGraphRunner(settings=settings, repo=repo)


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
    return {"request_id": request_id, "transactions": state.get("parsed_transactions"), "errors": state.get("errors", [])}


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
        return {"request_id": request_id, "documents": state.get("parsed_documents"), "transactions": state.get("parsed_transactions")}
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


