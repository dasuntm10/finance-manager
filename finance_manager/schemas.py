from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, TypedDict

from pydantic import BaseModel, Field


class Transaction(BaseModel):
    id: Optional[str] = None
    user_id: str
    timestamp: datetime
    amount: float
    currency: str
    merchant_name_raw: Optional[str] = None
    merchant_normalized: Optional[str] = None
    category: Optional[str] = None
    source: Literal["sms", "pdf", "email"]
    description: Optional[str] = None
    source_doc_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class Budget(BaseModel):
    id: Optional[str] = None
    user_id: str
    month: str  # e.g., "2025-12"
    total_limit: float
    per_category_limits: Dict[str, float] = Field(default_factory=dict)


class Document(BaseModel):
    id: Optional[str] = None
    user_id: str
    type: Literal["bank_statement", "salary_slip", "bill"]
    storage_path: str
    extracted_text: Optional[str] = None
    parsed_metadata: Dict[str, Any] = Field(default_factory=dict)
    embedding_id: Optional[str] = None


class AnalyticsRequest(BaseModel):
    user_id: str
    range: Optional[str] = None
    category: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None


class AnalyticsResponse(BaseModel):
    monthly_spend: List[Dict[str, Any]] = Field(default_factory=list)
    category_distribution: Dict[str, float] = Field(default_factory=dict)
    rolling_average: List[Dict[str, Any]] = Field(default_factory=list)


class PriceCompareResult(BaseModel):
    competitors: List[Dict[str, Any]] = Field(default_factory=list)
    coupons: List[Dict[str, Any]] = Field(default_factory=list)


class ResearchResult(BaseModel):
    answer: Optional[str] = None
    sources: List[Dict[str, Any]] = Field(default_factory=list)


class FinanceState(TypedDict, total=False):
    user_id: str
    request_id: str
    user_profile: Dict[str, Any]

    input_type: str  # sms_batch | pdf_upload | analytics_query | price_compare | research
    raw_input: Any
    files: List[str]

    parsed_transactions: List[Dict[str, Any]]
    parsed_documents: List[Dict[str, Any]]

    analytics_result: Optional[Dict[str, Any]]
    price_compare_result: Optional[Dict[str, Any]]
    research_answer: Optional[str]
    research_sources: Optional[List[Dict[str, Any]]]

    task_type: str
    messages: List[Dict[str, Any]]
    errors: List[str]


