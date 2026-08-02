"""Tests for IMAP email parsing and email-to-transaction mapping."""

from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage as RawEmail

from finance_manager.db import InMemoryRepository
from finance_manager.ingestion.email_client import (
    email_to_transaction,
    emails_to_transactions,
    parse_email_message,
    sender_allowed,
    strip_html,
)
from finance_manager.schemas import EmailMessage, Transaction


def build_raw(
    subject: str,
    body: str,
    sender: str = "alerts@mybank.com",
    date: str = "Sun, 02 Aug 2026 10:30:00 +0530",
    message_id: str = "<abc123@mybank.com>",
    html: bool = False,
) -> bytes:
    msg = RawEmail()
    msg["From"] = sender
    msg["To"] = "user@example.com"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id
    if html:
        msg.set_content("plain text fallback")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


DEBIT_BODY = (
    "Dear customer,\n"
    "Your card ending 4532 was debited USD 42.75 at WHOLE FOODS on 02/08/2026.\n"
    "Available balance USD 1,204.10\n"
)


def test_parses_headers_and_converts_date_to_naive_utc():
    msg = parse_email_message(build_raw("Transaction alert", DEBIT_BODY))

    assert msg.sender == "alerts@mybank.com"
    assert msg.subject == "Transaction alert"
    assert msg.message_id == "<abc123@mybank.com>"
    # +0530 becomes 05:00 UTC, with the offset dropped.
    assert msg.received_at == datetime(2026, 8, 2, 5, 0)
    assert msg.received_at.tzinfo is None
    assert "WHOLE FOODS" in msg.body


def test_parses_encoded_subject():
    raw = build_raw("=?utf-8?q?Payment_confirmation?=", DEBIT_BODY)
    assert parse_email_message(raw).subject == "Payment confirmation"


def test_extracts_text_from_html_only_body():
    html = "<html><body><p>Debited <b>USD 15.00</b> at CAFE ROMA</p></body></html>"
    text = strip_html(html)
    assert "USD 15.00" in text
    assert "<b>" not in text


def test_generates_message_id_when_header_is_missing():
    msg = RawEmail()
    msg["From"] = "alerts@mybank.com"
    msg["Subject"] = "Alert"
    msg["Date"] = "Sun, 02 Aug 2026 10:30:00 +0000"
    msg.set_content(DEBIT_BODY)

    parsed = parse_email_message(msg.as_bytes())
    assert parsed.message_id.startswith("<generated-")


def test_maps_debit_email_to_transaction():
    msg = parse_email_message(build_raw("Transaction alert", DEBIT_BODY))
    tx = email_to_transaction(msg, user_id="u1")

    assert tx is not None
    assert tx["amount"] == 42.75
    assert tx["currency"] == "USD"
    assert tx["merchant_name_raw"] == "WHOLE FOODS"
    assert tx["merchant_normalized"] == "whole foods"
    assert tx["source"] == "email"
    assert tx["source_doc_id"] == "<abc123@mybank.com>"
    assert tx["timestamp"] == datetime(2026, 8, 2, 5, 0)
    assert "debit" in tx["tags"]
    # Constructing the model proves the dict satisfies the schema.
    assert Transaction(**tx).amount == 42.75


def test_credit_email_becomes_a_negative_amount():
    body = "A refund of USD 30.00 from ASOS has been credited to your account."
    msg = parse_email_message(build_raw("Refund processed", body))
    tx = email_to_transaction(msg, user_id="u1")

    assert tx is not None
    assert tx["amount"] == -30.00
    assert "credit" in tx["tags"]


def test_non_transactional_email_is_skipped():
    msg = parse_email_message(
        build_raw("Your e-statement is ready", "Log in to view your statement.")
    )
    assert email_to_transaction(msg, user_id="u1") is None


def test_falls_back_to_subject_when_no_merchant_named():
    msg = parse_email_message(
        build_raw("Charged USD 9.99", "Your subscription was charged USD 9.99.")
    )
    tx = email_to_transaction(msg, user_id="u1")
    assert tx is not None
    assert tx["merchant_name_raw"] == "Charged USD 9.99"


def test_falls_back_to_sender_domain_when_subject_is_empty():
    msg = EmailMessage(
        message_id="<x@y>",
        sender="Alerts <alerts@mybank.com>",
        subject="",
        received_at=datetime(2026, 8, 2, 5, 0),
        body="Your subscription was charged USD 9.99.",
    )
    tx = email_to_transaction(msg, user_id="u1")
    assert tx is not None
    assert tx["merchant_name_raw"] == "mybank.com"


def test_batch_mapping_skips_non_transactions():
    messages = [
        parse_email_message(build_raw("Alert", DEBIT_BODY, message_id="<a@b>")),
        parse_email_message(build_raw("Newsletter", "Market news roundup", message_id="<c@d>")),
    ]
    assert len(emails_to_transactions(messages, user_id="u1")) == 1


def test_sender_allowlist_matches_address_and_domain():
    assert sender_allowed("Bank <alerts@mybank.com>", None) is True
    assert sender_allowed("Bank <alerts@mybank.com>", []) is True
    assert sender_allowed("Bank <alerts@mybank.com>", ["mybank.com"]) is True
    assert sender_allowed("Bank <alerts@mybank.com>", ["alerts@mybank.com"]) is True
    assert sender_allowed("Spam <deals@shop.io>", ["mybank.com"]) is False


async def test_refetching_the_same_mailbox_is_idempotent():
    repo = InMemoryRepository()
    msg = parse_email_message(build_raw("Transaction alert", DEBIT_BODY))

    first = await repo.upsert_transactions(
        [Transaction(**email_to_transaction(msg, user_id="u1"))]
    )
    # A second fetch returns the identical message, Message-ID and all.
    second = await repo.upsert_transactions(
        [Transaction(**email_to_transaction(msg, user_id="u1"))]
    )

    stored = await repo.list_transactions("u1")
    assert len(stored) == 1
    # The stored id is reused so references from earlier responses stay valid.
    assert first[0].id == second[0].id == stored[0].id


async def test_distinct_emails_are_stored_separately():
    repo = InMemoryRepository()
    messages = [
        parse_email_message(build_raw("Alert", DEBIT_BODY, message_id="<one@b>")),
        parse_email_message(
            build_raw(
                "Alert",
                "Your card was debited USD 11.00 at CAFE ROMA on 03/08/2026.",
                date="Mon, 03 Aug 2026 09:00:00 +0000",
                message_id="<two@b>",
            )
        ),
    ]
    await repo.upsert_transactions(
        [Transaction(**tx) for tx in emails_to_transactions(messages, user_id="u1")]
    )
    assert len(await repo.list_transactions("u1")) == 2
