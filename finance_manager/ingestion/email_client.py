"""IMAP email ingestion for bank alert emails.

Bank emails are richer and better dated than SMS: the ``Date`` header gives the
real transaction time instead of the ingestion time, and ``Message-ID`` gives a
natural idempotency key, so re-running a fetch does not duplicate rows.

The module is split so the parsing half is testable without a mail server:

    fetch_emails()          talks to IMAP and returns EmailMessage objects
    parse_email_message()   turns a raw RFC822 message into an EmailMessage
    email_to_transaction()  turns an EmailMessage into a transaction dict

Connections are opened read-only, so ingestion never marks mail as seen or
mutates the user's mailbox.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from finance_manager.config import Settings
from finance_manager.ingestion.parsing import (
    normalize_merchant,
    parse_direction,
    parse_merchant,
    parse_money,
    to_naive_utc,
)
from finance_manager.logger import logger
from finance_manager.schemas import EmailMessage


_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_BREAK = re.compile(r"(?i)<(?:br|/p|/div|/tr|/h[1-6])[^>]*>")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_EMAIL_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class EmailIngestionError(RuntimeError):
    """Raised when the mailbox cannot be reached or credentials are missing."""


# ---------------------------
# Message parsing
# ---------------------------


def _decode_header_value(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def strip_html(html: str) -> str:
    """Reduce an HTML email body to readable plain text.

    Uses BeautifulSoup when available and falls back to a regex strip so email
    ingestion still works in a slim install without bs4.
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup  # type: ignore

        text = BeautifulSoup(html, "html.parser").get_text("\n")
    except Exception:
        text = _HTML_BREAK.sub("\n", html)
        text = _HTML_TAG.sub(" ", text)
        text = (
            text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
        )
    text = _WHITESPACE.sub(" ", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def _extract_body(message: Message) -> str:
    """Return the best plain-text rendering of a possibly multipart email."""
    plain_parts: List[str] = []
    html_parts: List[str] = []

    def decode(part: Message) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            raw = part.get_payload()
            return raw if isinstance(raw, str) else ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return payload.decode("utf-8", errors="replace")

    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(decode(part))
            elif content_type == "text/html":
                html_parts.append(decode(part))
    else:
        if message.get_content_type() == "text/html":
            html_parts.append(decode(message))
        else:
            plain_parts.append(decode(message))

    if plain_parts:
        return "\n".join(p.strip() for p in plain_parts if p.strip()).strip()
    return strip_html("\n".join(html_parts))


def _fallback_message_id(sender: str, subject: str, received: str) -> str:
    digest = hashlib.sha256(f"{sender}|{subject}|{received}".encode("utf-8"))
    return f"<generated-{digest.hexdigest()[:32]}>"


def parse_email_message(raw: bytes) -> EmailMessage:
    """Parse raw RFC822 bytes into an EmailMessage with a naive-UTC timestamp."""
    message = email.message_from_bytes(raw)
    sender = _decode_header_value(message.get("From"))
    subject = _decode_header_value(message.get("Subject"))
    date_header = message.get("Date") or ""

    received_at: Optional[datetime] = None
    if date_header:
        try:
            received_at = to_naive_utc(parsedate_to_datetime(date_header))
        except (TypeError, ValueError, IndexError):
            received_at = None
    if received_at is None:
        received_at = datetime.utcnow()

    message_id = _decode_header_value(message.get("Message-ID")) or _fallback_message_id(
        sender, subject, date_header
    )
    return EmailMessage(
        message_id=message_id,
        sender=sender,
        subject=subject,
        received_at=received_at,
        body=_extract_body(message),
    )


def sender_allowed(sender: str, allowed: Optional[Sequence[str]]) -> bool:
    """Check a From header against an allowlist of addresses or domains.

    An empty or unset allowlist accepts everything, which keeps the feature
    usable before the user has curated their bank sender list.
    """
    if not allowed:
        return True
    lowered = (sender or "").lower()
    return any(entry.strip().lower() in lowered for entry in allowed if entry.strip())


# ---------------------------
# Transaction mapping
# ---------------------------


def _merchant_from_email(msg: EmailMessage, body_text: str) -> str:
    """Best-effort merchant label: message body, then subject, then sender domain."""
    merchant = parse_merchant(body_text) or parse_merchant(msg.subject)
    if merchant:
        return merchant
    if msg.subject:
        return msg.subject[:80]
    address = _EMAIL_ADDRESS.search(msg.sender or "")
    if address:
        return address.group(0).split("@")[-1]
    return (msg.sender or "Unknown sender")[:80]


def email_to_transaction(
    msg: EmailMessage, user_id: str, default_currency: str = "USD"
) -> Optional[Dict[str, Any]]:
    """Convert a bank email into a transaction dict, or None if it is not one.

    Credits (refunds, reversals, salary) are stored as negative amounts so that
    spend aggregates net them out instead of counting a refund as expenditure.
    """
    text = f"{msg.subject}\n{msg.body}".strip()
    money = parse_money(text)
    if money is None:
        return None

    direction = parse_direction(text)
    amount = abs(money.amount)
    if direction == "credit":
        amount = -amount

    merchant = _merchant_from_email(msg, msg.body or "")
    return {
        "user_id": user_id,
        "timestamp": to_naive_utc(msg.received_at),
        "amount": amount,
        "currency": money.currency or default_currency,
        "merchant_name_raw": merchant[:80],
        "merchant_normalized": normalize_merchant(merchant) or None,
        "source": "email",
        "description": (msg.subject or text)[:200],
        # Message-ID makes re-fetching the same mailbox idempotent.
        "source_doc_id": msg.message_id,
        "tags": [direction, "email"],
    }


def emails_to_transactions(
    messages: Iterable[EmailMessage], user_id: str, default_currency: str = "USD"
) -> List[Dict[str, Any]]:
    """Map a batch of emails to transaction dicts, skipping non-transactional mail."""
    parsed: List[Dict[str, Any]] = []
    for msg in messages:
        tx = email_to_transaction(msg, user_id=user_id, default_currency=default_currency)
        if tx is not None:
            parsed.append(tx)
    return parsed


# ---------------------------
# IMAP fetching
# ---------------------------


def _imap_date(value: datetime) -> str:
    # IMAP SINCE expects the DD-Mon-YYYY form, e.g. 02-Aug-2026.
    return value.strftime("%d-%b-%Y")


def _connect(settings: Settings) -> imaplib.IMAP4:
    if not settings.imap_host or not settings.imap_username or not settings.imap_password:
        raise EmailIngestionError(
            "IMAP is not configured. Set IMAP_HOST, IMAP_USERNAME and IMAP_PASSWORD."
        )
    factory = imaplib.IMAP4_SSL if settings.imap_use_ssl else imaplib.IMAP4
    try:
        conn = factory(settings.imap_host, settings.imap_port)
        conn.login(settings.imap_username, settings.imap_password)
    except Exception as err:
        raise EmailIngestionError(f"IMAP connection failed: {err}") from err
    return conn


def fetch_emails(
    settings: Settings,
    since_days: Optional[int] = None,
    limit: Optional[int] = None,
    folder: Optional[str] = None,
) -> List[EmailMessage]:
    """Fetch recent messages from the configured mailbox.

    This is a blocking call. The graph node wraps it in a worker thread so the
    async event loop is not stalled while the mailbox is read.
    """
    since_days = since_days if since_days is not None else settings.email_since_days
    limit = limit if limit is not None else settings.email_fetch_limit
    folder = folder or settings.imap_folder

    conn = _connect(settings)
    messages: List[EmailMessage] = []
    try:
        # readonly keeps ingestion non-destructive: nothing gets marked as seen.
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            raise EmailIngestionError(f"Cannot open IMAP folder '{folder}'")

        since = _imap_date(datetime.utcnow() - timedelta(days=max(since_days, 0)))
        status, data = conn.search(None, "SINCE", since)
        if status != "OK":
            raise EmailIngestionError("IMAP search failed")

        uids = (data[0] or b"").split()
        # Newest last in IMAP ordering, so take from the tail.
        if limit and len(uids) > limit:
            uids = uids[-limit:]

        for uid in reversed(uids):
            status, payload = conn.fetch(uid, "(RFC822)")
            if status != "OK" or not payload:
                continue
            raw = next(
                (item[1] for item in payload if isinstance(item, tuple) and len(item) > 1),
                None,
            )
            if not raw:
                continue
            try:
                parsed = parse_email_message(raw)
            except Exception as err:
                logger.warning("email_parse_failed", uid=uid.decode(errors="ignore"), error=str(err))
                continue
            if not sender_allowed(parsed.sender, settings.email_allowed_senders):
                continue
            messages.append(parsed)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass
    return messages
