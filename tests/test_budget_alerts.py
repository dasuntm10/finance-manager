"""Tests for budget evaluation, projection and alerting."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import pytest

from finance_manager.analysis.budget_alerts import evaluate_budget, month_bounds
from finance_manager.schemas import Budget, Transaction


def tx(amount: float, category: str, day: int, month: str = "2026-07") -> Transaction:
    year, mon = (int(part) for part in month.split("-"))
    return Transaction(
        user_id="u1",
        timestamp=datetime(year, mon, day, 10, 0),
        amount=amount,
        currency="USD",
        merchant_name_raw=f"{category} merchant",
        category=category,
        source="sms",
    )


def budget(total: float, **categories: float) -> Budget:
    return Budget(
        user_id="u1", month="2026-07", total_limit=total, per_category_limits=categories
    )


def codes(status, severity: Optional[str] = None) -> List[str]:
    return [
        alert.code
        for alert in status.alerts
        if severity is None or alert.severity == severity
    ]


AFTER_JULY = datetime(2026, 8, 2, 9, 0)


def test_month_bounds_covers_the_whole_month():
    start, end = month_bounds("2026-02")
    assert start == datetime(2026, 2, 1)
    assert end == datetime(2026, 3, 1)


def test_rejects_malformed_month():
    with pytest.raises(ValueError):
        month_bounds("July 2026")


def test_category_over_limit_raises_critical_alert():
    txs = [tx(350.0, "Food", 5), tx(100.0, "Transport", 6)]
    status = evaluate_budget(
        "u1", "2026-07", txs, budget(1000.0, Food=300.0), now=AFTER_JULY
    )

    food = next(c for c in status.categories if c.category == "Food")
    assert food.status == "over"
    assert food.remaining == -50.0
    # Utilization is stored rounded to two decimals.
    assert food.utilization == 1.17
    assert "limit_exceeded" in codes(status, "critical")


def test_unbudgeted_category_with_meaningful_share_is_flagged():
    txs = [tx(350.0, "Food", 5), tx(100.0, "Transport", 6)]
    status = evaluate_budget(
        "u1", "2026-07", txs, budget(1000.0, Food=300.0), now=AFTER_JULY
    )

    transport = next(c for c in status.categories if c.category == "Transport")
    assert transport.status == "unbudgeted"
    assert transport.limit is None
    assert "no_limit_set" in codes(status, "info")


def test_small_unbudgeted_category_is_not_flagged():
    txs = [tx(990.0, "Food", 5), tx(10.0, "Transport", 6)]
    status = evaluate_budget(
        "u1", "2026-07", txs, budget(5000.0, Food=2000.0), now=AFTER_JULY
    )
    assert "no_limit_set" not in codes(status)


def test_approaching_limit_raises_warning():
    txs = [tx(250.0, "Food", 5)]
    status = evaluate_budget(
        "u1", "2026-07", txs, budget(1000.0, Food=300.0), now=AFTER_JULY
    )
    assert "approaching_limit" in codes(status, "warning")


def test_closed_month_is_not_projected():
    txs = [tx(150.0, "Food", 2)]
    status = evaluate_budget(
        "u1", "2026-07", txs, budget(1000.0, Food=300.0), now=AFTER_JULY
    )
    # The month is over, so the projection equals the actual and no pace
    # warning is raised.
    assert status.projected_total == 150.0
    assert "projected_overspend" not in codes(status)


def test_current_month_projects_from_pace():
    # Day 8 of a 31-day month with 300 spent projects to about 1162.
    now = datetime(2026, 8, 8, 18, 0)
    txs = [tx(300.0, "Food", 4, month="2026-08")]
    status = evaluate_budget(
        "u1",
        "2026-08",
        txs,
        Budget(user_id="u1", month="2026-08", total_limit=1000.0),
        now=now,
    )

    assert status.days_elapsed == 8
    assert status.days_in_month == 31
    assert status.projected_total == 1162.5
    assert "projected_overspend" in codes(status, "warning")


def test_pace_alert_is_suppressed_early_in_the_month():
    # On day 2 the projection multiplies a single day by ~15, which is too noisy
    # to alert on. The projection is still reported.
    now = datetime(2026, 8, 2, 18, 0)
    txs = [tx(300.0, "Food", 1, month="2026-08")]
    status = evaluate_budget(
        "u1",
        "2026-08",
        txs,
        Budget(user_id="u1", month="2026-08", total_limit=1000.0),
        now=now,
    )

    assert status.projected_total == 4650.0
    assert "projected_overspend" not in codes(status)


def test_no_budget_produces_an_informational_alert():
    status = evaluate_budget("u1", "2026-07", [tx(120.0, "Food", 3)], None, now=AFTER_JULY)

    assert status.has_budget is False
    assert status.total_limit is None
    assert codes(status) == ["no_budget"]


def test_credits_reduce_spend():
    txs = [tx(200.0, "Shopping", 5), tx(-50.0, "Shopping", 9)]
    status = evaluate_budget(
        "u1", "2026-07", txs, budget(1000.0, Shopping=300.0), now=AFTER_JULY
    )

    shopping = next(c for c in status.categories if c.category == "Shopping")
    assert shopping.spent == 150.0
    assert status.total_spent == 150.0


def test_transactions_outside_the_month_are_ignored():
    txs = [tx(100.0, "Food", 5), tx(9999.0, "Food", 5, month="2026-06")]
    status = evaluate_budget(
        "u1", "2026-07", txs, budget(1000.0, Food=300.0), now=AFTER_JULY
    )

    assert status.total_spent == 100.0
    assert status.transaction_count == 1


def test_uncategorized_transactions_get_a_bucket():
    orphan = Transaction(
        user_id="u1",
        timestamp=datetime(2026, 7, 5, 10, 0),
        amount=75.0,
        currency="USD",
        source="sms",
    )
    status = evaluate_budget("u1", "2026-07", [orphan], budget(1000.0), now=AFTER_JULY)
    assert [c.category for c in status.categories] == ["Uncategorized"]


def test_alerts_are_ordered_by_severity():
    txs = [tx(400.0, "Food", 5), tx(260.0, "Transport", 6), tx(200.0, "Shopping", 7)]
    status = evaluate_budget(
        "u1",
        "2026-07",
        txs,
        budget(2000.0, Food=300.0, Transport=300.0),
        now=AFTER_JULY,
    )
    severities = [alert.severity for alert in status.alerts]
    assert severities == sorted(severities, key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s])
    assert severities[0] == "critical"


def test_total_limit_breach_is_reported():
    txs = [tx(1200.0, "Rent", 1)]
    status = evaluate_budget("u1", "2026-07", txs, budget(1000.0), now=AFTER_JULY)

    total_alerts = [a for a in status.alerts if a.scope == "total"]
    assert total_alerts[0].code == "limit_exceeded"
    assert status.total_remaining == -200.0
