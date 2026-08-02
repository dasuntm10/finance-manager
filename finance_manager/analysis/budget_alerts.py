"""Budget evaluation and alerting.

Turns a month's transactions plus a stored Budget into a BudgetStatus: what was
spent per category, how that compares to each limit, where the month is heading
at the current pace, and which alerts a user should actually see.

Pace projection matters because a limit breach is only actionable while there is
still month left. Spending 70 percent of a limit is fine on day 28 and a problem
on day 8, so the projection compares elapsed days against the month length.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from finance_manager.schemas import (
    Budget,
    BudgetAlert,
    BudgetCategoryStatus,
    BudgetStatus,
    Transaction,
)


UNCATEGORIZED = "Uncategorized"

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
# A category with no limit is only worth flagging once it is a real slice of the
# month's spend.
_UNBUDGETED_SHARE_THRESHOLD = 0.10
# Early in a month the pace estimate is dominated by a single day: on day 1 it
# multiplies that day by ~31. The projection is still reported, but it does not
# raise an alert until there are enough days to extrapolate from.
_MIN_DAYS_FOR_PROJECTION_ALERT = 5


def month_bounds(month: str) -> Tuple[datetime, datetime]:
    """Return the inclusive start and exclusive end datetimes of a YYYY-MM month."""
    try:
        start = datetime.strptime(month, "%Y-%m")
    except (TypeError, ValueError) as err:
        raise ValueError(f"Invalid month '{month}', expected YYYY-MM") from err
    days = calendar.monthrange(start.year, start.month)[1]
    return start, start + timedelta(days=days)


def month_key(value: datetime) -> str:
    return value.strftime("%Y-%m")


def _days_elapsed(month: str, now: datetime) -> Tuple[int, int]:
    """Return (days elapsed in the month so far, total days in the month)."""
    start, end = month_bounds(month)
    total_days = (end - start).days
    if now >= end:
        return total_days, total_days
    if now < start:
        return 0, total_days
    return max(1, now.day), total_days


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def evaluate_budget(
    user_id: str,
    month: str,
    transactions: Sequence[Transaction],
    budget: Optional[Budget],
    now: Optional[datetime] = None,
    warn_threshold: float = 0.8,
) -> BudgetStatus:
    """Compare a month's spend against its budget and produce alerts.

    Transactions outside the requested month are ignored, so callers may pass an
    unfiltered list. Credits carry negative amounts and therefore reduce spend.
    """
    reference = now or datetime.utcnow()
    start, end = month_bounds(month)
    in_month = [tx for tx in transactions if start <= tx.timestamp < end]

    spend_by_category: Dict[str, float] = defaultdict(float)
    for tx in in_month:
        spend_by_category[tx.category or UNCATEGORIZED] += tx.amount
    total_spent = sum(spend_by_category.values())

    days_elapsed, days_in_month = _days_elapsed(month, reference)
    is_current_month = start <= reference < end
    can_alert_on_pace = is_current_month and days_elapsed >= _MIN_DAYS_FOR_PROJECTION_ALERT
    # Only project while the month is still running; a closed month is final.
    pace_factor = (
        days_in_month / days_elapsed if (is_current_month and days_elapsed > 0) else 1.0
    )

    limits = dict(budget.per_category_limits) if budget else {}
    total_limit = budget.total_limit if budget else None

    categories: List[BudgetCategoryStatus] = []
    alerts: List[BudgetAlert] = []

    for category in sorted(set(spend_by_category) | set(limits)):
        spent = spend_by_category.get(category, 0.0)
        limit = limits.get(category)
        projected = spent * pace_factor if is_current_month else spent
        utilization = (spent / limit) if limit else None

        if limit is None:
            status = "unbudgeted"
        elif utilization is not None and utilization >= 1.0:
            status = "over"
        elif utilization is not None and utilization >= warn_threshold:
            status = "warning"
        else:
            status = "ok"

        categories.append(
            BudgetCategoryStatus(
                category=category,
                spent=_round(spent) or 0.0,
                limit=_round(limit),
                remaining=_round(limit - spent) if limit is not None else None,
                utilization=_round(utilization),
                projected_spend=_round(projected),
                status=status,
            )
        )

        if limit is None:
            # Only worth saying when a budget exists but misses this category.
            # With no budget at all, the single "no_budget" alert covers it.
            if (
                budget is not None
                and total_spent > 0
                and (spent / total_spent) >= _UNBUDGETED_SHARE_THRESHOLD
            ):
                alerts.append(
                    BudgetAlert(
                        scope="category",
                        category=category,
                        severity="info",
                        code="no_limit_set",
                        message=(
                            f"{category} accounts for {spent / total_spent * 100:.0f}% "
                            f"of this month's spend but has no limit set."
                        ),
                        spent=_round(spent) or 0.0,
                    )
                )
            continue

        if utilization is not None and utilization >= 1.0:
            alerts.append(
                BudgetAlert(
                    scope="category",
                    category=category,
                    severity="critical",
                    code="limit_exceeded",
                    message=(
                        f"{category} is over budget: {spent:.2f} spent of a "
                        f"{limit:.2f} limit ({utilization * 100:.0f}%)."
                    ),
                    spent=_round(spent) or 0.0,
                    limit=_round(limit),
                    utilization=_round(utilization),
                    projected=_round(projected),
                )
            )
        elif utilization is not None and utilization >= warn_threshold:
            alerts.append(
                BudgetAlert(
                    scope="category",
                    category=category,
                    severity="warning",
                    code="approaching_limit",
                    message=(
                        f"{category} is at {utilization * 100:.0f}% of its "
                        f"{limit:.2f} limit with {days_in_month - days_elapsed} "
                        f"day(s) left."
                    ),
                    spent=_round(spent) or 0.0,
                    limit=_round(limit),
                    utilization=_round(utilization),
                    projected=_round(projected),
                )
            )
        elif can_alert_on_pace and projected > limit:
            alerts.append(
                BudgetAlert(
                    scope="category",
                    category=category,
                    severity="warning",
                    code="projected_overspend",
                    message=(
                        f"{category} is on pace for {projected:.2f} by month end, "
                        f"over its {limit:.2f} limit."
                    ),
                    spent=_round(spent) or 0.0,
                    limit=_round(limit),
                    utilization=_round(utilization),
                    projected=_round(projected),
                )
            )

    projected_total = total_spent * pace_factor if is_current_month else total_spent
    total_utilization = (total_spent / total_limit) if total_limit else None

    if budget is None:
        alerts.append(
            BudgetAlert(
                scope="total",
                severity="info",
                code="no_budget",
                message=(
                    f"No budget set for {month}. Spend so far is {total_spent:.2f}."
                ),
                spent=_round(total_spent) or 0.0,
            )
        )
    elif total_limit:
        if total_utilization is not None and total_utilization >= 1.0:
            alerts.append(
                BudgetAlert(
                    scope="total",
                    severity="critical",
                    code="limit_exceeded",
                    message=(
                        f"Total spend {total_spent:.2f} exceeds the {total_limit:.2f} "
                        f"limit for {month}."
                    ),
                    spent=_round(total_spent) or 0.0,
                    limit=_round(total_limit),
                    utilization=_round(total_utilization),
                    projected=_round(projected_total),
                )
            )
        elif total_utilization is not None and total_utilization >= warn_threshold:
            alerts.append(
                BudgetAlert(
                    scope="total",
                    severity="warning",
                    code="approaching_limit",
                    message=(
                        f"Total spend is at {total_utilization * 100:.0f}% of the "
                        f"{total_limit:.2f} monthly limit."
                    ),
                    spent=_round(total_spent) or 0.0,
                    limit=_round(total_limit),
                    utilization=_round(total_utilization),
                    projected=_round(projected_total),
                )
            )
        elif can_alert_on_pace and projected_total > total_limit:
            alerts.append(
                BudgetAlert(
                    scope="total",
                    severity="warning",
                    code="projected_overspend",
                    message=(
                        f"At the current pace you will spend {projected_total:.2f} "
                        f"this month, over the {total_limit:.2f} limit."
                    ),
                    spent=_round(total_spent) or 0.0,
                    limit=_round(total_limit),
                    utilization=_round(total_utilization),
                    projected=_round(projected_total),
                )
            )

    alerts.sort(key=lambda a: (_SEVERITY_ORDER.get(a.severity, 9), a.category or ""))

    return BudgetStatus(
        user_id=user_id,
        month=month,
        has_budget=budget is not None,
        total_spent=_round(total_spent) or 0.0,
        total_limit=_round(total_limit),
        total_remaining=_round(total_limit - total_spent) if total_limit else None,
        total_utilization=_round(total_utilization),
        projected_total=_round(projected_total),
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
        transaction_count=len(in_month),
        categories=categories,
        alerts=alerts,
    )
