"""Notification tests — N-01 through N-03.

Tests email subject lines, body generation, and SMTP failure handling.
All SMTP calls are mocked.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, call, patch

import pytest

from src.models import PaymentStatus, PropertyResult, TransactionRecord
from src.notifier import (
    _build_subject,
    _compute_summary_line,
    _fallback_body,
    send_notification,
)
from tests.conftest import decode_mime_body, make_cfg_mock, make_txn


RUN_DATE = date(2026, 3, 1)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paid_result(name: str, amount: float, category: str) -> PropertyResult:
    return PropertyResult(
        property_name=name,
        status=PaymentStatus.PAID_ON_TIME,
        matched_transaction=make_txn(
            txn_date=RUN_DATE,
            description=f"Zelle From {name} tenant",
            amount=amount,
            category=category,
        ),
        notes=f"Received {RUN_DATE} — on time (deadline 2026-03-06).",
        step_resolved_by=1,
    )


def _missing_result(name: str) -> PropertyResult:
    return PropertyResult(
        property_name=name,
        status=PaymentStatus.MISSING,
        matched_transaction=None,
        notes="No match found after all three steps.",
        step_resolved_by=3,
    )


def _late_result(name: str, amount: float, category: str) -> PropertyResult:
    return PropertyResult(
        property_name=name,
        status=PaymentStatus.PAID_LATE,
        matched_transaction=make_txn(
            txn_date=date(2026, 3, 12),  # past grace deadline
            description=f"Zelle From {name} tenant",
            amount=amount,
            category=category,
        ),
        notes="Received 2026-03-12 — LATE (deadline was 2026-03-06).",
        step_resolved_by=1,
    )


def _review_result(name: str, amount: float) -> PropertyResult:
    return PropertyResult(
        property_name=name,
        status=PaymentStatus.REVIEW_NEEDED,
        matched_transaction=make_txn(amount=amount, category="Uncategorized"),
        notes="LLM-suggested match (confidence: medium). Rationale: Payment amount matches. HUMAN REVIEW REQUIRED.",
        step_resolved_by=3,
    )


def _make_notifier_cfg() -> MagicMock:
    cfg = make_cfg_mock()
    cfg.gmail_sender = "sender@example.com"
    cfg.gmail_password = "test-app-password"
    cfg.gmail_recipient = "recv@example.com"
    cfg.ollama_endpoint = "http://localhost:11434"
    cfg.ollama_model = "qwen3:8b"
    return cfg


# ---------------------------------------------------------------------------
# N-01: All paid on time
# ---------------------------------------------------------------------------


@patch("smtplib.SMTP")
def test_n01_all_paid_email_sent_with_all_received_subject(mock_smtp):
    """N-01: All 3 properties paid → subject 'All Received'; body contains amounts/dates."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _paid_result("Calmar", 3100.00, "Rental Income (Calmar)"),
        _paid_result("505", 2800.00, "Rental Income (505)"),
    ]
    cfg = _make_notifier_cfg()

    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is True
    # Verify subject line via _build_subject helper
    subject = _build_subject(results, RUN_DATE, cfg.email_subject_prefix)
    assert "All Received" in subject
    assert "ACTION NEEDED" not in subject

    # Verify SMTP was called
    mock_smtp.assert_called_once()


def test_n01_subject_all_received_for_paid_results():
    """N-01 (unit): _build_subject returns 'All Received' when all properties paid."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _paid_result("Calmar", 3100.00, "Rental Income (Calmar)"),
    ]
    subject = _build_subject(results, RUN_DATE, "[Agent - Rent Check]")
    assert "All Received" in subject
    assert "ACTION NEEDED" not in subject


# ---------------------------------------------------------------------------
# N-02: Mixed results (paid + missing + flagged)
# ---------------------------------------------------------------------------


@patch("smtplib.SMTP")
def test_n02_mixed_results_action_needed_subject(mock_smtp):
    """N-02: Mixed statuses (paid + missing) → subject 'ACTION NEEDED'; email sent."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
        _review_result("505", 2800.00),
    ]
    cfg = _make_notifier_cfg()

    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is True
    subject = _build_subject(results, RUN_DATE, cfg.email_subject_prefix)
    assert "ACTION NEEDED" in subject
    mock_smtp.assert_called_once()


def test_n02_subject_action_needed_for_missing_result():
    """N-02 (unit): _build_subject returns 'ACTION NEEDED' when a property is MISSING."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
    ]
    subject = _build_subject(results, RUN_DATE, "[Agent - Rent Check]")
    assert "ACTION NEEDED" in subject


def test_subject_late_payment_when_only_paid_late():
    """PAID_LATE with no missing properties → subject 'LATE PAYMENT', not 'ACTION NEEDED'."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _late_result("Calmar", 3100.00, "Rental Income (Calmar)"),
        _paid_result("505", 2800.00, "Rental Income (505)"),
    ]
    subject = _build_subject(results, RUN_DATE, "[Agent - Rent Check]")
    assert "LATE PAYMENT" in subject
    assert "ACTION NEEDED" not in subject
    assert "All Received" not in subject


def test_subject_action_needed_takes_priority_over_late():
    """A missing property overrides a late property → ACTION NEEDED, not LATE PAYMENT."""
    results = [
        _late_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
    ]
    subject = _build_subject(results, RUN_DATE, "[Agent - Rent Check]")
    assert "ACTION NEEDED" in subject
    assert "LATE PAYMENT" not in subject


def test_subject_all_received_when_all_on_time():
    """All properties paid on time → 'All Received', no ACTION NEEDED or LATE PAYMENT."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _paid_result("Calmar", 3100.00, "Rental Income (Calmar)"),
        _paid_result("505", 2800.00, "Rental Income (505)"),
    ]
    subject = _build_subject(results, RUN_DATE, "[Agent - Rent Check]")
    assert "All Received" in subject
    assert "ACTION NEEDED" not in subject
    assert "LATE PAYMENT" not in subject


def test_subject_review_needed_when_only_review_needed():
    """REVIEW_NEEDED with no missing properties → subject 'REVIEW NEEDED', not 'ACTION NEEDED'."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _review_result("Calmar", 3100.00),
        _paid_result("505", 2800.00, "Rental Income (505)"),
    ]
    subject = _build_subject(results, RUN_DATE, "[Agent - Rent Check]")
    assert "REVIEW NEEDED" in subject
    assert "ACTION NEEDED" not in subject
    assert "All Received" not in subject


def test_subject_action_needed_takes_priority_over_review_needed():
    """A missing property overrides a review-needed property → ACTION NEEDED."""
    results = [
        _review_result("Links Lane", 2950.00),
        _missing_result("Calmar"),
    ]
    subject = _build_subject(results, RUN_DATE, "[Agent - Rent Check]")
    assert "ACTION NEEDED" in subject
    assert "REVIEW NEEDED" not in subject


def test_n02_fallback_body_separates_paid_and_missing():
    """N-02 (unit): Fallback body lists paid and missing properties; summary line reflects facts."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
    ]
    cfg = _make_notifier_cfg()
    body = _fallback_body(results, RUN_DATE, cfg, error_message=None)

    assert "Links Lane" in body
    assert "Calmar" in body
    assert "not yet received" in body  # from _compute_summary_line
    assert "MISSING" in body


# ---------------------------------------------------------------------------
# N-03: SMTP failure → send_notification returns False, no exception raised
# ---------------------------------------------------------------------------


@patch("smtplib.SMTP")
def test_n03_smtp_failure_returns_false_no_exception(mock_smtp):
    """N-03: SMTP failure → send_notification returns False; no exception propagated."""
    import smtplib

    mock_smtp.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"Authentication credentials invalid"
    )

    results = [_paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)")]
    cfg = _make_notifier_cfg()

    # Must not raise
    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is False


@patch("smtplib.SMTP")
def test_n03_smtp_failure_os_error_returns_false(mock_smtp):
    """N-03: Network-level SMTP failure → returns False."""
    mock_smtp.return_value.__enter__.side_effect = OSError("Connection refused")

    results = [_missing_result("Links Lane")]
    cfg = _make_notifier_cfg()

    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is False


# ---------------------------------------------------------------------------
# Fallback body unit tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _compute_summary_line unit tests
# ---------------------------------------------------------------------------


def test_compute_summary_line_all_on_time():
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _paid_result("Calmar", 3100.00, "Rental Income (Calmar)"),
        _paid_result("505", 2800.00, "Rental Income (505)"),
    ]
    assert _compute_summary_line(results) == "All 3 rent payments received on time."


def test_compute_summary_line_some_late_no_missing():
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _late_result("Calmar", 3100.00, "Rental Income (Calmar)"),
        _paid_result("505", 2800.00, "Rental Income (505)"),
    ]
    line = _compute_summary_line(results)
    assert line == "2 of 3 payments received on time. 1 received late."


def test_compute_summary_line_some_missing():
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
        _missing_result("505"),
    ]
    line = _compute_summary_line(results)
    assert line == "1 of 3 payments received. 2 not yet received."


def test_compute_summary_line_missing_takes_priority_over_late():
    """When both missing and late are present, use the missing formula."""
    results = [
        _late_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
    ]
    line = _compute_summary_line(results)
    assert "not yet received" in line
    assert "received late" not in line


def test_compute_summary_line_some_review_needed_no_missing():
    """One REVIEW_NEEDED, two PAID_ON_TIME → '2 of 3 confirmed. 1 require review.'"""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _paid_result("505", 2800.00, "Rental Income (505)"),
        _review_result("Calmar", 3100.00),
    ]
    line = _compute_summary_line(results)
    assert line == "2 of 3 payments confirmed. 1 require review."


# ---------------------------------------------------------------------------
# HTML highlighting in fallback body
# ---------------------------------------------------------------------------


def test_fallback_body_late_payment_has_yellow_highlight():
    results = [
        _late_result("Calmar", 3100.00, "Rental Income (Calmar)"),
    ]
    cfg = _make_notifier_cfg()
    body = _fallback_body(results, RUN_DATE, cfg, error_message=None)
    assert "#FFF9C4" in body
    assert "#EF5350" not in body


def test_fallback_body_missing_has_red_highlight():
    results = [
        _missing_result("Calmar"),
    ]
    cfg = _make_notifier_cfg()
    body = _fallback_body(results, RUN_DATE, cfg, error_message=None)
    assert "#EF5350" in body
    assert "#FFF9C4" not in body


def test_fallback_body_review_needed_has_orange_highlight():
    results = [
        _review_result("Calmar", 3100.00),
    ]
    cfg = _make_notifier_cfg()
    body = _fallback_body(results, RUN_DATE, cfg, error_message=None)
    assert "#FFE0B2" in body
    assert "#EF5350" not in body


def test_fallback_body_on_time_has_green_highlight():
    """Paid-on-time status has green highlight; no red, yellow, or orange."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
    ]
    cfg = _make_notifier_cfg()
    body = _fallback_body(results, RUN_DATE, cfg, error_message=None)
    assert "#C8E6C9" in body
    assert "#FFF9C4" not in body
    assert "#EF5350" not in body


def test_fallback_body_highlights_only_status_not_name():
    """The highlighted <span> must close before the amount detail; name is plain bold."""
    results = [_late_result("Calmar", 3100.00, "Rental Income (Calmar)")]
    cfg = _make_notifier_cfg()
    body = _fallback_body(results, RUN_DATE, cfg, error_message=None)
    # The highlight span must close (</span>) before the amount detail.
    highlight_end = body.find("</span>")
    amount_start = body.find("$3,100.00")
    assert highlight_end != -1
    assert amount_start != -1
    assert highlight_end < amount_start
    # Property name should NOT be inside a highlight span
    calmar_pos = body.find("Calmar")
    first_span_open = body.find('<span style=')
    assert calmar_pos < first_span_open  # name appears before any highlight span


def test_fallback_body_date_and_amount_format():
    """Amounts use comma formatting and dates use M/D with deadline."""
    # PROP_CALMAR: due_day=1, grace=5 → deadline = 2026-03-06 → "3/6"
    # _late_result uses txn_date=2026-03-12 → "3/12"
    results = [_late_result("Calmar", 3100.00, "Rental Income (Calmar)")]
    cfg = _make_notifier_cfg()
    body = _fallback_body(results, RUN_DATE, cfg, error_message=None)
    assert "$3,100.00" in body
    assert "Received: 3/12" in body
    assert "Deadline: 3/6" in body
    # ISO date format must NOT appear
    assert "2026-03-12" not in body
