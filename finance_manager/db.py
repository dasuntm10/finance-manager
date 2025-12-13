from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence
from uuid import uuid4

from finance_manager.schemas import Budget, Transaction


class FinanceRepository:
    """Minimal repository abstraction.

    Replace this with a real Postgres implementation (psycopg/SQLAlchemy) when wiring the DB.
    """

    async def upsert_transactions(self, transactions: Sequence[Transaction]) -> List[Transaction]:
        raise NotImplementedError

    async def list_transactions(self, user_id: str, start: Optional[datetime] = None, end: Optional[datetime] = None) -> List[Transaction]:
        raise NotImplementedError

    async def set_budget(self, budget: Budget) -> Budget:
        raise NotImplementedError

    async def get_budget(self, user_id: str, month: str) -> Optional[Budget]:
        raise NotImplementedError


class InMemoryRepository(FinanceRepository):
    """Simple in-memory store to keep the prototype runnable without external services."""

    def __init__(self) -> None:
        self._transactions: List[Transaction] = []
        self._budgets: Dict[str, Budget] = {}

    async def upsert_transactions(self, transactions: Sequence[Transaction]) -> List[Transaction]:
        saved: List[Transaction] = []
        for tx in transactions:
            tx_id = tx.id or str(uuid4())
            tx = tx.copy(update={"id": tx_id})
            # naive dedupe by merchant+amount+timestamp
            exists = next(
                (
                    existing
                    for existing in self._transactions
                    if existing.user_id == tx.user_id
                    and existing.amount == tx.amount
                    and existing.timestamp == tx.timestamp
                    and existing.merchant_name_raw == tx.merchant_name_raw
                ),
                None,
            )
            if exists:
                self._transactions.remove(exists)
            self._transactions.append(tx)
            saved.append(tx)
        return saved

    async def list_transactions(
        self, user_id: str, start: Optional[datetime] = None, end: Optional[datetime] = None
    ) -> List[Transaction]:
        results = [tx for tx in self._transactions if tx.user_id == user_id]
        if start:
            results = [tx for tx in results if tx.timestamp >= start]
        if end:
            results = [tx for tx in results if tx.timestamp <= end]
        return results

    async def set_budget(self, budget: Budget) -> Budget:
        key = f"{budget.user_id}:{budget.month}"
        if budget.id is None:
            budget = budget.copy(update={"id": str(uuid4())})
        self._budgets[key] = budget
        return budget

    async def get_budget(self, user_id: str, month: str) -> Optional[Budget]:
        return self._budgets.get(f"{user_id}:{month}")

    async def summarize_by_category(self, user_id: str) -> Dict[str, float]:
        totals: Dict[str, float] = defaultdict(float)
        for tx in self._transactions:
            if tx.user_id != user_id:
                continue
            cat = tx.category or "Uncategorized"
            totals[cat] += tx.amount
        return totals


def get_repository() -> FinanceRepository:
    # In production, choose a Postgres-backed repository here.
    global _REPO_SINGLETON  # type: ignore
    try:
        repo = _REPO_SINGLETON  # type: ignore
    except NameError:
        repo = InMemoryRepository()
        _REPO_SINGLETON = repo  # type: ignore
    return repo


