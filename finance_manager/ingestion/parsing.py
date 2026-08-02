"""Text parsing helpers shared by the SMS and email ingestion paths.

The goal here is to pull a trustworthy amount, currency, direction and merchant
out of a short bank notification. Bank messages are noisy: they contain masked
card numbers, reference ids, dates and balances, all of which look like numbers
to a naive regex. The parser therefore sanitizes those spans first and only then
looks for a currency-adjacent or keyword-adjacent amount.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel


# ---------------------------
# Currency handling
# ---------------------------

CURRENCY_CODES = {
    "USD",
    "EUR",
    "GBP",
    "LKR",
    "INR",
    "AED",
    "AUD",
    "CAD",
    "SGD",
    "JPY",
}

# "Rs" is ambiguous across LKR and INR. The project's default bank senders are
# Sri Lankan, so it resolves to LKR unless an explicit code appears in the text.
_SYMBOL_TO_CODE = {
    "$": "USD",
    "us$": "USD",
    "€": "EUR",
    "£": "GBP",
    "₹": "INR",
    "rs": "LKR",
    "rs.": "LKR",
    "lkr": "LKR",
}


class ParsedMoney(BaseModel):
    """An amount plus the currency it was found next to, if any."""

    amount: float
    currency: Optional[str] = None
    # How the amount was located, useful for debugging low-quality parses.
    matched_by: str = "unknown"


# Self-contained group: this fragment gets concatenated into larger patterns, so
# its internal alternation must not escape and swallow the surrounding pattern.
_NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_CURRENCY_TOKEN = (
    r"(?:USD|EUR|GBP|LKR|INR|AED|AUD|CAD|SGD|JPY|US\$|Rs\.?|\$|€|£|₹)"
)

_AMOUNT_AFTER_CURRENCY = re.compile(
    r"(" + _CURRENCY_TOKEN + r")\s*(" + _NUM + r")", re.IGNORECASE
)
_AMOUNT_BEFORE_CURRENCY = re.compile(
    r"(" + _NUM + r")\s*(" + _CURRENCY_TOKEN + r")", re.IGNORECASE
)
_AMOUNT_BY_KEYWORD = re.compile(
    r"(?:amount|amt|debited|credited|charged|spent|paid|payment\s+of|purchase\s+of|"
    r"transfer\s+of|withdrawal\s+of|total|value)\W{0,14}?(" + _NUM + r")",
    re.IGNORECASE,
)

# Spans that contain digits which are never the transaction amount.
_MASKED_TOKEN = re.compile(r"(?:x{2,}|\*{2,}|#{2,})[\s\-]*\d{2,6}", re.IGNORECASE)
_CARD_CONTEXT = re.compile(
    r"(?:card|acct|a/c|account|ending(?:\s+in)?|ref(?:erence)?|txn|trace|otp|pin)"
    r"\s*(?:no\.?|number|id)?\s*[:#]?\s*[x*#\d][x*#\d\-]{1,}",
    re.IGNORECASE,
)
_DATE_LIKE = re.compile(r"\b\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}\b")
_TIME_LIKE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.IGNORECASE)
# A running balance is not a spend amount.
_BALANCE_CLAUSE = re.compile(
    r"(?:available\s+balance|avl\s*bal|bal(?:ance)?)\W{0,14}?"
    r"(?:" + _CURRENCY_TOKEN + r")?\s*" + _NUM,
    re.IGNORECASE,
)

_CREDIT_WORDS = (
    "credited",
    "credit",
    "refund",
    "refunded",
    "reversal",
    "reversed",
    "received",
    "deposit",
    "deposited",
    "salary",
    "cashback",
)
_DEBIT_WORDS = (
    "debited",
    "debit",
    "spent",
    "charged",
    "purchase",
    "paid",
    "payment",
    "withdrawn",
    "withdrawal",
    "transferred",
)


def _sanitize_for_amount(text: str) -> str:
    """Blank out spans whose digits are never the transaction amount."""
    cleaned = text
    for pattern in (
        _BALANCE_CLAUSE,
        _MASKED_TOKEN,
        _CARD_CONTEXT,
        _DATE_LIKE,
        _TIME_LIKE,
    ):
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _code_for_token(token: str) -> Optional[str]:
    key = token.strip().lower()
    if key.upper() in CURRENCY_CODES:
        return key.upper()
    return _SYMBOL_TO_CODE.get(key) or _SYMBOL_TO_CODE.get(key.rstrip("."))


def parse_currency(text: str) -> Optional[str]:
    """Return the first explicit currency code or symbol found in the text."""
    match = re.search(r"\b(" + "|".join(sorted(CURRENCY_CODES)) + r")\b", text.upper())
    if match:
        return match.group(1)
    symbol = re.search(_CURRENCY_TOKEN, text, re.IGNORECASE)
    if symbol:
        return _code_for_token(symbol.group(0))
    return None


def parse_money(text: str) -> Optional[ParsedMoney]:
    """Extract the transaction amount and its currency from a bank message.

    Resolution order, most trustworthy first:
      1. a number directly attached to a currency token ("LKR 4,500.00")
      2. a number followed by a currency token ("4,500.00 LKR")
      3. a number introduced by a transaction keyword ("debited with 4500")
      4. the largest two-decimal number left in the sanitized text
    """
    if not text:
        return None
    cleaned = _sanitize_for_amount(text)

    match = _AMOUNT_AFTER_CURRENCY.search(cleaned)
    if match:
        amount = _to_float(match.group(2))
        if amount is not None:
            return ParsedMoney(
                amount=amount,
                currency=_code_for_token(match.group(1)),
                matched_by="currency_prefix",
            )

    match = _AMOUNT_BEFORE_CURRENCY.search(cleaned)
    if match:
        amount = _to_float(match.group(1))
        if amount is not None:
            return ParsedMoney(
                amount=amount,
                currency=_code_for_token(match.group(2)),
                matched_by="currency_suffix",
            )

    match = _AMOUNT_BY_KEYWORD.search(cleaned)
    if match:
        amount = _to_float(match.group(1))
        if amount is not None:
            return ParsedMoney(
                amount=amount, currency=parse_currency(text), matched_by="keyword"
            )

    # Last resort: the largest decimal-looking number that survived sanitizing.
    decimals = [
        value
        for value in (_to_float(m.group(0)) for m in re.finditer(_NUM, cleaned))
        if value is not None and value > 0
    ]
    if decimals:
        return ParsedMoney(
            amount=max(decimals), currency=parse_currency(text), matched_by="fallback"
        )
    return None


def parse_direction(text: str) -> str:
    """Classify a message as a 'debit' (money out) or 'credit' (money in).

    Credit wording is checked first because refund and reversal messages often
    also contain debit vocabulary describing the original purchase.
    """
    lowered = (text or "").lower()
    if any(word in lowered for word in _CREDIT_WORDS):
        return "credit"
    if any(word in lowered for word in _DEBIT_WORDS):
        return "debit"
    return "debit"


# A period is kept only when it sits inside a token, so "NETFLIX.COM" survives
# while the sentence-ending period in "at WHOLE FOODS. Balance ..." terminates
# the match.
_MERCHANT_BODY = r"((?:[^\n;,.]|\.(?!\s|$)){2,80})"
_MERCHANT_PATTERNS = (
    re.compile(r"\bat\s+" + _MERCHANT_BODY, re.IGNORECASE),
    re.compile(r"\bto\s+" + _MERCHANT_BODY, re.IGNORECASE),
    re.compile(r"\bfrom\s+" + _MERCHANT_BODY, re.IGNORECASE),
)
# Words that mark the end of a merchant name inside a longer sentence.
_MERCHANT_STOPWORDS = (
    " on ",
    " for ",
    " ref ",
    " dated ",
    " using ",
    " with ",
    " your ",
    " available ",
    " bal ",
    " balance ",
    " card ",
    " account ",
)


def _trim_merchant(candidate: str) -> str:
    text = " " + candidate.strip() + " "
    cut = len(text)
    for stop in _MERCHANT_STOPWORDS:
        idx = text.lower().find(stop)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].strip(" -:")


def parse_merchant(text: str) -> Optional[str]:
    """Pull a merchant name out of a bank message, if one is recognizable."""
    if not text:
        return None
    for pattern in _MERCHANT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        candidate = _trim_merchant(match.group(1))
        # Reject captures that are just numbers or a stray article.
        if len(candidate) >= 2 and re.search(r"[A-Za-z]{2,}", candidate):
            return candidate[:80]
    return None


_MERCHANT_NOISE = re.compile(
    r"\b(pvt|private|ltd|limited|inc|llc|plc|corp|corporation|co|www|com|net)\b",
    re.IGNORECASE,
)


def normalize_merchant(name: Optional[str]) -> str:
    """Reduce a merchant string to a stable grouping key.

    Strips punctuation, digits, legal suffixes and casing so that
    "NETFLIX.COM 4567" and "Netflix com" collapse to the same key. This key is
    what recurring-charge detection groups on.
    """
    if not name:
        return ""
    text = name.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [tok for tok in text.split() if not tok.isdigit()]
    tokens = [tok for tok in tokens if not _MERCHANT_NOISE.fullmatch(tok)]
    return " ".join(tokens).strip()


def to_naive_utc(value: datetime) -> datetime:
    """Normalize a datetime to naive UTC.

    Email headers carry timezone-aware datetimes while SMS parsing produces
    naive ones. Mixing the two raises TypeError on comparison, which would break
    sorting and date filtering, so every ingested timestamp goes through here.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def split_lines(text: str) -> List[str]:
    """Split a block of text into non-empty, stripped lines."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]
