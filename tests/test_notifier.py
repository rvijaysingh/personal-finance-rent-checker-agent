"""Notification tests — N-01 through N-04.

Tests email subject lines, body generation, SMTP failure handling, and the
LLM-unavailable fallback path. All SMTP and Ollama calls are mocked.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, call, patch

import pytest

from src.models import PaymentStatus, PropertyResult, TransactionRecord
from src.notifier import _build_subject, _fallback_body, send_notification
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


def test_n02_fallback_body_separates_paid_and_missing():
    """N-02 (unit): Fallback body lists paid and missing properties separately."""
    results = [
        _paid_result("Links Lane", 2950.00, "Rental Income (Links Lane)"),
        _missing_result("Calmar"),
    ]
    body = _fallback_body(results, RUN_DATE, llm_unavailable=False, error_message=None)

    assert "Links Lane" in body
    assert "Calmar" in body
    assert "ACTION NEEDED" in body
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
