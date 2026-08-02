"""LLM narrative for the monthly report.

The model is given a compact brief of figures that are already computed and is
told to use only those figures. That keeps the prose grounded: the report's
numbers come from the database, and the model only chooses how to say them.

When the LLM is unavailable the deterministic fallback keeps the report complete.
"""

from __future__ import annotations

from typing import List, Optional

from finance_manager.llm import complete as llm_complete
from finance_manager.logger import logger
from finance_manager.schemas import MonthlyReport


_SYSTEM_RULES = (
    "You are a personal finance assistant writing a short monthly summary.\n"
    "Rules:\n"
    "- Use ONLY the figures in the brief. Never invent numbers, merchants or trends.\n"
    "- Write 3 to 5 sentences of plain prose. No bullet points, no headings.\n"
    "- Lead with the overall spend and how it moved versus last month.\n"
    "- Call out the single most useful thing to act on, if the brief shows one.\n"
    "- Be direct and neutral. Do not moralize about the user's spending.\n"
)


def build_brief(report: MonthlyReport) -> str:
    """Render the report's key figures as a compact text brief for the model."""
    lines: List[str] = [
        f"Month: {report.month}",
        f"Currency: {report.currency}",
        f"Total spend: {report.total_spent:.2f}",
        f"Previous month total: {report.previous_total:.2f}",
        f"Change vs previous month: "
        + (f"{report.change_pct:+.1f}%" if report.change_pct is not None else "n/a"),
        f"Transactions: {report.transaction_count}",
        f"Daily average so far: {report.daily_average:.2f}",
    ]

    if report.categories:
        top = ", ".join(
            f"{line.category} {line.amount:.2f} ({line.share * 100:.0f}%)"
            for line in report.categories[:5]
        )
        lines.append(f"Top categories: {top}")

    if report.top_merchants:
        merchants = ", ".join(
            f"{line.merchant} {line.amount:.2f} x{line.transactions}"
            for line in report.top_merchants[:5]
        )
        lines.append(f"Top merchants: {merchants}")

    if report.budget and report.budget.has_budget:
        budget = report.budget
        lines.append(
            f"Budget: limit {budget.total_limit:.2f}, spent {budget.total_spent:.2f}"
            + (
                f", projected month end {budget.projected_total:.2f}"
                if budget.projected_total is not None
                else ""
            )
        )
        for alert in budget.alerts[:3]:
            lines.append(f"Budget alert ({alert.severity}): {alert.message}")

    if report.recurring.active_count:
        lines.append(
            f"Recurring charges: {report.recurring.active_count} active, "
            f"{report.recurring.monthly_total:.2f} per month"
        )
    for series in report.recurring.price_increases[:2]:
        lines.append(
            f"Price increase: {series.merchant_label} from "
            f"{series.typical_amount:.2f} to {series.last_amount:.2f} "
            f"({series.amount_change_pct:+.1f}%)"
        )

    if report.largest_transaction:
        largest = report.largest_transaction
        lines.append(
            f"Largest charge: {largest['amount']:.2f} at {largest['merchant']}"
        )
    return "\n".join(lines)


def fallback_narrative(report: MonthlyReport) -> str:
    """Deterministic summary used when the LLM is unavailable."""
    parts = [
        f"In {report.month} you spent {report.total_spent:.2f} {report.currency} "
        f"across {report.transaction_count} transactions."
    ]
    if report.change_pct is not None:
        direction = "up" if report.change_pct > 0 else "down"
        parts.append(
            f"That is {direction} {abs(report.change_pct):.0f}% versus the "
            f"previous month ({report.previous_total:.2f})."
        )
    if report.categories:
        top = report.categories[0]
        parts.append(
            f"{top.category} led at {top.amount:.2f} "
            f"({top.share * 100:.0f}% of the total)."
        )
    critical = [a for a in (report.budget.alerts if report.budget else []) if a.severity == "critical"]
    if critical:
        parts.append(critical[0].message)
    elif report.recurring.active_count:
        parts.append(
            f"Recurring charges account for about "
            f"{report.recurring.monthly_total:.2f} per month."
        )
    return " ".join(parts)


def generate_narrative(report: MonthlyReport) -> str:
    """Return LLM prose for the report, falling back to a deterministic summary."""
    prompt = f"{_SYSTEM_RULES}\nBrief:\n{build_brief(report)}\n\nSummary:"
    try:
        answer = llm_complete(prompt)
    except Exception as err:  # pragma: no cover - llm_complete already guards
        logger.warning("report_narrative_failed", month=report.month, error=str(err))
        answer = None
    if answer and answer.strip():
        return answer.strip()
    logger.info("report_narrative_fallback", month=report.month)
    return fallback_narrative(report)
