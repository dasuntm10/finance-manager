"""Assemble the monthly report data set.

Everything in the report is computed deterministically from stored transactions
before any language model is involved. The LLM only writes prose over numbers
that are already fixed, so a model outage degrades the wording of the report and
never its figures.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from finance_manager.analysis.budget_alerts import month_bounds, month_key
from finance_manager.analysis.budget_alerts import evaluate_budget
from finance_manager.analysis.recurring import detect_recurring, summarize_recurring
from finance_manager.config import Settings, get_settings
from finance_manager.db import FinanceRepository
from finance_manager.ingestion.parsing import normalize_merchant
from finance_manager.schemas import (
    BudgetStatus,
    MonthlyReport,
    ReportCategoryLine,
    ReportMerchantLine,
    Transaction,
)


TOP_MERCHANT_LIMIT = 8


def previous_month(month: str) -> str:
    """Return the YYYY-MM key of the month before the given one."""
    start, _ = month_bounds(month)
    return month_key(start - timedelta(days=1))


def _in_month(transactions: Sequence[Transaction], month: str) -> List[Transaction]:
    start, end = month_bounds(month)
    return [tx for tx in transactions if start <= tx.timestamp < end]


def _totals_by_category(transactions: Sequence[Transaction]) -> Dict[str, float]:
    totals: Dict[str, float] = defaultdict(float)
    for tx in transactions:
        totals[tx.category or "Uncategorized"] += tx.amount
    return dict(totals)


def _dominant_currency(
    transactions: Sequence[Transaction], fallback: str
) -> str:
    counts = Counter(tx.currency for tx in transactions if tx.currency)
    return counts.most_common(1)[0][0] if counts else fallback


def _change_pct(current: float, previous: float) -> Optional[float]:
    if not previous:
        return None
    return round((current - previous) / abs(previous) * 100.0, 1)


def _category_lines(
    current: Dict[str, float], previous: Dict[str, float], total: float
) -> List[ReportCategoryLine]:
    lines: List[ReportCategoryLine] = []
    for category, amount in current.items():
        prior = previous.get(category, 0.0)
        lines.append(
            ReportCategoryLine(
                category=category,
                amount=round(amount, 2),
                share=round(amount / total, 4) if total else 0.0,
                previous_amount=round(prior, 2),
                change_pct=_change_pct(amount, prior),
            )
        )
    lines.sort(key=lambda line: line.amount, reverse=True)
    return lines


def _merchant_lines(transactions: Sequence[Transaction]) -> List[ReportMerchantLine]:
    totals: Dict[str, float] = defaultdict(float)
    counts: Dict[str, int] = defaultdict(int)
    labels: Dict[str, str] = {}
    for tx in transactions:
        if tx.amount <= 0:
            continue
        key = (
            tx.merchant_normalized
            or normalize_merchant(tx.merchant_name_raw)
            or (tx.merchant_name_raw or "Unknown").lower()
        )
        totals[key] += tx.amount
        counts[key] += 1
        labels.setdefault(key, (tx.merchant_name_raw or key)[:60])
    lines = [
        ReportMerchantLine(
            merchant=labels.get(key, key), amount=round(amount, 2), transactions=counts[key]
        )
        for key, amount in totals.items()
    ]
    lines.sort(key=lambda line: line.amount, reverse=True)
    return lines[:TOP_MERCHANT_LIMIT]


def _largest_transaction(transactions: Sequence[Transaction]) -> Optional[Dict[str, object]]:
    debits = [tx for tx in transactions if tx.amount > 0]
    if not debits:
        return None
    largest = max(debits, key=lambda tx: tx.amount)
    return {
        "merchant": largest.merchant_name_raw or largest.description or "Unknown",
        "amount": round(largest.amount, 2),
        "currency": largest.currency,
        "category": largest.category,
        "timestamp": largest.timestamp.isoformat(),
    }


def build_highlights(report: MonthlyReport) -> List[str]:
    """Deterministic bullet points that never depend on an LLM being reachable."""
    highlights: List[str] = []

    if report.change_pct is not None:
        direction = "more" if report.change_pct > 0 else "less"
        highlights.append(
            f"You spent {abs(report.change_pct):.0f}% {direction} than in "
            f"{previous_month(report.month)} "
            f"({report.total_spent:.2f} vs {report.previous_total:.2f} {report.currency})."
        )
    else:
        highlights.append(
            f"Total spend for {report.month} was "
            f"{report.total_spent:.2f} {report.currency} across "
            f"{report.transaction_count} transactions."
        )

    if report.categories:
        top = report.categories[0]
        highlights.append(
            f"{top.category} was the largest category at {top.amount:.2f} "
            f"{report.currency} ({top.share * 100:.0f}% of spend)."
        )
        movers = [
            line
            for line in report.categories
            if line.change_pct is not None and abs(line.change_pct) >= 25
        ]
        if movers:
            mover = max(movers, key=lambda line: abs(line.change_pct or 0))
            verb = "up" if (mover.change_pct or 0) > 0 else "down"
            highlights.append(
                f"{mover.category} is {verb} {abs(mover.change_pct or 0):.0f}% "
                f"month over month ({mover.previous_amount:.2f} to {mover.amount:.2f})."
            )

    if report.budget and report.budget.alerts:
        for alert in report.budget.alerts:
            if alert.severity in ("critical", "warning"):
                highlights.append(alert.message)
                break

    if report.recurring.active_count:
        count = report.recurring.active_count
        noun = "recurring charge" if count == 1 else "recurring charges"
        verb = "costs" if count == 1 else "cost"
        highlights.append(
            f"{count} {noun} {verb} about "
            f"{report.recurring.monthly_total:.2f} {report.currency} per month "
            f"({report.recurring.annual_total:.2f} per year)."
        )
    for series in report.recurring.price_increases[:1]:
        highlights.append(
            f"{series.merchant_label} increased from about "
            f"{series.typical_amount:.2f} to {series.last_amount:.2f} "
            f"({series.amount_change_pct:+.0f}%)."
        )

    if report.largest_transaction:
        largest = report.largest_transaction
        highlights.append(
            f"Largest single charge: {largest['amount']:.2f} {largest['currency']} "
            f"at {largest['merchant']}."
        )
    return highlights


async def build_monthly_report(
    repo: FinanceRepository,
    user_id: str,
    month: Optional[str] = None,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
    include_narrative: bool = True,
) -> MonthlyReport:
    """Build the full monthly report for a user.

    Recurring detection runs over the user's whole history rather than just the
    reported month, because a cadence cannot be established from a single month
    of data.
    """
    settings = settings or get_settings()
    reference = now or datetime.utcnow()
    month = month or month_key(reference)

    history = await repo.list_transactions(user_id=user_id)
    current_txs = _in_month(history, month)
    prior_month = previous_month(month)
    prior_txs = _in_month(history, prior_month)

    current_totals = _totals_by_category(current_txs)
    prior_totals = _totals_by_category(prior_txs)
    total_spent = sum(current_totals.values())
    prior_total = sum(prior_totals.values())

    budget = await repo.get_budget(user_id=user_id, month=month)
    budget_status: BudgetStatus = evaluate_budget(
        user_id=user_id,
        month=month,
        transactions=current_txs,
        budget=budget,
        now=reference,
        warn_threshold=settings.budget_warn_threshold,
    )

    recurring = detect_recurring(
        history,
        min_occurrences=settings.recurring_min_occurrences,
        now=reference,
    )

    days_elapsed = max(budget_status.days_elapsed, 1)
    report = MonthlyReport(
        user_id=user_id,
        month=month,
        generated_at=reference,
        currency=_dominant_currency(current_txs, settings.default_currency),
        total_spent=round(total_spent, 2),
        transaction_count=len(current_txs),
        previous_total=round(prior_total, 2),
        change_pct=_change_pct(total_spent, prior_total),
        daily_average=round(total_spent / days_elapsed, 2),
        largest_transaction=_largest_transaction(current_txs),
        categories=_category_lines(current_totals, prior_totals, total_spent),
        top_merchants=_merchant_lines(current_txs),
        budget=budget_status,
        recurring=summarize_recurring(recurring),
    )
    report.highlights = build_highlights(report)

    if include_narrative:
        # Imported here so report data assembly stays importable without liteLLM.
        from finance_manager.reports.narrative import generate_narrative

        report.narrative = generate_narrative(report)
    return report
