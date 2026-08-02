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


# ---------------------------
# Email ingestion
# ---------------------------


class EmailMessage(BaseModel):
    """A fetched bank email, normalized to naive UTC."""

    message_id: str
    sender: str
    subject: str = ""
    received_at: datetime
    body: str = ""


# ---------------------------
# Recurring charges and subscriptions
# ---------------------------


class RecurringSeries(BaseModel):
    """A merchant that charges on a detectable schedule."""

    merchant_key: str
    merchant_label: str
    category: Optional[str] = None
    currency: str
    cadence: Literal["weekly", "biweekly", "monthly", "quarterly", "yearly"]
    interval_days: float
    expected_interval_days: float
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    typical_amount: float
    last_amount: float
    # Cost normalized to a month so cadences can be summed together.
    monthly_cost: float
    # Latest charge versus the median of previous charges, in percent.
    amount_change_pct: Optional[float] = None
    amount_stability: Literal["fixed", "variable"] = "fixed"
    status: Literal["active", "lapsed"] = "active"
    next_expected: Optional[datetime] = None
    days_until_next: Optional[int] = None
    confidence: float = 0.0
    transaction_ids: List[str] = Field(default_factory=list)


class RecurringSummary(BaseModel):
    """Headline numbers across all detected recurring series."""

    total_series: int = 0
    active_count: int = 0
    lapsed_count: int = 0
    monthly_total: float = 0.0
    annual_total: float = 0.0
    # Every detected series, ordered by monthly cost.
    series: List[RecurringSeries] = Field(default_factory=list)
    # Active series expected to charge within the lookahead window.
    upcoming: List[RecurringSeries] = Field(default_factory=list)
    price_increases: List[RecurringSeries] = Field(default_factory=list)


# ---------------------------
# Budget status and alerts
# ---------------------------


class BudgetAlert(BaseModel):
    scope: Literal["total", "category"]
    category: Optional[str] = None
    severity: Literal["critical", "warning", "info"]
    code: Literal[
        "limit_exceeded",
        "approaching_limit",
        "projected_overspend",
        "no_limit_set",
        "no_budget",
    ]
    message: str
    spent: float = 0.0
    limit: Optional[float] = None
    utilization: Optional[float] = None
    projected: Optional[float] = None


class BudgetCategoryStatus(BaseModel):
    category: str
    spent: float
    limit: Optional[float] = None
    remaining: Optional[float] = None
    utilization: Optional[float] = None
    projected_spend: Optional[float] = None
    status: Literal["ok", "warning", "over", "unbudgeted"] = "ok"


class BudgetStatus(BaseModel):
    user_id: str
    month: str
    has_budget: bool = False
    total_spent: float = 0.0
    total_limit: Optional[float] = None
    total_remaining: Optional[float] = None
    total_utilization: Optional[float] = None
    projected_total: Optional[float] = None
    days_elapsed: int = 0
    days_in_month: int = 0
    transaction_count: int = 0
    categories: List[BudgetCategoryStatus] = Field(default_factory=list)
    alerts: List[BudgetAlert] = Field(default_factory=list)


# ---------------------------
# Monthly reports
# ---------------------------


class ReportCategoryLine(BaseModel):
    category: str
    amount: float
    share: float
    previous_amount: float = 0.0
    change_pct: Optional[float] = None


class ReportMerchantLine(BaseModel):
    merchant: str
    amount: float
    transactions: int


class MonthlyReport(BaseModel):
    user_id: str
    month: str
    generated_at: datetime
    currency: str = "USD"
    total_spent: float = 0.0
    transaction_count: int = 0
    previous_total: float = 0.0
    change_pct: Optional[float] = None
    daily_average: float = 0.0
    largest_transaction: Optional[Dict[str, Any]] = None
    categories: List[ReportCategoryLine] = Field(default_factory=list)
    top_merchants: List[ReportMerchantLine] = Field(default_factory=list)
    budget: Optional[BudgetStatus] = None
    recurring: RecurringSummary = Field(default_factory=RecurringSummary)
    # Deterministic bullet points, always present.
    highlights: List[str] = Field(default_factory=list)
    # LLM-written prose grounded in the numbers above; None when unavailable.
    narrative: Optional[str] = None


class FinanceState(TypedDict, total=False):
    user_id: str
    request_id: str
    user_profile: Dict[str, Any]

    # sms_batch | email_batch | pdf_upload | analytics | price_compare
    # | research | recurring | report
    input_type: str
    raw_input: Any
    files: List[str]

    parsed_transactions: List[Dict[str, Any]]
    parsed_documents: List[Dict[str, Any]]
    email_messages: List[Dict[str, Any]]

    analytics_result: Optional[Dict[str, Any]]
    budget_status: Optional[Dict[str, Any]]
    recurring_series: Optional[List[Dict[str, Any]]]
    recurring_summary: Optional[Dict[str, Any]]
    report_result: Optional[Dict[str, Any]]
    price_compare_result: Optional[Dict[str, Any]]
    research_answer: Optional[str]
    research_sources: Optional[List[Dict[str, Any]]]

    task_type: str
    messages: List[Dict[str, Any]]
    errors: List[str]


