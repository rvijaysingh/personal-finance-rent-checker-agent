"""Transaction matcher tests — M-01 through M-11.

Tests the three-step hybrid matching pipeline using fixture transaction lists.
All Ollama HTTP calls are mocked so no real network requests are made.

Naming: test_{scenario_id}_{brief_description}
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.models import PaymentStatus, PropertyConfig, TransactionRecord
from src.transaction_matcher import (
    _step1_category_match,
    _step2_amount_match,
    match_properties,
)
from tests.conftest import (
    ALL_PROPS,
    CHECK_MONTH,
    PROP_505,
    PROP_CALMAR,
    PROP_LINKS_LANE,
    load_txn_fixture,
    make_cfg_mock,
    make_txn,
)


def _llm_no_match() -> str:
    return json.dumps({"match_found": False, "transaction_indices": [], "confidence": "low", "reasoning": "No match."})


def _llm_match(index: int = 0) -> str:
    return json.dumps({"match_found": True, "transaction_indices": [index], "confidence": "high", "reasoning": "Match found."})


def _llm_split(indices: list[int]) -> str:
    return json.dumps({"match_found": True, "transaction_indices": indices, "confidence": "medium", "reasoning": "Split payment detected."})


# ---------------------------------------------------------------------------
# M-01: All 3 properties paid on time, correct amounts and categories
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_m01_all_three_paid_on_time(mock_llm, mock_health, all_paid_txns):
    """M-01: All 3 properties paid on time, correct amounts, correct categories → all 'paid_on_time'."""
    cfg = make_cfg_mock(ALL_PROPS)
    results = match_properties(all_paid_txns, cfg)

    assert len(results) == 3
    assert all(r.status == PaymentStatus.PAID_ON_TIME for r in results)
    assert all(r.step_resolved_by == 1 for r in results)
    mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# M-02: 1 of 3 paid, 2 missing
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_m02_one_paid_two_missing(mock_llm, mock_health):
    """M-02: 1 of 3 paid; 2 go through Step 3 with no LLM match → MISSING."""
    mock_llm.return_value = _llm_no_match()
    txns = load_txn_fixture("partial_paid.json")
    cfg = make_cfg_mock(ALL_PROPS)

    results = match_properties(txns, cfg)

    paid = [r for r in results if r.status == PaymentStatus.PAID_ON_TIME]
    missing = [r for r in results if r.status == PaymentStatus.MISSING]

    assert len(paid) == 1
    assert paid[0].property_name == "Links Lane"
    assert len(missing) == 2
    assert mock_llm.call_count == 2  # Called once for each unresolved property


# ---------------------------------------------------------------------------
# M-03: Zero transactions in current month
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_m03_empty_month_all_missing_no_crash(mock_llm, mock_health):
    """M-03: 0 transactions → all MISSING, no crash, LLM not called (no candidates)."""
    txns = load_txn_fixture("empty_month.json")
    cfg = make_cfg_mock(ALL_PROPS)

    results = match_properties(txns, cfg)

    assert len(results) == 3
    assert all(r.status == PaymentStatus.MISSING for r in results)
    mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# M-04: Duplicate Zelle from same merchant, same amount
# ---------------------------------------------------------------------------


def test_m04_duplicate_zelle_first_used_second_flagged_in_notes():
    """M-04: Two category-matched transactions → first used, WARNING in notes."""
    txns = load_txn_fixture("duplicate_zelle.json")
    assert len(txns) == 2

    result = _step1_category_match(PROP_LINKS_LANE, txns, CHECK_MONTH)

    assert result is not None
    assert result.matched_transaction is txns[0]  # first match used
    assert "WARNING" in result.notes
    assert "2" in result.notes  # "2 category-matched transactions found"
    assert result.step_resolved_by == 1


# ---------------------------------------------------------------------------
# M-05: Two transactions sum to expected rent, neither matches alone
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_m05_split_payment_neither_matches_alone_llm_flags_split(mock_llm, mock_health):
    """M-05: Each transaction is ~50% of rent; Steps 1/2 fail alone; LLM suggests split."""
    mock_llm.return_value = _llm_split([0, 1])
    txns = load_txn_fixture("split_payment.json")
    assert txns[0]["amount"] == 1475.00

    # Verify Steps 1 and 2 don't match either transaction alone
    assert _step1_category_match(PROP_LINKS_LANE, txns, CHECK_MONTH) is None
    assert _step2_amount_match(PROP_LINKS_LANE, txns, CHECK_MONTH) is None

    # Full pipeline: goes to Step 3, LLM suggests split
    cfg = make_cfg_mock([PROP_LINKS_LANE])
    results = match_properties(txns, cfg)

    assert len(results) == 1
    assert results[0].status == PaymentStatus.LLM_SUGGESTED
    assert results[0].step_resolved_by == 3
    assert "split payment" in results[0].notes.lower()


# ---------------------------------------------------------------------------
# M-06: Payment dated Feb 28 in a March run (early payment lookback)
# ---------------------------------------------------------------------------


def test_m06_prior_month_payment_within_lookback_matches():
    """M-06: Feb 28 transaction → on-time match for March rent (within grace deadline)."""
    txns = load_txn_fixture("prior_month_payment.json")
    assert txns[0]["date"] == date(2026, 2, 28)

    result = _step1_category_match(PROP_LINKS_LANE, txns, CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.PAID_ON_TIME
    assert result.step_resolved_by == 1


# ---------------------------------------------------------------------------
# M-07: Payment dated March 7 — after 5-day grace period (deadline March 6)
# ---------------------------------------------------------------------------


def test_m07_late_payment_after_grace_flagged():
    """M-07: March 7 payment with due_day=1 + grace=5 → deadline March 6 → PAID_LATE."""
    txns = load_txn_fixture("late_payment.json")
    assert txns[0]["date"] == date(2026, 3, 7)

    result = _step1_category_match(PROP_LINKS_LANE, txns, CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.PAID_LATE
    assert result.step_resolved_by == 1
    assert "LATE" in result.notes


# ---------------------------------------------------------------------------
# M-08: Correct category, wrong amount ($1,500 vs $2,950)
# ---------------------------------------------------------------------------


def test_m08_category_match_wrong_amount_returns_wrong_amount_not_llm():
    """M-08: Category matches but amount wrong → WRONG_AMOUNT from Step 1; does not fall to LLM."""
    txns = load_txn_fixture("category_mismatch.json")
    assert txns[0]["amount"] == 1500.00

    result = _step1_category_match(PROP_LINKS_LANE, txns, CHECK_MONTH)

    assert result is not None
    assert result.status == PaymentStatus.WRONG_AMOUNT
    assert result.step_resolved_by == 1
    assert "1500" in result.notes


# ---------------------------------------------------------------------------
# M-09: Correct amount ($2,950), category wrong ("Transfer")
# ---------------------------------------------------------------------------


def test_m09_amount_match_no_category_returns_possible_match_step2():
    """M-09: Correct amount but wrong category → Step 1 misses, Step 2 returns POSSIBLE_MATCH."""
    txns = load_txn_fixture("amount_no_category.json")
    assert txns[0]["category"] == "Transfer"

    # Step 1 should not match
    s1 = _step1_category_match(PROP_LINKS_LANE, txns, CHECK_MONTH)
    assert s1 is None

    # Step 2 should match
    s2 = _step2_amount_match(PROP_LINKS_LANE, txns, CHECK_MONTH)
    assert s2 is not None
    assert s2.status == PaymentStatus.POSSIBLE_MATCH
    assert s2.step_resolved_by == 2
    assert "MANUAL REVIEW" in s2.notes


# ---------------------------------------------------------------------------
# M-10: Transaction claimed by Step 1 for A is not offered to Step 3 for B
# ---------------------------------------------------------------------------


@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_m10_no_double_match_step1_claim_excludes_from_step3(mock_llm, mock_health):
    """M-10: Transaction matched by Step 1 for A is excluded from Step 3 candidates for B."""
    # Use fixture: 1 transaction with Links Lane category
    txns = load_txn_fixture("ambiguous_match.json")
    assert len(txns) == 1

    # Two properties: A matches by category, B has no match
    cfg = make_cfg_mock([PROP_LINKS_LANE, PROP_CALMAR])
    results = match_properties(txns, cfg)

    result_a = next(r for r in results if r.property_name == "Links Lane")
    result_b = next(r for r in results if r.property_name == "Calmar")

    # A resolved by Step 1
    assert result_a.status == PaymentStatus.PAID_ON_TIME
    assert result_a.step_resolved_by == 1

    # B goes to Step 3 but has NO candidates (txn claimed by A)
    assert result_b.status == PaymentStatus.MISSING
    assert result_b.step_resolved_by == 3
    mock_llm.assert_not_called()  # No candidates to offer LLM


# ---------------------------------------------------------------------------
# M-11: Category label with extra whitespace still matches
# ---------------------------------------------------------------------------


def test_m11_category_with_extra_whitespace_stripped_and_matched():
    """M-11: Leading/trailing whitespace in transaction's category label is stripped before comparison."""
    txns = load_txn_fixture("messy_merchant.json")
    assert txns[0]["category"] == "  Rental Income (Links Lane)  "

    result = _step1_category_match(PROP_LINKS_LANE, txns, CHECK_MONTH)

    assert result is not None
    assert result.status in (PaymentStatus.PAID_ON_TIME, PaymentStatus.PAID_LATE)
    assert result.step_resolved_by == 1
