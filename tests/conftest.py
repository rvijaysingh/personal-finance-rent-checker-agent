"""Shared fixtures and helpers for all test files.

Defines the three canonical property configs, transaction builder helpers,
and fixture loaders used across test_transaction_matcher, test_llm_client,
test_notifier, test_run_coordinator, and test_scraper_parsing.
"""

from __future__ import annotations

import email
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.models import PropertyConfig, TransactionRecord

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Canonical property configs (placeholder names and amounts per task spec)
# ---------------------------------------------------------------------------

PROP_LINKS_LANE = PropertyConfig(
    name="Links Lane",
    merchant_name="JANE DOE",
    expected_rent=2950.00,
    due_day=1,
    grace_period_days=5,
    category_label="Rental Income (Links Lane)",
    account="Chase Checking \u20221230",
)

PROP_CALMAR = PropertyConfig(
    name="Calmar",
    merchant_name="BOB SMITH",
    expected_rent=3100.00,
    due_day=1,
    grace_period_days=5,
    category_label="Rental Income (Calmar)",
    account="Chase Checking \u20221230",
)

PROP_505 = PropertyConfig(
    name="505",
    merchant_name="ALICE JOHNSON",
    expected_rent=2800.00,
    due_day=1,
    grace_period_days=5,
    category_label="Rental Income (505)",
    account="Chase Checking \u20221230",
)

ALL_PROPS = [PROP_LINKS_LANE, PROP_CALMAR, PROP_505]

# March 2026: due_day=1, grace=5 → deadline = March 6.
# On-time: any date ≤ 2026-03-06. Late: ≥ 2026-03-07.
CHECK_MONTH = date(2026, 3, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_txn(
    *,
    txn_date: date = date(2026, 3, 3),
    description: str = "Zelle From JANE DOE",
    amount: float = 2950.00,
    account: str = "Chase Checking \u20221230",
    category: str = "Rental Income (Links Lane)",
) -> TransactionRecord:
    """Build a TransactionRecord with sensible defaults."""
    return TransactionRecord(
        date=txn_date,
        description=description,
        amount=amount,
        account=account,
        category=category,
    )


def make_cfg_mock(properties: list[PropertyConfig] | None = None) -> MagicMock:
    """Create a mock AppConfig suitable for transaction_matcher tests."""
    cfg = MagicMock()
    cfg.properties = properties if properties is not None else list(ALL_PROPS)
    cfg.ollama_endpoint = "http://localhost:11434"
    cfg.ollama_model = "qwen3:8b"
    cfg.prompts = {
        "rent_match": (
            "Evaluate: property={{property_name}} tenant={{merchant_name}} "
            "rent={{expected_rent}} due={{due_day}} grace={{grace_period_days}} "
            "transactions={{transactions_json}}"
        ),
        "payment_summary": "Summary: date={{check_date}} results={{results_json}}",
    }
    cfg.email_subject_prefix = "[Agent - Rent Check]"
    return cfg


def decode_mime_body(mime_string: str) -> str:
    """Decode the HTML body from a MIME email string produced by notifier._send_smtp.

    The notifier builds a MIMEMultipart('alternative') message with an HTML
    MIMEText part. When serialised via msg.as_string() the HTML payload is
    base64-encoded. This helper parses the MIME envelope and returns the
    decoded HTML text for assertion in tests.
    """
    msg = email.message_from_string(mime_string)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode("utf-8")
    # Fallback: return the raw string if no HTML part found
    return mime_string


def load_fixture_raw(filename: str) -> str:
    """Return the raw text content of a fixture file."""
    return (FIXTURE_DIR / filename).read_text(encoding="utf-8")


def load_fixture_json(filename: str) -> object:
    """Return the parsed JSON content of a fixture file."""
    return json.loads(load_fixture_raw(filename))


def load_txn_fixture(filename: str) -> list[TransactionRecord]:
    """Load a JSON array fixture and convert date strings to date objects."""
    rows = load_fixture_json(filename)
    assert isinstance(rows, list)
    txns: list[TransactionRecord] = []
    for row in rows:
        row["date"] = date.fromisoformat(row["date"])
        txns.append(TransactionRecord(**row))  # type: ignore[typeddict-item]
    return txns


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def all_paid_txns() -> list[TransactionRecord]:
    """Three transactions — one per property, all on time."""
    return load_txn_fixture("all_paid.json")


@pytest.fixture
def monarch_api_response() -> dict:
    """Monarch-style GraphQL API response dict for scraper parsing tests."""
    result = load_fixture_json("monarch_api_response.json")
    assert isinstance(result, dict)
    return result


@pytest.fixture
def monarch_page_html() -> str:
    """Placeholder Monarch page HTML for scraper selector tests.

    Replace tests/fixtures/monarch_page.html with a real page dump from
    logs/scraper_*.html for higher-fidelity selector testing.
    """
    return load_fixture_raw("monarch_page.html")
