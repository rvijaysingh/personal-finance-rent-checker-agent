"""Shared data types used across all modules.

Defining types here avoids cross-module coupling: the scraper, matcher,
notifier, and orchestrator all import from this single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import TypedDict


class PaymentStatus(Enum):
    """Outcome of the three-step matching pipeline for a single property."""

    PAID_ON_TIME = "paid_on_time"
    PAID_LATE = "paid_late"
    REVIEW_NEEDED = "review_needed"  # Step 3: LLM found a likely match — human should verify
    MISSING = "missing"              # No match found, or LLM unavailable


class TransactionRecord(TypedDict):
    """A single transaction extracted from Monarch Money.

    amount is always positive for income (credits). The scraper normalises
    the sign: deposits are positive, expenses are negative.
    """

    date: date
    description: str
    amount: float
    account: str
    category: str


@dataclass
class PropertyConfig:
    """Configuration for a single rental property."""

    name: str
    merchant_name: str
    expected_rent: float
    due_day: int              # day of month rent is due (1–28)
    grace_period_days: int    # days after due_day still considered on-time
    category_label: str       # Monarch category label for Step 1 match
    account: str              # Monarch account name for Step 1 scoping


@dataclass
class PropertyResult:
    """Matching outcome for a single property after running all steps."""

    property_name: str
    status: PaymentStatus
    matched_transaction: TransactionRecord | None
    notes: str                # human-readable context for email; for REVIEW_NEEDED includes LLM rationale
    step_resolved_by: int | None  # 1, 2, 3, or None if unresolved
