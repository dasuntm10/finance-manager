"""End-to-end tests over the compiled LangGraph pipeline.

These use the in-memory repository and pre-supplied email payloads, so they run
without IMAP, Qdrant, or an LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest

from finance_manager.db import InMemoryRepository
from finance_manager.graph import FinanceGraphRunner, guard_node
from finance_manager.schemas import Budget
from finance_manager.vector_store import NullVectorStore


NOW = datetime.utcnow()
CURRENT_MONTH = NOW.strftime("%Y-%m")
# Mid-month anchor so every fixture transaction lands in the current month no
# matter which day the suite runs on.
ANCHOR = datetime(NOW.year, NOW.month, 15, 9, 0)


def email(subject: str, body: str, message_id: str, days_ago: int = 0) -> Dict[str, Any]:
    return {
        "message_id": message_id,
        "sender": "alerts@mybank.com",
        "subject": subject,
        "received_at": (ANCHOR - timedelta(days=days_ago)).isoformat(),
        "body": body,
    }


def bank_emails() -> List[Dict[str, Any]]:
    return [
        email(
            "Transaction alert",
            "Your card ending 4532 was debited USD 42.75 at WHOLE FOODS. "
            "Available balance USD 1,204.10",
            "<one@mybank.com>",
        ),
        email(
            "Transaction alert",
            "Your card was debited USD 12.99 at NETFLIX.COM.",
            "<two@mybank.com>",
        ),
        email("Your e-statement is ready", "Log in to view it.", "<three@mybank.com>"),
    ]


@pytest.fixture
def runner() -> FinanceGraphRunner:
    return FinanceGraphRunner(repo=InMemoryRepository(), vector_store=NullVectorStore())


async def test_email_batch_ingests_persists_and_checks_budget(runner):
    state = await runner.arun(
        task_type="email_batch",
        raw_input={"messages": bank_emails()},
        user_id="u1",
    )

    assert state.get("errors") == []
    # The e-statement notice carries no amount and is skipped.
    assert len(state["parsed_transactions"]) == 2
    assert len(state["email_messages"]) == 3

    stored = await runner.repo.list_transactions("u1")
    assert {tx.merchant_name_raw for tx in stored} == {"WHOLE FOODS", "NETFLIX.COM"}
    assert all(tx.source == "email" for tx in stored)
    # Categorization ran on the way through.
    assert all(tx.category for tx in stored)

    status = state["budget_status"]
    assert status is not None
    assert status["user_id"] == "u1"
    assert status["has_budget"] is False
    assert [alert["code"] for alert in status["alerts"]] == ["no_budget"]


async def test_explicit_user_id_is_not_dropped(runner):
    # Regression guard: an unparenthesized ternary used to discard the explicit
    # user_id whenever raw_input was empty.
    state = await runner.arun(task_type="recurring", user_id="alice")
    assert state["user_id"] == "alice"


async def test_repeated_email_ingest_is_idempotent(runner):
    for _ in range(2):
        await runner.arun(
            task_type="email_batch", raw_input={"messages": bank_emails()}, user_id="u1"
        )
    assert len(await runner.repo.list_transactions("u1")) == 2


async def test_budget_task_reports_alerts(runner):
    await runner.arun(
        task_type="email_batch", raw_input={"messages": bank_emails()}, user_id="u1"
    )
    await runner.repo.set_budget(
        Budget(
            user_id="u1",
            month=CURRENT_MONTH,
            total_limit=40.0,
            per_category_limits={"Groceries": 10.0},
        )
    )

    state = await runner.arun(task_type="budget", raw_input={}, user_id="u1")
    status = state["budget_status"]

    assert status["has_budget"] is True
    assert status["total_spent"] > 40.0
    assert "limit_exceeded" in [alert["code"] for alert in status["alerts"]]
    # The budget node must not overwrite the analytics payload any more.
    assert state.get("analytics_result") is None


async def test_recurring_task_returns_summary_and_series(runner):
    messages = [
        email(
            "Transaction alert",
            "Your card was debited USD 12.99 at NETFLIX.COM.",
            f"<netflix-{n}@mybank.com>",
            days_ago=5 + 30 * n,
        )
        for n in range(6)
    ]
    await runner.arun(task_type="email_batch", raw_input={"messages": messages}, user_id="u1")

    state = await runner.arun(task_type="recurring", raw_input={}, user_id="u1")

    assert state["errors"] == []
    assert state["recurring_summary"]["active_count"] == 1
    assert state["recurring_series"][0]["cadence"] == "monthly"
    assert state["recurring_series"][0]["merchant_label"] == "NETFLIX.COM"


async def test_report_task_builds_a_report(runner):
    await runner.arun(
        task_type="email_batch", raw_input={"messages": bank_emails()}, user_id="u1"
    )

    state = await runner.arun(
        task_type="report",
        raw_input={"month": CURRENT_MONTH, "include_narrative": False},
        user_id="u1",
    )
    report = state["report_result"]

    assert report["user_id"] == "u1"
    assert report["total_spent"] == 55.74
    assert report["transaction_count"] == 2
    assert report["highlights"]
    assert report["budget"] is not None


async def test_unknown_task_type_fails_loudly(runner):
    with pytest.raises(ValueError, match="Unsupported task_type"):
        await runner.arun(task_type="teleport", raw_input={}, user_id="u1")


async def test_guard_records_async_node_failures():
    async def boom(state):
        raise RuntimeError("kaboom")

    result = await guard_node("exploder", boom)({"request_id": "r1", "errors": []})
    assert result["errors"] == ["exploder: kaboom"]


def test_guard_records_sync_node_failures():
    def boom(state):
        raise ValueError("nope")

    result = guard_node("exploder", boom)({"request_id": "r1", "errors": ["earlier"]})
    assert result["errors"] == ["earlier", "exploder: nope"]


async def test_a_failing_node_does_not_kill_the_run(runner, monkeypatch):
    # Force the persistence step to fail and confirm the pipeline still finishes
    # with the error recorded rather than raising out of the graph.
    async def broken_upsert(transactions):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(runner.repo, "upsert_transactions", broken_upsert)
    state = await runner.arun(
        task_type="email_batch", raw_input={"messages": bank_emails()}, user_id="u1"
    )

    assert any("persistence: database unavailable" in err for err in state["errors"])
    assert state.get("budget_status") is not None
