"""Recurring charge and subscription detection.

Groups a user's transactions by normalized merchant, then looks for a regular
cadence in the gaps between charges. A series is recurring when its intervals
cluster around a known cadence (weekly through yearly); amount stability is
recorded but not required, because utility bills recur reliably at amounts that
vary month to month.

The detector is deliberately dependency-free and deterministic so it can be unit
tested without a database, an LLM or a network call.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median, pstdev
from typing import Dict, List, Optional, Sequence, Tuple

from finance_manager.ingestion.parsing import normalize_merchant
from finance_manager.schemas import RecurringSeries, RecurringSummary, Transaction


DAYS_PER_MONTH = 30.44

# (label, expected interval in days, tolerance in days)
CADENCE_SPECS: Tuple[Tuple[str, float, float], ...] = (
    ("weekly", 7.0, 2.0),
    ("biweekly", 14.0, 3.0),
    ("monthly", DAYS_PER_MONTH, 6.0),
    ("quarterly", 91.31, 12.0),
    ("yearly", 365.25, 30.0),
)

# Charges closer together than this are treated as one event (split payments,
# retries, or an authorization plus its capture) rather than a short cadence.
_SAME_EVENT_DAYS = 2.0
# Fraction of gaps that must sit within the cadence tolerance.
_MIN_REGULAR_FRACTION = 0.6
# A price change smaller than this is noise (rounding, FX drift).
_PRICE_CHANGE_EPSILON_PCT = 1.0


def _match_cadence(interval_days: float) -> Optional[Tuple[str, float, float]]:
    """Return the cadence spec whose expected interval covers this gap."""
    for label, expected, tolerance in CADENCE_SPECS:
        if abs(interval_days - expected) <= tolerance:
            return label, expected, tolerance
    return None


def _collapse_same_event(
    transactions: Sequence[Transaction],
) -> List[Transaction]:
    """Drop charges that land within a couple of days of the previous one."""
    collapsed: List[Transaction] = []
    for tx in transactions:
        if collapsed:
            gap = (tx.timestamp - collapsed[-1].timestamp).total_seconds() / 86400.0
            if gap < _SAME_EVENT_DAYS:
                continue
        collapsed.append(tx)
    return collapsed


def _coefficient_of_variation(values: Sequence[float]) -> float:
    """Standard deviation over mean, guarded against a zero mean."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    return pstdev(values) / abs(mean)


def _group_key(tx: Transaction) -> str:
    normalized = tx.merchant_normalized or normalize_merchant(tx.merchant_name_raw)
    if normalized:
        return normalized
    return (tx.merchant_name_raw or tx.description or "unknown").strip().lower()[:80]


def _confidence(
    occurrences: int, interval_cv: float, amount_cv: float
) -> float:
    occurrence_score = min(1.0, max(0.0, (occurrences - 2) / 4.0))
    regularity_score = max(0.0, 1.0 - interval_cv / 0.5)
    amount_score = max(0.0, 1.0 - amount_cv / 0.5)
    score = 0.4 * occurrence_score + 0.4 * regularity_score + 0.2 * amount_score
    return round(min(1.0, max(0.0, score)), 2)


def detect_recurring(
    transactions: Sequence[Transaction],
    min_occurrences: int = 3,
    now: Optional[datetime] = None,
    amount_stability_threshold: float = 0.15,
) -> List[RecurringSeries]:
    """Find recurring charge series in a transaction list.

    Only debits are considered: refunds and salary credits are money in, not
    subscriptions. Series are returned sorted by monthly cost, largest first.
    """
    reference = now or datetime.utcnow()
    # A cadence needs at least two charges to have an interval at all, so a
    # caller-supplied lower bound is clamped rather than trusted.
    min_occurrences = max(2, int(min_occurrences))
    groups: Dict[Tuple[str, str], List[Transaction]] = defaultdict(list)
    for tx in transactions:
        if tx.amount is None or tx.amount <= 0:
            continue
        groups[(_group_key(tx), tx.currency)].append(tx)

    series: List[RecurringSeries] = []
    for (key, currency), items in groups.items():
        if len(items) < min_occurrences:
            continue
        ordered = _collapse_same_event(sorted(items, key=lambda t: t.timestamp))
        if len(ordered) < min_occurrences:
            continue

        gaps = [
            (b.timestamp - a.timestamp).total_seconds() / 86400.0
            for a, b in zip(ordered, ordered[1:])
        ]
        gaps = [gap for gap in gaps if gap > 0]
        if not gaps or len(gaps) < min_occurrences - 1:
            continue

        median_gap = median(gaps)
        matched = _match_cadence(median_gap)
        if matched is None:
            continue
        cadence, expected_days, tolerance = matched

        regular = [gap for gap in gaps if abs(gap - median_gap) <= tolerance]
        if len(regular) / len(gaps) < _MIN_REGULAR_FRACTION:
            continue

        amounts = [tx.amount for tx in ordered]
        typical_amount = median(amounts)
        last_amount = amounts[-1]
        amount_cv = _coefficient_of_variation(amounts)
        interval_cv = _coefficient_of_variation(gaps)

        prior_amounts = amounts[:-1]
        change_pct: Optional[float] = None
        if prior_amounts:
            prior_median = median(prior_amounts)
            if prior_median:
                raw_change = (last_amount - prior_median) / prior_median * 100.0
                if abs(raw_change) >= _PRICE_CHANGE_EPSILON_PCT:
                    change_pct = round(raw_change, 2)

        last_seen = ordered[-1].timestamp
        days_since_last = (reference - last_seen).total_seconds() / 86400.0
        # Allow one missed cycle plus a few days of settlement lag before
        # calling a subscription lapsed.
        is_lapsed = days_since_last > (median_gap * 1.5 + 3)
        next_expected = last_seen + timedelta(days=median_gap)

        series.append(
            RecurringSeries(
                merchant_key=key,
                merchant_label=(
                    ordered[-1].merchant_name_raw or key
                ).strip()[:80],
                category=ordered[-1].category,
                currency=currency,
                cadence=cadence,
                interval_days=round(median_gap, 2),
                expected_interval_days=expected_days,
                occurrences=len(ordered),
                first_seen=ordered[0].timestamp,
                last_seen=last_seen,
                typical_amount=round(typical_amount, 2),
                last_amount=round(last_amount, 2),
                monthly_cost=round(typical_amount * (DAYS_PER_MONTH / median_gap), 2),
                amount_change_pct=change_pct,
                amount_stability=(
                    "fixed" if amount_cv <= amount_stability_threshold else "variable"
                ),
                status="lapsed" if is_lapsed else "active",
                next_expected=None if is_lapsed else next_expected,
                days_until_next=(
                    None if is_lapsed else int(round((next_expected - reference).total_seconds() / 86400.0))
                ),
                confidence=_confidence(len(ordered), interval_cv, amount_cv),
                transaction_ids=[tx.id for tx in ordered if tx.id],
            )
        )

    series.sort(key=lambda s: s.monthly_cost, reverse=True)
    return series


def summarize_recurring(
    series: Sequence[RecurringSeries], upcoming_days: int = 14
) -> RecurringSummary:
    """Roll a list of series up into headline subscription numbers."""
    active = [s for s in series if s.status == "active"]
    lapsed = [s for s in series if s.status == "lapsed"]
    upcoming = sorted(
        [
            s
            for s in active
            if s.days_until_next is not None and s.days_until_next <= upcoming_days
        ],
        key=lambda s: s.days_until_next if s.days_until_next is not None else 0,
    )
    increases = [
        s for s in active if s.amount_change_pct is not None and s.amount_change_pct > 0
    ]
    increases.sort(key=lambda s: s.amount_change_pct or 0.0, reverse=True)

    return RecurringSummary(
        total_series=len(series),
        active_count=len(active),
        lapsed_count=len(lapsed),
        monthly_total=round(sum(s.monthly_cost for s in active), 2),
        annual_total=round(sum(s.monthly_cost for s in active) * 12, 2),
        series=list(series),
        upcoming=upcoming,
        price_increases=increases,
    )
