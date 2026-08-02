"""Smoke tests for the HTTP surface of the new endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from finance_manager.api import main as api_main
from finance_manager.db import InMemoryRepository
from finance_manager.graph import FinanceGraphRunner
from finance_manager.vector_store import NullVectorStore


NOW = datetime.utcnow()
CURRENT_MONTH = NOW.strftime("%Y-%m")
ANCHOR = datetime(NOW.year, NOW.month, 15, 9, 0)


def email(body: str, message_id: str, days_ago: int = 0) -> dict:
    return {
        "message_id": message_id,
        "sender": "alerts@mybank.com",
        "subject": "Transaction alert",
        "received_at": (ANCHOR - timedelta(days=days_ago)).isoformat(),
        "body": body,
    }


MESSAGES = [
    email("Your card was debited USD 42.75 at WHOLE FOODS.", "<one@b>"),
    email("Your card was debited USD 12.99 at NETFLIX.COM.", "<two@b>"),
]


@pytest.fixture
def client() -> TestClient:
    """A client backed by a fresh in-memory repository per test.

    The runner and the repository dependencies are both overridden with the same
    store, so writes through /budget are visible to the graph.
    """
    repo = InMemoryRepository()
    runner = FinanceGraphRunner(repo=repo, vector_store=NullVectorStore())
    api_main.app.dependency_overrides[api_main.get_runner] = lambda: runner
    api_main.app.dependency_overrides[api_main.get_repository] = lambda: repo
    with TestClient(api_main.app) as test_client:
        yield test_client
    api_main.app.dependency_overrides.clear()


def ingest(client: TestClient) -> dict:
    response = client.post("/ingest/email", json={"user_id": "u1", "messages": MESSAGES})
    assert response.status_code == 200
    return response.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ingest_email_returns_transactions_and_budget_status(client):
    body = ingest(client)

    assert body["emails_fetched"] == 2
    assert body["transactions_parsed"] == 2
    assert body["errors"] == []
    assert body["budget_status"]["user_id"] == "u1"


def test_budget_status_endpoint(client):
    ingest(client)
    client.post(
        "/budget",
        json={
            "user_id": "u1",
            "month": CURRENT_MONTH,
            "total_limit": 40.0,
            "per_category_limits": {"Groceries": 10.0},
        },
    )

    response = client.get("/budget/status", params={"user_id": "u1", "month": CURRENT_MONTH})
    status = response.json()["budget_status"]

    assert response.status_code == 200
    assert status["has_budget"] is True
    assert status["total_spent"] == 55.74
    assert "limit_exceeded" in [alert["code"] for alert in status["alerts"]]


def test_recurring_endpoint(client):
    messages = [
        email(
            "Your card was debited USD 12.99 at NETFLIX.COM.",
            f"<n{n}@b>",
            days_ago=5 + 30 * n,
        )
        for n in range(6)
    ]
    client.post("/ingest/email", json={"user_id": "u1", "messages": messages})

    response = client.get("/recurring", params={"user_id": "u1"})
    body = response.json()

    assert response.status_code == 200
    assert body["summary"]["active_count"] == 1
    assert body["series"][0]["cadence"] == "monthly"


def test_monthly_report_json(client):
    ingest(client)
    response = client.post(
        "/reports/monthly",
        json={"user_id": "u1", "month": CURRENT_MONTH, "include_narrative": False},
    )
    report = response.json()["report"]

    assert response.status_code == 200
    assert report["total_spent"] == 55.74
    assert report["highlights"]


def test_monthly_report_html_and_download(client):
    ingest(client)
    params = {"user_id": "u1", "month": CURRENT_MONTH, "include_narrative": False}

    response = client.get("/reports/monthly.html", params=params)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "WHOLE FOODS" in response.text
    assert "Content-Disposition" not in response.headers

    download = client.get("/reports/monthly.html", params={**params, "download": True})
    assert "attachment" in download.headers["Content-Disposition"]
    assert CURRENT_MONTH in download.headers["Content-Disposition"]


def test_sms_ingest_still_returns_budget_status(client):
    response = client.post(
        "/ingest/sms",
        json={
            "user_id": "u1",
            "messages": [{"text": "Debited USD 20.00 at CAFE ROMA", "sender": "BOC"}],
            "senders": ["BOC"],
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["budget_status"] is not None
    assert body["errors"] == []
