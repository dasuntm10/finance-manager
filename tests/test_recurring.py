"""Tests for recurring charge and subscription detection."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from finance_manager.analysis.recurring import detect_recurring, summarize_recurring
from finance_manager.ingestion.parsing import normalize_merchant
from finance_manager.schemas import Transaction


NOW = datetime(2026, 8, 2, 12, 0)


def tx(
    merchant: str,
    amount: float,
    when: datetime,
    category: Optional[str] = "Entertainment",
    currency: str = "USD",
) -> Transaction:
    return Transaction(
        id=f"{merchant}-{when.date()}-{amount}",
        user_id="u1",
        timestamp=when,
        amount=amount,
        currency=currency,
        merchant_name_raw=merchant,
        merchant_normalized=normalize_merchant(merchant),
        category=category,
        source="email",
    )


def series_every(merchant: str, amount: float, gap_days: int, count: int, ends_days_ago: int) -> List[Transaction]:
    """Build `count` charges `gap_days` apart, the last one `ends_days_ago` ago."""
    last = NOW - timedelta(days=ends_days_ago)
    return [
        tx(merchant, amount, last - timedelta(days=gap_days * offset))
        for offset in reversed(range(count))
    ]


def test_detects_monthly_subscription():
    txs = series_every("NETFLIX.COM", 12.99, gap_days=30, count=6, ends_days_ago=5)
    found = detect_recurring(txs, now=NOW)

    assert len(found) == 1
    netflix = found[0]
    assert netflix.cadence == "monthly"
    assert netflix.occurrences == 6
    assert netflix.status == "active"
    assert netflix.typical_amount == 12.99
    # 30-day cadence normalized to an average month.
    assert netflix.monthly_cost == 13.18
    assert netflix.days_until_next == 25
    assert netflix.confidence >= 0.9


def test_groups_merchant_spelling_variants_together():
    txs = [
        tx("NETFLIX.COM 4567", 12.99, NOW - timedelta(days=95)),
        tx("Netflix Com", 12.99, NOW - timedelta(days=65)),
        tx("netflix.com", 12.99, NOW - timedelta(days=35)),
        tx("NETFLIX.COM", 12.99, NOW - timedelta(days=5)),
    ]
    found = detect_recurring(txs, now=NOW)
    assert len(found) == 1
    assert found[0].occurrences == 4


def test_flags_price_increase_on_latest_charge():
    txs = series_every("SPOTIFY", 12.99, gap_days=30, count=5, ends_days_ago=35)
    txs.append(tx("SPOTIFY", 15.99, NOW - timedelta(days=5)))

    found = detect_recurring(txs, now=NOW)
    assert len(found) == 1
    assert found[0].last_amount == 15.99
    assert found[0].amount_change_pct is not None
    assert 22 < found[0].amount_change_pct < 24


def test_marks_stale_series_as_lapsed():
    # Monthly cadence but nothing for four months: likely cancelled.
    txs = series_every("OLD GYM", 45.0, gap_days=30, count=4, ends_days_ago=120)
    found = detect_recurring(txs, now=NOW)

    assert len(found) == 1
    assert found[0].status == "lapsed"
    assert found[0].next_expected is None
    assert found[0].days_until_next is None


def test_ignores_irregular_merchant():
    # Gaps of 3, 30 and 61 days: the median matches a monthly cadence but too
    # few gaps sit inside the tolerance, so it must not be reported.
    start = NOW - timedelta(days=100)
    txs = [
        tx("CORNER SHOP", 8.0, start),
        tx("CORNER SHOP", 12.0, start + timedelta(days=3)),
        tx("CORNER SHOP", 9.5, start + timedelta(days=33)),
        tx("CORNER SHOP", 11.0, start + timedelta(days=94)),
    ]
    assert detect_recurring(txs, now=NOW) == []


def test_ignores_unknown_cadence():
    # A regular 20-day rhythm matches no known billing cadence.
    txs = series_every("ODD BILLER", 25.0, gap_days=20, count=5, ends_days_ago=2)
    assert detect_recurring(txs, now=NOW) == []


def test_requires_minimum_occurrences():
    txs = series_every("NEW THING", 9.99, gap_days=30, count=2, ends_days_ago=1)
    assert detect_recurring(txs, now=NOW) == []
    # Two charges is enough once the caller lowers the bar.
    assert len(detect_recurring(txs, min_occurrences=2, now=NOW)) == 1


def test_min_occurrences_below_two_is_clamped():
    # A single charge has no interval at all; asking for min_occurrences=1 used
    # to reach median([]) and raise.
    singles = [tx("ONE OFF", 30.0, NOW - timedelta(days=4))]
    assert detect_recurring(singles, min_occurrences=1, now=NOW) == []

    pair = series_every("PAIR", 9.99, gap_days=30, count=2, ends_days_ago=1)
    assert len(detect_recurring(pair, min_occurrences=1, now=NOW)) == 1


def test_ignores_credits():
    # Refunds carry negative amounts and are money in, not a subscription.
    txs = series_every("PAYROLL", -3000.0, gap_days=30, count=6, ends_days_ago=2)
    assert detect_recurring(txs, now=NOW) == []


def test_collapses_same_day_duplicate_charges():
    txs = series_every("HOSTING", 20.0, gap_days=30, count=5, ends_days_ago=3)
    # A retry a few hours later must not read as a zero-length cadence.
    txs.append(tx("HOSTING", 20.0, NOW - timedelta(days=3) + timedelta(hours=6)))

    found = detect_recurring(txs, now=NOW)
    assert len(found) == 1
    assert found[0].occurrences == 5
    assert found[0].cadence == "monthly"


def test_detects_weekly_cadence():
    txs = series_every("LAUNDRY", 15.0, gap_days=7, count=8, ends_days_ago=1)
    found = detect_recurring(txs, now=NOW)
    assert found[0].cadence == "weekly"
    # Weekly at 15.00 is roughly 65 per month.
    assert 64 < found[0].monthly_cost < 66


def test_variable_amounts_still_count_as_recurring():
    # Utility bills recur reliably at amounts that move month to month.
    amounts = [82.0, 110.0, 64.0, 95.0, 130.0]
    last = NOW - timedelta(days=4)
    txs = [
        tx("CEB ELECTRICITY", amount, last - timedelta(days=30 * offset), category="Utilities")
        for offset, amount in enumerate(reversed(amounts))
    ]
    found = detect_recurring(txs, now=NOW)
    assert len(found) == 1
    assert found[0].amount_stability == "variable"
    assert found[0].cadence == "monthly"


def test_summary_rolls_up_active_series():
    txs = (
        series_every("NETFLIX", 12.99, gap_days=30, count=6, ends_days_ago=5)
        + series_every("OLD GYM", 45.0, gap_days=30, count=4, ends_days_ago=120)
        + series_every("LAUNDRY", 15.0, gap_days=7, count=8, ends_days_ago=1)
    )
    summary = summarize_recurring(detect_recurring(txs, now=NOW), upcoming_days=30)

    assert summary.total_series == 3
    assert summary.active_count == 2
    assert summary.lapsed_count == 1
    # Lapsed series must not inflate the running monthly cost.
    assert summary.monthly_total == round(13.18 + 65.23, 2)
    assert summary.annual_total == round(summary.monthly_total * 12, 2)
    assert [s.merchant_label for s in summary.upcoming] == ["LAUNDRY", "NETFLIX"]
    assert len(summary.series) == 3
