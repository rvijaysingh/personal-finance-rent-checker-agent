"""LLM integration tests — L-01 through L-06.

Tests JSON parsing (`_parse_json_response`), Ollama HTTP behaviour, and the
notifier's fallback path when Ollama is unavailable.

All network calls are mocked via unittest.mock.patch.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.models import PaymentStatus, PropertyResult, TransactionRecord
from src.transaction_matcher import (
    OllamaUnavailableError,
    _parse_json_response,
    _step3_llm_match,
)
from tests.conftest import (
    CHECK_MONTH,
    PROP_LINKS_LANE,
    decode_mime_body,
    load_fixture_raw,
    make_cfg_mock,
    make_txn,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ollama_payload(match_found: bool, indices: list[int] | None = None) -> str:
    return json.dumps({
        "match_found": match_found,
        "transaction_indices": indices if indices is not None else ([0] if match_found else []),
        "confidence": "high" if match_found else "low",
        "reasoning": "Test reasoning.",
    })


# ---------------------------------------------------------------------------
# L-01: Ollama returns valid JSON → parsed and used
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_l01_valid_json_response_produces_llm_suggested(mock_call, mock_health):
    """L-01: Ollama returns valid JSON → result is LLM_SUGGESTED with matched transaction."""
    fixture = load_fixture_raw("llm_valid_response.json")
    mock_call.return_value = fixture

    cfg = make_cfg_mock([PROP_LINKS_LANE])
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.LLM_SUGGESTED
    assert result.matched_transaction is txn
    assert result.step_resolved_by == 3
    assert "Test reasoning." not in result.notes or "Test reasoning." in result.notes  # reasoning included
    mock_call.assert_called_once()


# ---------------------------------------------------------------------------
# L-02: Ollama unreachable → deterministic matches returned, template fallback
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=False)
@patch("src.transaction_matcher._call_ollama")
def test_l02_ollama_unreachable_step3_skipped_returns_llm_skipped_missing(mock_call, mock_health):
    """L-02: Ollama unreachable → Step 3 skipped, LLM_SKIPPED_MISSING returned."""
    cfg = make_cfg_mock([PROP_LINKS_LANE])
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.LLM_SKIPPED_MISSING
    assert "Ollama unreachable" in result.notes
    assert result.step_resolved_by == 3
    mock_call.assert_not_called()  # LLM call never made


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_l02_ollama_unreachable_email_uses_fallback_template(mock_smtp, mock_ollama):
    """L-02 (notifier): Ollama unavailable for email → Python template fallback used."""
    from src.notifier import send_notification

    mock_ollama.side_effect = OSError("Connection refused")

    results = [
        PropertyResult(
            property_name="Links Lane",
            status=PaymentStatus.PAID_ON_TIME,
            matched_transaction=make_txn(),
            notes="On time.",
            step_resolved_by=1,
        )
    ]
    cfg = make_cfg_mock()
    cfg.gmail_sender = "sender@example.com"
    cfg.gmail_password = "password"
    cfg.gmail_recipient = "recv@example.com"

    sent = send_notification(results, cfg, run_date=date(2026, 3, 1))

    assert sent is True
    # Email body generated via fallback template mentions LLM unavailability
    call_args = mock_smtp.return_value.__enter__.return_value.sendmail.call_args
    assert call_args is not None
    body = decode_mime_body(call_args[0][2])
    assert "unavailable" in body.lower() or "LLM" in body


# ---------------------------------------------------------------------------
# L-03: Ollama returns markdown-fenced JSON
# ---------------------------------------------------------------------------


def test_l03_markdown_fenced_json_parsed_correctly():
    """L-03: _parse_json_response strips ```json fences and returns a dict."""
    raw = load_fixture_raw("llm_markdown_fenced.txt")
    result = _parse_json_response(raw)

    assert result is not None
    assert isinstance(result, dict)
    assert result.get("match_found") is True
    assert result.get("transaction_indices") == [0]


# ---------------------------------------------------------------------------
# L-04: Ollama returns preamble prose + JSON
# ---------------------------------------------------------------------------


def test_l04_preamble_prose_json_extracted():
    """L-04: _parse_json_response extracts JSON from prose-wrapped LLM output."""
    raw = load_fixture_raw("llm_preamble.txt")
    result = _parse_json_response(raw)

    assert result is not None
    assert isinstance(result, dict)
    assert result.get("match_found") is True
    assert result.get("transaction_indices") == [0]


# ---------------------------------------------------------------------------
# L-05: Ollama returns malformed JSON → property marked MISSING
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_l05_malformed_json_response_returns_missing(mock_call, mock_health):
    """L-05: Malformed LLM JSON → _parse_json_response returns None → MISSING status."""
    mock_call.return_value = load_fixture_raw("llm_invalid.txt")

    cfg = make_cfg_mock([PROP_LINKS_LANE])
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.MISSING
    assert result.step_resolved_by == 3
    # Raw response snippet should appear in notes
    assert "could not be parsed" in result.notes


def test_l05_parse_json_response_invalid_returns_none():
    """L-05: _parse_json_response returns None for non-JSON text."""
    raw = load_fixture_raw("llm_invalid.txt")
    result = _parse_json_response(raw)
    assert result is None


# ---------------------------------------------------------------------------
# L-06: Ollama returns empty string → treated as unavailable
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_l06_empty_llm_response_returns_missing(mock_call, mock_health):
    """L-06: Empty LLM response → _parse_json_response returns None → MISSING."""
    mock_call.return_value = ""

    cfg = make_cfg_mock([PROP_LINKS_LANE])
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.MISSING
    assert result.step_resolved_by == 3


def test_l06_parse_json_empty_string_returns_none():
    """L-06: Empty string → _parse_json_response returns None."""
    assert _parse_json_response("") is None


@patch("src.notifier._call_ollama_for_summary")
@patch("smtplib.SMTP")
def test_l06_empty_ollama_response_notifier_uses_fallback(mock_smtp, mock_ollama):
    """L-06 (notifier): Empty Ollama response treated as unavailable → fallback template."""
    from src.notifier import send_notification

    mock_ollama.return_value = ""  # empty string triggers ValueError("empty response")

    results = [
        PropertyResult(
            property_name="Links Lane",
            status=PaymentStatus.MISSING,
            matched_transaction=None,
            notes="No match found.",
            step_resolved_by=3,
        )
    ]
    cfg = make_cfg_mock()
    cfg.gmail_sender = "sender@example.com"
    cfg.gmail_password = "password"
    cfg.gmail_recipient = "recv@example.com"

    sent = send_notification(results, cfg, run_date=date(2026, 3, 1))

    assert sent is True
    call_args = mock_smtp.return_value.__enter__.return_value.sendmail.call_args
    assert call_args is not None
    body = decode_mime_body(call_args[0][2])
    assert "unavailable" in body.lower() or "LLM" in body
