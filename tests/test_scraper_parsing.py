"""Scraper parsing tests — S-01 through S-05.

Tests the JSON-parsing layer of the Monarch scraper in isolation.
No Playwright, no browser, no network calls — all tests operate on
fixture data fed directly to the internal parsing functions.

The Monarch scraper uses API response interception (not DOM scraping).
These tests exercise the functions that turn raw JSON responses into
TransactionRecord objects.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.monarch_scraper import (
    _clean_category,
    _clean_description,
    _find_transaction_list,
    _map_transaction,
    _parse_api_responses,
    _parse_date,
)
from tests.conftest import FIXTURE_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_captured(response_dict: dict) -> list[dict]:
    """Wrap a response dict in the captured-response format _parse_api_responses expects."""
    return [{"url": "https://api.monarch.com/graphql", "body": json.dumps(response_dict)}]


def _raw_txn(
    *,
    txn_id: str = "txn-001",
    txn_date: str = "2026-03-01",
    amount: float = 2950.00,
    merchant_name: str = "Zelle From JANE DOE",
    category_name: str = "Rental Income (Links Lane)",
    account_display: str = "Chase Checking \u20221230",
) -> dict:
    """Build a raw transaction dict in Monarch API nested format."""
    return {
        "id": txn_id,
        "date": txn_date,
        "amount": amount,
        "merchant": {"name": merchant_name},
        "category": {"name": category_name},
        "account": {"displayName": account_display},
    }


# ---------------------------------------------------------------------------
# S-01: Real API JSON fixture → correct transaction count
# ---------------------------------------------------------------------------


def test_s01_api_fixture_returns_three_transactions(monarch_api_response):
    """S-01: monarch_api_response.json fed to _parse_api_responses → exactly 3 records."""
    captured = _make_captured(monarch_api_response)

    txns = _parse_api_responses(captured)

    assert len(txns) == 3


def test_s01_all_three_transaction_ids_unique(monarch_api_response):
    """S-01: All 3 transactions have distinct descriptions (no dedup loss)."""
    captured = _make_captured(monarch_api_response)

    txns = _parse_api_responses(captured)

    descriptions = {t["description"] for t in txns}
    assert len(descriptions) == 3


def test_s01_duplicate_responses_deduped(monarch_api_response):
    """S-01: Two identical captured responses → still 3 unique transactions (dedup by ID)."""
    body = json.dumps(monarch_api_response)
    captured = [
        {"url": "https://api.monarch.com/graphql?page=1", "body": body},
        {"url": "https://api.monarch.com/graphql?page=2", "body": body},
    ]

    txns = _parse_api_responses(captured)

    assert len(txns) == 3  # Not 6 — duplicates eliminated by transaction ID


# ---------------------------------------------------------------------------
# S-02: Amounts parsed as correct floats
# ---------------------------------------------------------------------------


def test_s02_amounts_are_floats_not_strings(monarch_api_response):
    """S-02: All amounts in parsed transactions are Python float values."""
    captured = _make_captured(monarch_api_response)

    txns = _parse_api_responses(captured)

    for t in txns:
        assert isinstance(t["amount"], float), f"Expected float, got {type(t['amount'])} for {t}"


def test_s02_amounts_match_fixture_values(monarch_api_response):
    """S-02: Parsed amounts equal the fixture values (2950.0, 3100.0, 2800.0)."""
    captured = _make_captured(monarch_api_response)

    txns = _parse_api_responses(captured)

    amounts = {t["amount"] for t in txns}
    assert amounts == {2950.00, 3100.00, 2800.00}


def test_s02_map_transaction_amount_preserved():
    """S-02 (unit): _map_transaction converts numeric amount field to float."""
    raw = _raw_txn(amount=2950.00)
    txn = _map_transaction(raw)
    assert txn is not None
    assert txn["amount"] == 2950.00
    assert isinstance(txn["amount"], float)


# ---------------------------------------------------------------------------
# S-03: Date string parsing
# ---------------------------------------------------------------------------


def test_s03_iso_date_parsed():
    """S-03: ISO date '2026-03-03' → date(2026, 3, 3)."""
    assert _parse_date("2026-03-03") == date(2026, 3, 3)


def test_s03_abbreviated_month_name_parsed():
    """S-03: 'Mar 3, 2026' → date(2026, 3, 3)."""
    assert _parse_date("Mar 3, 2026") == date(2026, 3, 3)


def test_s03_full_month_name_parsed():
    """S-03: 'March 3, 2026' → date(2026, 3, 3)."""
    assert _parse_date("March 3, 2026") == date(2026, 3, 3)


def test_s03_us_date_format_parsed():
    """S-03: '03/03/2026' → date(2026, 3, 3)."""
    assert _parse_date("03/03/2026") == date(2026, 3, 3)


def test_s03_iso_datetime_suffix_stripped():
    """S-03: ISO datetime '2026-03-03T00:00:00' → date(2026, 3, 3) (T-suffix stripped)."""
    assert _parse_date("2026-03-03T00:00:00") == date(2026, 3, 3)


def test_s03_invalid_date_returns_none():
    """S-03: Unrecognised date string → _parse_date returns None."""
    assert _parse_date("not-a-date") is None


def test_s03_empty_date_returns_none():
    """S-03: Empty string → _parse_date returns None."""
    assert _parse_date("") is None


def test_s03_fixture_dates_are_date_objects(monarch_api_response):
    """S-03: All dates in parsed fixture transactions are date objects."""
    captured = _make_captured(monarch_api_response)
    txns = _parse_api_responses(captured)
    for t in txns:
        assert isinstance(t["date"], date), f"Expected date object, got {type(t['date'])}"


# ---------------------------------------------------------------------------
# S-04: Account names extracted correctly
# ---------------------------------------------------------------------------


def test_s04_account_name_from_nested_display_name():
    """S-04: account.displayName nested dict → extracted as plain string."""
    raw = _raw_txn(account_display="Chase Checking \u20221230")
    txn = _map_transaction(raw)
    assert txn is not None
    assert txn["account"] == "Chase Checking \u20221230"


def test_s04_account_name_fallback_to_name_field():
    """S-04: account.name used when displayName absent."""
    raw = _raw_txn()
    raw["account"] = {"name": "Chase Savings"}
    txn = _map_transaction(raw)
    assert txn is not None
    assert txn["account"] == "Chase Savings"


def test_s04_account_string_preserved_directly():
    """S-04: account field as plain string (non-nested) → passed through."""
    raw = _raw_txn()
    raw["account"] = "Chase Checking \u20221230"
    txn = _map_transaction(raw)
    assert txn is not None
    assert txn["account"] == "Chase Checking \u20221230"


def test_s04_fixture_account_names(monarch_api_response):
    """S-04: All transactions from fixture have the expected account name."""
    captured = _make_captured(monarch_api_response)
    txns = _parse_api_responses(captured)
    for t in txns:
        assert t["account"] == "Chase Checking \u20221230", (
            f"Unexpected account: {t['account']!r}"
        )


# ---------------------------------------------------------------------------
# S-05: Category labels extracted correctly
# ---------------------------------------------------------------------------


def test_s05_category_from_nested_name():
    """S-05: category.name nested dict → extracted as plain string."""
    raw = _raw_txn(category_name="Rental Income (Links Lane)")
    txn = _map_transaction(raw)
    assert txn is not None
    assert txn["category"] == "Rental Income (Links Lane)"


def test_s05_category_string_preserved_directly():
    """S-05: category field as plain string → passed through (after _clean_category)."""
    raw = _raw_txn()
    raw["category"] = "Rental Income (Links Lane)"
    txn = _map_transaction(raw)
    assert txn is not None
    assert txn["category"] == "Rental Income (Links Lane)"


def test_s05_fixture_categories():
    """S-05: All three fixture categories extracted correctly."""
    api_response = json.loads(
        (FIXTURE_DIR / "monarch_api_response.json").read_text(encoding="utf-8")
    )
    captured = _make_captured(api_response)
    txns = _parse_api_responses(captured)

    categories = {t["category"] for t in txns}
    assert "Rental Income (Links Lane)" in categories
    assert "Rental Income (Calmar)" in categories
    assert "Rental Income (505)" in categories


def test_s05_clean_category_strips_leading_emoji():
    """S-05: _clean_category strips leading emoji/symbol characters."""
    result = _clean_category("\U0001f3e0 Rental Income (Links Lane)")
    assert result == "Rental Income (Links Lane)"


def test_s05_clean_category_takes_first_line():
    """S-05: _clean_category returns only the first non-empty line."""
    result = _clean_category("Rental Income (Links Lane)\nExtra info")
    assert result == "Rental Income (Links Lane)"


def test_s05_clean_description_removes_unicode_pua():
    """S-05: _clean_description strips Unicode private-use-area icon characters."""
    pua_char = "\ue000"  # example PUA character
    result = _clean_description(f"Zelle From JANE DOE {pua_char}")
    assert pua_char not in result
    assert "Zelle From JANE DOE" in result


# ---------------------------------------------------------------------------
# _find_transaction_list path discovery
# ---------------------------------------------------------------------------


def test_find_transaction_list_standard_path(monarch_api_response):
    """_find_transaction_list resolves data.allTransactions.results path."""
    result = _find_transaction_list(monarch_api_response, "https://test")
    assert result is not None
    assert len(result) == 3


def test_find_transaction_list_alternate_path():
    """_find_transaction_list handles data.getTransactions.results path."""
    data = {"data": {"getTransactions": {"results": [_raw_txn()]}}}
    result = _find_transaction_list(data, "https://test")
    assert result is not None
    assert len(result) == 1


def test_find_transaction_list_returns_none_for_empty():
    """_find_transaction_list returns None when no transaction-like list found."""
    data = {"data": {"somethingElse": {"notResults": []}}}
    result = _find_transaction_list(data, "https://test")
    assert result is None


# ---------------------------------------------------------------------------
# _map_transaction edge cases
# ---------------------------------------------------------------------------


def test_map_transaction_missing_date_returns_none():
    """_map_transaction returns None when date field is absent."""
    raw = {"id": "x", "amount": 100.0, "merchant": {"name": "Test"}}
    assert _map_transaction(raw) is None


def test_map_transaction_missing_amount_returns_none():
    """_map_transaction returns None when amount field is absent."""
    raw = {"id": "x", "date": "2026-03-01", "merchant": {"name": "Test"}}
    assert _map_transaction(raw) is None


def test_map_transaction_empty_account_dict():
    """_map_transaction handles account={} (missing displayName) → empty string."""
    raw = _raw_txn()
    raw["account"] = {}
    txn = _map_transaction(raw)
    assert txn is not None
    assert txn["account"] == ""


def test_parse_api_responses_skips_non_json():
    """_parse_api_responses silently skips captured responses with non-JSON bodies."""
    captured = [
        {"url": "https://example.com/not-json", "body": "this is not json"},
        {"url": "https://api.monarch.com/graphql", "body": json.dumps(
            {"data": {"allTransactions": {"results": [_raw_txn()]}}}
        )},
    ]
    txns = _parse_api_responses(captured)
    assert len(txns) == 1
