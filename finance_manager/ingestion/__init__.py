"""Ingestion sources and shared parsing helpers.

Modules:
    parsing       Text parsing shared by SMS and email ingestion.
    email_client  IMAP fetching and bank-email to transaction parsing.
"""

from finance_manager.ingestion.parsing import (
    ParsedMoney,
    normalize_merchant,
    parse_direction,
    parse_merchant,
    parse_money,
    to_naive_utc,
)

__all__ = [
    "ParsedMoney",
    "normalize_merchant",
    "parse_direction",
    "parse_merchant",
    "parse_money",
    "to_naive_utc",
]
