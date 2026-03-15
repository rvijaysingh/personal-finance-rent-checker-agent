"""LLM integration tests — L-01 through L-07.

Tests JSON parsing (`_parse_json_response`), Ollama HTTP behaviour, and the
Anthropic primary + Ollama fallback chain for Step 3.

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
    load_fixture_raw,
    make_cfg_mock,
    make_txn,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ollama_payload(match_found: bool, index: int = 0) -> str:
    if match_found:
        return json.dumps({
            "status": "likely_match",
            "matched_transaction_index": index,
            "confidence": "high",
            "rationale": "Test rationale.",
        })
    return json.dumps({
        "status": "no_match_found",
        "matched_transaction_index": None,
        "confidence": "low",
        "rationale": "No match found.",
    })


# ---------------------------------------------------------------------------
# L-01: Ollama returns valid JSON → parsed and used
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_l01_valid_json_response_produces_review_needed(mock_call, mock_health):
    """L-01: Ollama returns valid JSON → result is REVIEW_NEEDED with matched transaction."""
    fixture = load_fixture_raw("llm_valid_response.json")
    mock_call.return_value = fixture

    cfg = make_cfg_mock([PROP_LINKS_LANE])
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.REVIEW_NEEDED
    assert result.matched_transaction is txn
    assert result.step_resolved_by == 3
    mock_call.assert_called_once()


# ---------------------------------------------------------------------------
# L-02: Ollama unreachable → deterministic matches returned, template fallback
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=False)
@patch("src.transaction_matcher._call_ollama")
def test_l02_ollama_unreachable_step3_skipped_returns_missing(mock_call, mock_health):
    """L-02: Ollama unreachable → Step 3 skipped, MISSING returned."""
    cfg = make_cfg_mock([PROP_LINKS_LANE])
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.MISSING
    assert "unreachable" in result.notes.lower()
    assert result.step_resolved_by == 3
    mock_call.assert_not_called()  # LLM call never made


# ---------------------------------------------------------------------------
# L-03: Ollama returns markdown-fenced JSON
# ---------------------------------------------------------------------------


def test_l03_markdown_fenced_json_parsed_correctly():
    """L-03: _parse_json_response strips ```json fences and returns a dict."""
    raw = load_fixture_raw("llm_markdown_fenced.txt")
    result = _parse_json_response(raw)

    assert result is not None
    assert isinstance(result, dict)
    assert result.get("status") == "likely_match"
    assert result.get("matched_transaction_index") == 0


# ---------------------------------------------------------------------------
# L-04: Ollama returns preamble prose + JSON
# ---------------------------------------------------------------------------


def test_l04_preamble_prose_json_extracted():
    """L-04: _parse_json_response extracts JSON from prose-wrapped LLM output."""
    raw = load_fixture_raw("llm_preamble.txt")
    result = _parse_json_response(raw)

    assert result is not None
    assert isinstance(result, dict)
    assert result.get("status") == "likely_match"
    assert result.get("matched_transaction_index") == 0


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


# ---------------------------------------------------------------------------
# L-07: Anthropic primary LLM for Step 3 — fallback chain
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable")
@patch("src.transaction_matcher._call_ollama")
@patch("src.transaction_matcher._call_anthropic")
def test_l07_anthropic_called_first_when_api_key_set(mock_anthropic, mock_ollama, mock_health):
    """L-07: Anthropic API key set → _call_anthropic is tried first; Ollama not called."""
    fixture = load_fixture_raw("llm_valid_response.json")
    mock_anthropic.return_value = fixture

    cfg = make_cfg_mock([PROP_LINKS_LANE])
    cfg.anthropic_api_key = "sk-ant-test-key"
    cfg.anthropic_model = "claude-haiku-4-5-20251001"
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.REVIEW_NEEDED
    mock_anthropic.assert_called_once()
    mock_ollama.assert_not_called()


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
@patch("src.transaction_matcher._call_anthropic")
def test_l07_anthropic_failure_falls_back_to_ollama(mock_anthropic, mock_ollama, mock_health):
    """L-07: Anthropic raises → Ollama is tried as fallback."""
    mock_anthropic.side_effect = OSError("Anthropic unreachable")
    fixture = load_fixture_raw("llm_valid_response.json")
    mock_ollama.return_value = fixture

    cfg = make_cfg_mock([PROP_LINKS_LANE])
    cfg.anthropic_api_key = "sk-ant-test-key"
    cfg.anthropic_model = "claude-haiku-4-5-20251001"
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.REVIEW_NEEDED
    mock_anthropic.assert_called_once()
    mock_ollama.assert_called_once()


@patch("src.transaction_matcher._check_ollama_reachable", return_value=False)
@patch("src.transaction_matcher._call_ollama")
@patch("src.transaction_matcher._call_anthropic")
def test_l07_both_llms_unavailable_returns_missing(mock_anthropic, mock_ollama, mock_health):
    """L-07: Both Anthropic and Ollama unavailable → MISSING with note."""
    mock_anthropic.side_effect = OSError("Anthropic unreachable")

    cfg = make_cfg_mock([PROP_LINKS_LANE])
    cfg.anthropic_api_key = "sk-ant-test-key"
    cfg.anthropic_model = "claude-haiku-4-5-20251001"
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.MISSING
    assert "unreachable" in result.notes.lower() or "unavailable" in result.notes.lower()
    mock_ollama.assert_not_called()


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
@patch("src.transaction_matcher._call_anthropic")
def test_l07_no_anthropic_key_skips_anthropic_uses_ollama(mock_anthropic, mock_ollama, mock_health):
    """L-07: No Anthropic key set → Anthropic skipped, Ollama called directly."""
    fixture = load_fixture_raw("llm_valid_response.json")
    mock_ollama.return_value = fixture

    cfg = make_cfg_mock([PROP_LINKS_LANE])
    cfg.anthropic_api_key = ""  # no key
    cfg.anthropic_model = "claude-haiku-4-5-20251001"
    txn = make_txn(category="Uncategorized")

    result = _step3_llm_match(PROP_LINKS_LANE, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.REVIEW_NEEDED
    mock_anthropic.assert_not_called()
    mock_ollama.assert_called_once()
