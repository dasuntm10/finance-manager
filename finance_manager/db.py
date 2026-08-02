from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple
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


def transaction_fingerprint(tx: Transaction) -> Tuple[str, str, float, str, str]:
    """Build the identity key used to recognize an already-stored transaction.

    ``source_doc_id`` carries the email Message-ID on the email path, which makes
    re-fetching the same mailbox idempotent. It is included alongside the amount,
    merchant and timestamp rather than used alone because a single PDF document
    id is shared by every transaction extracted from that statement.
    """
    return (
        tx.user_id,
        tx.source_doc_id or "",
        round(float(tx.amount), 2),
        (tx.merchant_name_raw or "").strip().lower(),
        tx.timestamp.isoformat(),
    )


class InMemoryRepository(FinanceRepository):
    """Simple in-memory store to keep the prototype runnable without external services."""

    def __init__(self) -> None:
        # Keyed by transaction id, with a fingerprint index alongside so upserts
        # are O(1) rather than a full scan per incoming transaction.
        self._transactions: Dict[str, Transaction] = {}
        self._fingerprints: Dict[Tuple[str, str, float, str, str], str] = {}
        self._budgets: Dict[str, Budget] = {}

    async def upsert_transactions(self, transactions: Sequence[Transaction]) -> List[Transaction]:
        saved: List[Transaction] = []
        for tx in transactions:
            fingerprint = transaction_fingerprint(tx)
            existing_id = self._fingerprints.get(fingerprint)
            # Reuse the stored id on a repeat so downstream references stay valid.
            tx_id = existing_id or tx.id or str(uuid4())
            stored = tx.model_copy(update={"id": tx_id})
            self._transactions[tx_id] = stored
            self._fingerprints[fingerprint] = tx_id
            saved.append(stored)
        return saved

    async def list_transactions(
        self, user_id: str, start: Optional[datetime] = None, end: Optional[datetime] = None
    ) -> List[Transaction]:
        results = [tx for tx in self._transactions.values() if tx.user_id == user_id]
        if start:
            results = [tx for tx in results if tx.timestamp >= start]
        if end:
            results = [tx for tx in results if tx.timestamp <= end]
        results.sort(key=lambda tx: tx.timestamp)
        return results

    async def set_budget(self, budget: Budget) -> Budget:
        key = f"{budget.user_id}:{budget.month}"
        if budget.id is None:
            budget = budget.model_copy(update={"id": str(uuid4())})
        self._budgets[key] = budget
        return budget

    async def get_budget(self, user_id: str, month: str) -> Optional[Budget]:
        return self._budgets.get(f"{user_id}:{month}")

    async def summarize_by_category(self, user_id: str) -> Dict[str, float]:
        totals: Dict[str, float] = defaultdict(float)
        for tx in self._transactions.values():
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


