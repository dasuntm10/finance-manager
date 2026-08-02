"""Tests for the shared bank-message parsing helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from finance_manager.ingestion.parsing import (
    normalize_merchant,
    parse_currency,
    parse_direction,
    parse_merchant,
    parse_money,
    to_naive_utc,
)


SMS_DEBIT = (
    "Your card ending 4532 was debited LKR 4,500.00 at KEELLS SUPER "
    "on 02/08/2026. Available balance LKR 125,300.00"
)


def test_parses_amount_next_to_currency():
    money = parse_money(SMS_DEBIT)
    assert money is not None
    assert money.amount == 4500.00
    assert money.currency == "LKR"
    assert money.matched_by == "currency_prefix"


def test_ignores_card_digits_and_dates():
    # The card suffix (4532) and the date parts must never win over the amount.
    money = parse_money("Card XX1234 debited USD 25.50 on 11/07/2026")
    assert money is not None
    assert money.amount == 25.50
    assert money.currency == "USD"


def test_ignores_running_balance():
    # The balance is larger than the charge, so a naive "largest number" parser
    # would pick it. The balance clause is stripped before matching.
    money = parse_money(SMS_DEBIT)
    assert money is not None
    assert money.amount != 125300.00


def test_amount_after_number_then_currency():
    money = parse_money("Purchase of 1,299.99 USD at STEAM GAMES")
    assert money is not None
    assert money.amount == 1299.99
    assert money.currency == "USD"


def test_amount_from_keyword_without_currency():
    money = parse_money("Payment of 1500 to CEB was successful")
    assert money is not None
    assert money.amount == 1500.0
    assert money.matched_by == "keyword"


def test_returns_none_for_message_without_amount():
    assert parse_money("Your monthly e-statement is now available") is None


def test_direction_detects_debit_and_credit():
    assert parse_direction(SMS_DEBIT) == "debit"
    assert parse_direction("LKR 2,000.00 credited to your account") == "credit"
    # Refund wording wins even though it also describes the original purchase.
    assert parse_direction("Refund for your purchase of USD 30 at ASOS") == "credit"


def test_merchant_extraction_stops_at_trailing_clause():
    assert parse_merchant(SMS_DEBIT) == "KEELLS SUPER"
    assert parse_merchant("Paid to SPOTIFY for Premium") == "SPOTIFY"


def test_merchant_extraction_returns_none_when_absent():
    assert parse_merchant("Transaction completed successfully") is None


def test_normalize_merchant_collapses_variants():
    assert normalize_merchant("NETFLIX.COM 4567") == "netflix"
    assert normalize_merchant("Netflix Com") == "netflix"
    assert normalize_merchant("Acme Foods (Pvt) Ltd") == "acme foods"
    assert normalize_merchant(None) == ""


def test_parse_currency_prefers_explicit_code():
    assert parse_currency("Spent 40 GBP at Tesco") == "GBP"
    assert parse_currency("Charged $19.99") == "USD"


def test_to_naive_utc_normalizes_offsets():
    aware = datetime(2026, 8, 2, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    naive = to_naive_utc(aware)
    assert naive.tzinfo is None
    assert naive == datetime(2026, 8, 2, 5, 0)
    # Already-naive values pass through untouched.
    assert to_naive_utc(datetime(2026, 8, 2, 5, 0)) == datetime(2026, 8, 2, 5, 0)
