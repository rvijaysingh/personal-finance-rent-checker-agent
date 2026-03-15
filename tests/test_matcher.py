"""Unit tests for src/transaction_matcher.py.

All external calls (Ollama) are mocked. No real network requests are made.
Tests follow the naming convention: test_{function}_{scenario}_{expected_result}.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.models import PaymentStatus, PropertyConfig, PropertyResult, TransactionRecord
from src.transaction_matcher import (
    OllamaUnavailableError,
    _amount_matches,
    _due_deadline,
    _is_on_time,
    _parse_json_response,
    _step1_category_match,
    _step2_amount_match,
    match_properties,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_prop(
    *,
    name: str = "Links Lane",
    tenant: str = "Alice Smith",
    rent: float = 1500.0,
    due_day: int = 1,
    grace: int = 5,
    category: str = "Rental Income (Links Lane)",
    account: str = "Chase Checking ••1230",
) -> PropertyConfig:
    return PropertyConfig(
        name=name,
        merchant_name=tenant,
        expected_rent=rent,
        due_day=due_day,
        grace_period_days=grace,
        category_label=category,
        account=account,
    )


def make_txn(
    *,
    txn_date: date = date(2026, 3, 3),
    description: str = "Zelle from Alice",
    amount: float = 1500.0,
    account: str = "Chase Checking ••1230",
    category: str = "Rental Income (Links Lane)",
) -> TransactionRecord:
    return TransactionRecord(
        date=txn_date,
        description=description,
        amount=amount,
        account=account,
        category=category,
    )


def make_config(tolerance: float = 2.0, ollama_available: bool = True) -> MagicMock:
    cfg = MagicMock()
    cfg.amount_tolerance_percent = tolerance
    cfg.ollama_endpoint = "http://localhost:11434"
    cfg.ollama_model = "qwen3:8b"
    cfg.anthropic_api_key = ""
    cfg.anthropic_model = "claude-haiku-4-5-20251001"
    cfg.prompts = {"rent_match": "Template: {{property_name}} {{transactions_json}}"}
    return cfg


CHECK_MONTH = date(2026, 3, 1)


# ---------------------------------------------------------------------------
# Amount matching
# ---------------------------------------------------------------------------


def test_amount_matches_exact_returns_true():
    assert _amount_matches(1500.0, 1500.0, 2.0) is True


def test_amount_matches_within_tolerance_returns_true():
    # 2% of 1500 = 30; 1500 + 29 = 1529 should be within tolerance
    assert _amount_matches(1529.0, 1500.0, 2.0) is True


def test_amount_matches_outside_tolerance_returns_false():
    # 2% of 1500 = 30; 1500 + 31 = 1531 should be outside tolerance
    assert _amount_matches(1531.0, 1500.0, 2.0) is False


def test_amount_matches_zero_expected_exact_match_returns_true():
    assert _amount_matches(0.0, 0.0, 2.0) is True


def test_amount_matches_zero_expected_nonzero_actual_returns_false():
    assert _amount_matches(1.0, 0.0, 2.0) is False


def test_amount_matches_boundary_exactly_at_tolerance_returns_true():
    # Exactly 2% over: 1500 * 1.02 = 1530.0
    assert _amount_matches(1530.0, 1500.0, 2.0) is True


# ---------------------------------------------------------------------------
# Date / deadline helpers
# ---------------------------------------------------------------------------


def test_is_on_time_payment_on_due_day_returns_true():
    assert _is_on_time(date(2026, 3, 1), due_day=1, grace_period_days=5, check_month=CHECK_MONTH) is True


def test_is_on_time_payment_within_grace_returns_true():
    assert _is_on_time(date(2026, 3, 5), due_day=1, grace_period_days=5, check_month=CHECK_MONTH) is True


def test_is_on_time_payment_on_last_grace_day_returns_true():
    # due_day=1 + grace=5 → deadline is March 6
    assert _is_on_time(date(2026, 3, 6), due_day=1, grace_period_days=5, check_month=CHECK_MONTH) is True


def test_is_on_time_payment_one_day_late_returns_false():
    # Deadline is March 6; March 7 is late
    assert _is_on_time(date(2026, 3, 7), due_day=1, grace_period_days=5, check_month=CHECK_MONTH) is False


def test_due_deadline_no_grace_returns_due_day():
    result = _due_deadline(due_day=1, grace_period_days=0, check_month=CHECK_MONTH)
    assert result == date(2026, 3, 1)


def test_due_deadline_grace_wraps_to_next_month():
    # due_day=28, grace=5 → deadline = April 2
    result = _due_deadline(due_day=28, grace_period_days=5, check_month=CHECK_MONTH)
    assert result == date(2026, 4, 2)


# ---------------------------------------------------------------------------
# Step 1 — Category match
# ---------------------------------------------------------------------------


def test_step1_category_match_on_time_returns_paid_on_time():
    prop = make_prop()
    txn = make_txn(txn_date=date(2026, 3, 3))
    result = _step1_category_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.PAID_ON_TIME
    assert result.matched_transaction == txn
    assert result.step_resolved_by == 1


def test_step1_category_match_late_returns_paid_late():
    prop = make_prop()
    txn = make_txn(txn_date=date(2026, 3, 10))  # after grace deadline of March 6
    result = _step1_category_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.PAID_LATE
    assert result.step_resolved_by == 1


def test_step1_category_match_wrong_amount_returns_review_needed():
    prop = make_prop(rent=1500.0)
    txn = make_txn(amount=1000.0)  # far outside 2% tolerance → partial match flagged
    result = _step1_category_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.REVIEW_NEEDED
    assert result.step_resolved_by == 1
    assert "amount" in result.notes.lower()
    assert "tolerance" in result.notes.lower()


def test_step1_category_match_no_match_returns_none():
    prop = make_prop(category="Rental Income (Links Lane)")
    txn = make_txn(category="Food & Dining")
    result = _step1_category_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is None


def test_step1_category_match_wrong_account_returns_none():
    prop = make_prop(account="Chase Checking ••1230")
    txn = make_txn(account="Chase Savings ••9999", category="Rental Income (Links Lane)")
    result = _step1_category_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is None


def test_step1_category_match_account_with_suffix_returns_match():
    """Config account 'Total Checking (First Republic)' matches Monarch returning
    'Total Checking (First Republic) (...1829)' — substring match must pass."""
    prop = make_prop(
        account="Total Checking (First Republic)",
        category="Rental Income (505)",
    )
    txn = make_txn(
        account="Total Checking (First Republic) (...1829)",
        category="Rental Income (505)",
    )
    result = _step1_category_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.PAID_ON_TIME


def test_step1_category_match_multiple_transactions_warns_in_notes():
    prop = make_prop()
    txn1 = make_txn(txn_date=date(2026, 3, 1))
    txn2 = make_txn(txn_date=date(2026, 3, 15))
    result = _step1_category_match(prop, [txn1, txn2], check_month=CHECK_MONTH)

    assert result is not None
    assert "WARNING" in result.notes
    assert "2" in result.notes


def test_step1_category_match_amount_within_tolerance_returns_paid_on_time():
    prop = make_prop(rent=1500.0)
    txn = make_txn(amount=1510.0)  # ~0.67% over — within 2%
    result = _step1_category_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.PAID_ON_TIME


# ---------------------------------------------------------------------------
# Step 2 — Amount fallback
# ---------------------------------------------------------------------------


def test_step2_amount_match_returns_review_needed():
    prop = make_prop(rent=1500.0)
    txn = make_txn(category="Uncategorized", amount=1500.0)
    result = _step2_amount_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.REVIEW_NEEDED
    assert result.step_resolved_by == 2
    assert "category" in result.notes.lower()


def test_step2_amount_match_no_match_returns_none():
    prop = make_prop(rent=1500.0)
    txn = make_txn(amount=500.0)  # far from 1500
    result = _step2_amount_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is None


def test_step2_amount_match_ignores_negative_amounts():
    prop = make_prop(rent=1500.0)
    txn = make_txn(amount=-1500.0)  # expense, not income
    result = _step2_amount_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is None


def test_step2_amount_match_multiple_results_notes_ambiguity():
    prop = make_prop(rent=1500.0)
    txn1 = make_txn(category="Transfer", amount=1500.0, description="Wire 1")
    txn2 = make_txn(category="Transfer", amount=1500.0, description="Wire 2")
    result = _step2_amount_match(prop, [txn1, txn2], check_month=CHECK_MONTH)

    assert result is not None
    assert "2" in result.notes  # "2 amount-matching transactions"


def test_step2_amount_match_account_with_suffix_returns_match():
    """Config account substring matches Monarch account with appended suffix."""
    prop = make_prop(rent=1500.0, account="Total Checking (First Republic)")
    txn = make_txn(
        category="Uncategorized",
        amount=1500.0,
        account="Total Checking (First Republic) (...1829)",
    )
    result = _step2_amount_match(prop, [txn], check_month=CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.REVIEW_NEEDED


# ---------------------------------------------------------------------------
# Step 3 — LLM fallback (mocked)
# ---------------------------------------------------------------------------


def _make_ollama_response(match_found: bool, index: int = 0) -> str:
    if match_found:
        return json.dumps({
            "status": "likely_match",
            "matched_transaction_index": index,
            "confidence": "medium",
            "rationale": "Test rationale.",
        })
    return json.dumps({
        "status": "no_match_found",
        "matched_transaction_index": None,
        "confidence": "low",
        "rationale": "No match found.",
    })


@patch("src.transaction_matcher._call_ollama")
def test_step3_llm_finds_match_returns_review_needed(mock_ollama):
    mock_ollama.return_value = _make_ollama_response(match_found=True, index=0)
    cfg = make_config()
    prop = make_prop()
    txn = make_txn(category="Uncategorized")

    from src.transaction_matcher import _step3_llm_match

    result = _step3_llm_match(prop, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.REVIEW_NEEDED
    assert result.matched_transaction == txn
    assert result.step_resolved_by == 3
    assert "Test rationale." in result.notes


@patch("src.transaction_matcher._call_ollama")
def test_step3_llm_finds_nothing_returns_missing(mock_ollama):
    mock_ollama.return_value = _make_ollama_response(match_found=False)
    cfg = make_config()
    prop = make_prop()
    txn = make_txn(category="Uncategorized")

    from src.transaction_matcher import _step3_llm_match

    result = _step3_llm_match(prop, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.MISSING
    assert result.matched_transaction is None


@patch("src.transaction_matcher._call_ollama")
def test_step3_ollama_unavailable_returns_missing(mock_ollama):
    mock_ollama.side_effect = OllamaUnavailableError("connection refused")
    cfg = make_config()
    prop = make_prop()
    txn = make_txn(category="Uncategorized")

    from src.transaction_matcher import _step3_llm_match

    result = _step3_llm_match(prop, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.MISSING
    assert "unreachable" in result.notes.lower()


@patch("src.transaction_matcher._call_ollama")
def test_step3_llm_malformed_response_returns_missing(mock_ollama):
    mock_ollama.return_value = "this is not json at all"
    cfg = make_config()
    prop = make_prop()
    txn = make_txn(category="Uncategorized")

    from src.transaction_matcher import _step3_llm_match

    result = _step3_llm_match(prop, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.MISSING
    assert "could not be parsed" in result.notes


@patch("src.transaction_matcher._call_ollama")
def test_step3_no_candidates_returns_missing_without_calling_llm(mock_ollama):
    """When all transactions are expenses (negative), skip LLM entirely."""
    cfg = make_config()
    prop = make_prop()
    txn = make_txn(amount=-500.0)  # expense

    from src.transaction_matcher import _step3_llm_match

    result = _step3_llm_match(prop, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.MISSING
    mock_ollama.assert_not_called()


@patch("src.transaction_matcher._call_ollama")
def test_step3_all_positive_transactions_offered_to_llm(mock_ollama):
    """Step 3 sends all positive-amount transactions to LLM regardless of account."""
    mock_ollama.return_value = _make_ollama_response(match_found=True, index=0)
    cfg = make_config()
    prop = make_prop()
    # Transaction from a completely different account — should still reach LLM
    txn = make_txn(
        category="Uncategorized",
        amount=1500.0,
        account="Completely Different Bank Account",
    )

    from src.transaction_matcher import _step3_llm_match

    result = _step3_llm_match(prop, [txn], cfg, CHECK_MONTH)

    assert result.status == PaymentStatus.REVIEW_NEEDED
    mock_ollama.assert_called_once()


# ---------------------------------------------------------------------------
# JSON response parsing
# ---------------------------------------------------------------------------


def test_parse_json_response_clean_json_returns_dict():
    raw = '{"match_found": true, "transaction_indices": [0], "reasoning": "test"}'
    result = _parse_json_response(raw)
    assert result is not None
    assert result["match_found"] is True


def test_parse_json_response_markdown_fence_returns_dict():
    raw = '```json\n{"match_found": false}\n```'
    result = _parse_json_response(raw)
    assert result is not None
    assert result["match_found"] is False


def test_parse_json_response_prose_wrapping_extracts_json():
    raw = 'Here is my answer:\n{"match_found": true, "transaction_indices": [1]}\nThank you.'
    result = _parse_json_response(raw)
    assert result is not None
    assert result["match_found"] is True


def test_parse_json_response_invalid_returns_none():
    assert _parse_json_response("not json at all") is None
    assert _parse_json_response("") is None
    assert _parse_json_response("[]") is None  # array, not object


# ---------------------------------------------------------------------------
# Full pipeline (end-to-end with mocked LLM)
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._call_ollama")
def test_match_properties_all_step1_returns_paid_on_time(mock_ollama):
    """Happy path: all three properties resolved by Step 1."""
    props = [
        make_prop(name="Links Lane", category="Rental Income (Links Lane)"),
        make_prop(name="Calmar", category="Rental Income (Calmar)", account="Chase Checking ••1230"),
        make_prop(name="505", category="Rental Income (505)", account="Chase Checking ••1230"),
    ]
    txns = [
        make_txn(category="Rental Income (Links Lane)", txn_date=date(2026, 3, 3)),
        make_txn(category="Rental Income (Calmar)", txn_date=date(2026, 3, 3)),
        make_txn(category="Rental Income (505)", txn_date=date(2026, 3, 3)),
    ]
    cfg = make_config()
    cfg.properties = props

    results = match_properties(txns, cfg)

    assert len(results) == 3
    assert all(r.status == PaymentStatus.PAID_ON_TIME for r in results)
    assert all(r.step_resolved_by == 1 for r in results)
    mock_ollama.assert_not_called()  # LLM not needed when Step 1 resolves all


@patch("src.transaction_matcher._call_ollama")
def test_match_properties_step2_fallback_used_when_step1_fails(mock_ollama):
    """Property missing category label but correct amount → REVIEW_NEEDED from Step 2."""
    prop = make_prop(rent=1500.0)
    txn = make_txn(category="Transfer", amount=1500.0)
    cfg = make_config()
    cfg.properties = [prop]

    results = match_properties([txn], cfg)

    assert results[0].status == PaymentStatus.REVIEW_NEEDED
    assert results[0].step_resolved_by == 2
    mock_ollama.assert_not_called()  # LLM not needed when Step 2 resolves


@patch("src.transaction_matcher._call_ollama")
def test_match_properties_step3_called_when_steps12_fail(mock_ollama):
    """Property with no category or amount match proceeds to LLM (Step 3)."""
    mock_ollama.return_value = _make_ollama_response(match_found=False)
    prop = make_prop(rent=1500.0)
    txn = make_txn(category="Groceries", amount=50.0)  # unrelated transaction
    cfg = make_config()
    cfg.properties = [prop]

    results = match_properties([txn], cfg)

    assert results[0].status == PaymentStatus.MISSING
    assert results[0].step_resolved_by == 3
    mock_ollama.assert_called_once()


@patch("src.transaction_matcher._call_ollama")
def test_match_properties_empty_transactions_returns_missing_all(mock_ollama):
    """No transactions at all → all properties missing (LLM skips empty candidates)."""
    props = [make_prop(name="Links Lane"), make_prop(name="Calmar")]
    cfg = make_config()
    cfg.properties = props

    results = match_properties([], cfg)

    assert len(results) == 2
    assert all(r.status == PaymentStatus.MISSING for r in results)
    mock_ollama.assert_not_called()
