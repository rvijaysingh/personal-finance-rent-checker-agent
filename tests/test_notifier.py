"""Notification tests — N-01 through N-04.

Tests email subject lines, body generation, SMTP failure handling, and the
LLM-unavailable fallback path. All SMTP and Ollama calls are mocked.
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


def _possible_result(name: str, amount: float) -> PropertyResult:
    return PropertyResult(
        property_name=name,
        status=PaymentStatus.POSSIBLE_MATCH,
        matched_transaction=make_txn(amount=amount, category="Transfer"),
        notes="Amount matches but category wrong. MANUAL REVIEW RECOMMENDED.",
        step_resolved_by=2,
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


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_n01_all_paid_email_sent_with_all_received_subject(mock_smtp, mock_ollama):
    """N-01: All 3 properties paid → subject 'All Received'; body contains amounts/dates."""
    mock_ollama.return_value = (
        "<p>All payments received.</p>"
        "<ul>"
        "<li>Links Lane: $2,950.00 on 2026-03-01</li>"
        "<li>Calmar: $3,100.00 on 2026-03-01</li>"
        "<li>505: $2,800.00 on 2026-03-01</li>"
        "</ul>"
    )

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


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_n02_mixed_results_action_needed_subject(mock_smtp, mock_ollama):
    """N-02: Mixed statuses → subject 'ACTION NEEDED'; email sent."""
    mock_ollama.return_value = "<p>ACTION NEEDED: Calmar and 505 require attention.</p>"

    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
        _possible_result("505", 2800.00),
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


def test_n02_fallback_body_separates_paid_and_missing():
    """N-02 (unit): Fallback body lists paid and missing properties; summary line reflects facts."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
    ]
    body = _fallback_body(results, RUN_DATE, llm_unavailable=False, error_message=None)

    assert "Links Lane" in body
    assert "Calmar" in body
    assert "not yet received" in body  # from _compute_summary_line
    assert "MISSING" in body


# ---------------------------------------------------------------------------
# N-03: SMTP failure → send_notification returns False, no exception raised
# ---------------------------------------------------------------------------


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_n03_smtp_failure_returns_false_no_exception(mock_smtp, mock_ollama):
    """N-03: SMTP failure → send_notification returns False; no exception propagated."""
    import smtplib

    mock_ollama.return_value = "<p>Results here.</p>"
    mock_smtp.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"Authentication credentials invalid"
    )

    results = [_paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)")]
    cfg = _make_notifier_cfg()

    # Must not raise
    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is False


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_n03_smtp_failure_os_error_returns_false(mock_smtp, mock_ollama):
    """N-03: Network-level SMTP failure → returns False."""
    mock_ollama.return_value = "<p>Results.</p>"
    mock_smtp.return_value.__enter__.side_effect = OSError("Connection refused")

    results = [_missing_result("Links Lane")]
    cfg = _make_notifier_cfg()

    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is False


# ---------------------------------------------------------------------------
# N-04: LLM unavailable → Python template fallback used, email still sent
# ---------------------------------------------------------------------------


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_n04_llm_unavailable_uses_fallback_email_still_sent(mock_smtp, mock_ollama):
    """N-04: Ollama unavailable for email → fallback body with 'unavailable' note; email sent."""
    mock_ollama.side_effect = OSError("Connection refused to Ollama")

    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
    ]
    cfg = _make_notifier_cfg()

    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is True
    call_args = mock_smtp.return_value.__enter__.return_value.sendmail.call_args
    assert call_args is not None
    body = decode_mime_body(call_args[0][2])
    assert "unavailable" in body.lower()


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_n04_llm_timeout_uses_fallback(mock_smtp, mock_ollama):
    """N-04: Ollama timeout → fallback body used; email still sent."""
    import urllib.error
    mock_ollama.side_effect = urllib.error.URLError("timed out")

    results = [_missing_result("505")]
    cfg = _make_notifier_cfg()

    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is True
    call_args = mock_smtp.return_value.__enter__.return_value.sendmail.call_args
    assert call_args is not None
    body = decode_mime_body(call_args[0][2])
    assert "unavailable" in body.lower()


# ---------------------------------------------------------------------------
# Fallback body unit tests
# ---------------------------------------------------------------------------


def test_fallback_body_contains_llm_unavailable_note_when_flag_set():
    """Fallback body includes LLM-unavailable notice when llm_unavailable=True."""
    results = [_paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)")]
    body = _fallback_body(results, RUN_DATE, llm_unavailable=True, error_message=None)
    assert "LLM review and email generation were unavailable" in body


def test_fallback_body_no_llm_note_when_flag_false():
    """Fallback body does not include LLM note when llm_unavailable=False."""
    results = [_paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)")]
    body = _fallback_body(results, RUN_DATE, llm_unavailable=False, error_message=None)
    assert "LLM review and email generation were unavailable" not in body


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


# ---------------------------------------------------------------------------
# HTML highlighting in fallback body
# ---------------------------------------------------------------------------


def test_fallback_body_late_payment_has_yellow_highlight():
    results = [
        _late_result("Calmar", 3100.00, "Rental Income (Calmar)"),
    ]
    body = _fallback_body(results, RUN_DATE, llm_unavailable=False, error_message=None)
    assert "#FFEB3B" in body
    assert "#EF5350" not in body


def test_fallback_body_missing_has_red_highlight():
    results = [
        _missing_result("Calmar"),
    ]
    body = _fallback_body(results, RUN_DATE, llm_unavailable=False, error_message=None)
    assert "#EF5350" in body
    assert "#FFEB3B" not in body


def test_fallback_body_on_time_has_no_highlight():
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
    ]
    body = _fallback_body(results, RUN_DATE, llm_unavailable=False, error_message=None)
    assert "#FFEB3B" not in body
    assert "#EF5350" not in body


def test_fallback_body_highlights_only_name_and_status_not_full_line():
    """The yellow/red <span> must close before the transaction detail."""
    results = [_late_result("Calmar", 3100.00, "Rental Income (Calmar)")]
    body = _fallback_body(results, RUN_DATE, llm_unavailable=False, error_message=None)
    # The highlight span must close (</span>) before the transaction line appears.
    highlight_end = body.find("</span>")
    transaction_start = body.find("Transaction:")
    assert highlight_end != -1
    assert transaction_start != -1
    assert highlight_end < transaction_start


# ---------------------------------------------------------------------------
# LLM validation: discard body that omits the required summary line
# ---------------------------------------------------------------------------


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_llm_body_without_summary_line_falls_back_to_python_template(mock_smtp, mock_ollama):
    """If LLM response omits the required summary line, fallback body is used instead."""
    mock_ollama.return_value = "<p>Everything looks fine.</p>"  # no summary line

    results = [_paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)")]
    cfg = _make_notifier_cfg()

    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is True
    call_args = mock_smtp.return_value.__enter__.return_value.sendmail.call_args
    body = decode_mime_body(call_args[0][2])
    # Fallback body must contain the correct summary line
    assert "All 1 rent payments received on time." in body
    # LLM text must NOT appear
    assert "Everything looks fine." not in body


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_llm_body_with_summary_line_is_used(mock_smtp, mock_ollama):
    """If LLM response contains the required summary line, it is accepted."""
    summary = "All 1 rent payments received on time."
    mock_ollama.return_value = f"<p>{summary}</p><ul><li>Links Lane: paid.</li></ul>"

    results = [_paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)")]
    cfg = _make_notifier_cfg()

    sent = send_notification(results, cfg, run_date=RUN_DATE)

    assert sent is True
    call_args = mock_smtp.return_value.__enter__.return_value.sendmail.call_args
    body = decode_mime_body(call_args[0][2])
    assert "Links Lane: paid." in body
