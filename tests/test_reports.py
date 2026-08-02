"""Tests for monthly report assembly, narrative fallback and HTML rendering."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

import pytest

from finance_manager.config import Settings
from finance_manager.db import InMemoryRepository
from finance_manager.ingestion.parsing import normalize_merchant
from finance_manager.reports.builder import build_monthly_report, previous_month
from finance_manager.reports.narrative import build_brief, fallback_narrative
from finance_manager.reports.render import render_report_html
from finance_manager.schemas import Budget, Transaction


NOW = datetime(2026, 8, 20, 12, 0)
SETTINGS = Settings()


def tx(merchant: str, amount: float, when: datetime, category: str) -> Transaction:
    return Transaction(
        user_id="u1",
        timestamp=when,
        amount=amount,
        currency="USD",
        merchant_name_raw=merchant,
        merchant_normalized=normalize_merchant(merchant),
        category=category,
        source="email",
    )


def seed() -> List[Transaction]:
    """Two months of spend plus a monthly subscription running since March."""
    july = [
        tx("WHOLE FOODS", 200.0, datetime(2026, 7, 4, 9, 0), "Groceries"),
        tx("UBER", 60.0, datetime(2026, 7, 9, 9, 0), "Transport"),
        tx("CAFE ROMA", 40.0, datetime(2026, 7, 15, 9, 0), "Food"),
    ]
    august = [
        tx("WHOLE FOODS", 320.0, datetime(2026, 8, 3, 9, 0), "Groceries"),
        tx("WHOLE FOODS", 90.0, datetime(2026, 8, 11, 9, 0), "Groceries"),
        tx("UBER", 45.0, datetime(2026, 8, 6, 9, 0), "Transport"),
        tx("CAFE ROMA", 25.0, datetime(2026, 8, 12, 9, 0), "Food"),
        tx("BIG APPLIANCE", 500.0, datetime(2026, 8, 14, 9, 0), "Shopping"),
    ]
    netflix = [
        tx("NETFLIX.COM", 12.99, datetime(2026, 8, 15, 9, 0) - timedelta(days=30 * n), "Entertainment")
        for n in range(6)
    ]
    return july + august + netflix


async def build(repo: InMemoryRepository, month: str = "2026-08", narrative: bool = False):
    return await build_monthly_report(
        repo, user_id="u1", month=month, settings=SETTINGS, now=NOW, include_narrative=narrative
    )


@pytest.fixture
async def repo() -> InMemoryRepository:
    store = InMemoryRepository()
    await store.upsert_transactions(seed())
    return store


def test_previous_month_wraps_across_years():
    assert previous_month("2026-08") == "2026-07"
    assert previous_month("2026-01") == "2025-12"


async def test_report_totals_and_month_over_month(repo):
    report = await build(repo)

    # August: 320 + 90 + 45 + 25 + 500 plus the 12.99 Netflix charge on the 15th.
    assert report.total_spent == 992.99
    assert report.transaction_count == 6
    # July: 200 + 60 + 40 plus Netflix on the 16th.
    assert report.previous_total == 312.99
    assert report.change_pct == 217.3
    assert report.currency == "USD"


async def test_categories_are_ranked_and_compared(repo):
    report = await build(repo)

    assert [line.category for line in report.categories][:2] == ["Shopping", "Groceries"]
    groceries = next(line for line in report.categories if line.category == "Groceries")
    assert groceries.amount == 410.0
    assert groceries.previous_amount == 200.0
    assert groceries.change_pct == 105.0
    assert 0 < groceries.share < 1


async def test_top_merchants_are_aggregated(repo):
    report = await build(repo)

    top = report.top_merchants[0]
    assert top.merchant == "BIG APPLIANCE"
    assert top.amount == 500.0
    # The two Whole Foods trips collapse into one merchant line.
    whole_foods = next(m for m in report.top_merchants if "WHOLE FOODS" in m.merchant)
    assert whole_foods.transactions == 2
    assert whole_foods.amount == 410.0


async def test_largest_transaction_is_reported(repo):
    report = await build(repo)
    assert report.largest_transaction["merchant"] == "BIG APPLIANCE"
    assert report.largest_transaction["amount"] == 500.0


async def test_report_includes_budget_status(repo):
    await repo.set_budget(
        Budget(
            user_id="u1",
            month="2026-08",
            total_limit=800.0,
            per_category_limits={"Groceries": 300.0},
        )
    )
    report = await build(repo)

    assert report.budget is not None
    assert report.budget.has_budget is True
    assert report.budget.total_spent == 992.99
    codes = [alert.code for alert in report.budget.alerts]
    assert "limit_exceeded" in codes


async def test_report_detects_recurring_charges(repo):
    report = await build(repo)

    assert report.recurring.active_count == 1
    netflix = report.recurring.series[0]
    assert netflix.merchant_label == "NETFLIX.COM"
    assert netflix.cadence == "monthly"
    assert report.recurring.monthly_total > 0


async def test_highlights_are_generated_without_an_llm(repo):
    report = await build(repo)

    assert report.narrative is None
    assert len(report.highlights) >= 3
    joined = " ".join(report.highlights)
    assert "Shopping" in joined
    assert "217" in joined


async def test_empty_month_still_produces_a_report(repo):
    report = await build(repo, month="2020-01")

    assert report.total_spent == 0.0
    assert report.transaction_count == 0
    assert report.categories == []
    assert report.highlights  # falls back to a plain total line


async def test_narrative_fallback_is_deterministic(repo):
    report = await build(repo)
    text = fallback_narrative(report)

    assert "992.99" in text
    assert "2026-08" in text
    # Running it again gives the identical string.
    assert fallback_narrative(report) == text


async def test_brief_contains_the_grounding_figures(repo):
    report = await build(repo)
    brief = build_brief(report)

    assert "Total spend: 992.99" in brief
    assert "Previous month total: 312.99" in brief
    assert "Top categories:" in brief


async def test_renders_standalone_html(repo):
    await repo.set_budget(
        Budget(user_id="u1", month="2026-08", total_limit=800.0, per_category_limits={"Food": 50.0})
    )
    report = await build(repo)
    html = render_report_html(report)

    assert html.startswith("<!doctype html>")
    assert "Monthly report 2026-08" in html
    assert "992.99" in html
    assert "BIG APPLIANCE" in html
    assert "NETFLIX.COM" in html
    # No external assets: the document must render offline.
    assert "http://" not in html
    assert "<script" not in html


async def test_html_escapes_narrative_text(repo):
    report = await build(repo)
    report.narrative = "Spending <script>alert(1)</script> rose"
    html = render_report_html(report)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
