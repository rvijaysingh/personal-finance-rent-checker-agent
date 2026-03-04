"""Three-step hybrid transaction matching pipeline.

Applies matching in order for each property until resolved:
  Step 1 — exact Monarch category label match (deterministic)
  Step 2 — amount fallback on all deposit-account transactions (deterministic)
  Step 3 — LLM review via Ollama for unresolved properties (optional)

Each property exits the pipeline as soon as it is resolved. Step 3 is only
called for properties that Steps 1 and 2 could not match.

Run standalone to test with a fixture file:
    python -m src.transaction_matcher --transactions-file transactions.json
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config_loader import AppConfig

from src.models import PaymentStatus, PropertyConfig, PropertyResult, TransactionRecord

logger = logging.getLogger(__name__)

# Amount tolerance used in Steps 1 and 2. A payment within this percentage
# of the expected rent is considered a match.
AMOUNT_TOLERANCE_PCT = 2.0


class OllamaUnavailableError(Exception):
    """Raised when the Ollama service cannot be reached."""


def match_properties(
    transactions: list[TransactionRecord],
    config: "AppConfig",
) -> list[PropertyResult]:
    """Run the three-step matching pipeline for all configured properties.

    Two-pass design:
      Pass 1 — deterministic Steps 1 and 2 for every property. Collects the
               object IDs of matched transactions so they cannot be reused.
      Pass 2 — LLM Step 3 only for properties not resolved in Pass 1, using
               only transactions that were not matched by another property.

    Args:
        transactions: Transactions for the current window (raw, unfiltered).
        config: Validated application configuration.

    Returns:
        One PropertyResult per property in config.properties, in order.
    """
    today = date.today()
    results: list[PropertyResult | None] = [None] * len(config.properties)
    # Track Python object IDs of transactions claimed by Steps 1 or 2 so
    # they are not offered as candidates to Step 3 for other properties.
    matched_ids: set[int] = set()

    # Pass 1: deterministic matching for all properties.
    for i, prop in enumerate(config.properties):
        logger.info("Matching property %s (Steps 1 and 2)", prop.name)
        result = _match_steps_1_and_2(prop, transactions, today)
        if result is not None:
            results[i] = result
            if result.matched_transaction is not None:
                matched_ids.add(id(result.matched_transaction))
            logger.info(
                "  %s → %s (step %s)",
                prop.name, result.status.value, result.step_resolved_by,
            )

    # Pass 2: LLM fallback for unresolved properties, excluding already-matched
    # transactions so a transaction confirmed for one property is never offered
    # as a candidate for a different property.
    unmatched_txns = [t for t in transactions if id(t) not in matched_ids]
    for i, prop in enumerate(config.properties):
        if results[i] is None:
            logger.info(
                "Matching property %s (Step 3, %d unmatched candidates)",
                prop.name, len(unmatched_txns),
            )
            results[i] = _step3_llm_match(prop, unmatched_txns, config, today)
            logger.info(
                "  %s → %s (step %s)",
                prop.name, results[i].status.value, results[i].step_resolved_by,  # type: ignore[union-attr]
            )

    return [r for r in results if r is not None]


def _match_steps_1_and_2(
    prop: PropertyConfig,
    transactions: list[TransactionRecord],
    check_month: date,
) -> PropertyResult | None:
    """Run Steps 1 and 2 for a single property; return None if neither matches."""
    result = _step1_category_match(prop, transactions, check_month)
    if result is not None:
        return result

    logger.debug("%s: Step 1 found no category match; trying Step 2", prop.name)

    result = _step2_amount_match(prop, transactions, check_month)
    if result is not None:
        return result

    logger.debug("%s: Step 2 found no amount match", prop.name)
    return None


# ---------------------------------------------------------------------------
# Step 1 — Category label match
# ---------------------------------------------------------------------------


def _step1_category_match(
    prop: PropertyConfig,
    transactions: list[TransactionRecord],
    check_month: date,
) -> PropertyResult | None:
    """Find transactions whose category exactly matches the property's label.

    Returns a PropertyResult if one or more matches found, else None.
    """
    matches = [
        t for t in transactions
        if t["category"].strip() == prop.category_label
        and t["account"] == prop.account
    ]

    if not matches:
        return None

    # Flag duplicate payments (multiple category matches in same month)
    duplicate_note = ""
    if len(matches) > 1:
        amounts = [t["amount"] for t in matches]
        duplicate_note = (
            f"WARNING: {len(matches)} category-matched transactions found "
            f"this month (amounts: {amounts}). Manual review recommended. "
        )
        logger.warning(
            "%s: %d category-matched transactions found — possible duplicate payment",
            prop.name, len(matches),
        )

    # Use the first (most recent if sorted by date desc) match
    txn = matches[0]

    # Evaluate amount
    if not _amount_matches(txn["amount"], prop.expected_rent, AMOUNT_TOLERANCE_PCT):
        return PropertyResult(
            property_name=prop.name,
            status=PaymentStatus.WRONG_AMOUNT,
            matched_transaction=txn,
            notes=(
                f"{duplicate_note}"
                f"Expected ${prop.expected_rent:.2f}, received ${txn['amount']:.2f} "
                f"(difference: ${abs(txn['amount'] - prop.expected_rent):.2f}). "
                f"Category: {txn['category']!r}."
            ),
            step_resolved_by=1,
        )

    # Evaluate timeliness
    on_time = _is_on_time(txn["date"], prop.due_day, prop.grace_period_days, check_month)
    status = PaymentStatus.PAID_ON_TIME if on_time else PaymentStatus.PAID_LATE

    deadline = _due_deadline(prop.due_day, prop.grace_period_days, check_month)
    timeliness_note = (
        f"Received {txn['date']} — on time (deadline {deadline})."
        if on_time
        else f"Received {txn['date']} — LATE (deadline was {deadline})."
    )

    return PropertyResult(
        property_name=prop.name,
        status=status,
        matched_transaction=txn,
        notes=f"{duplicate_note}{timeliness_note}",
        step_resolved_by=1,
    )


# ---------------------------------------------------------------------------
# Step 2 — Amount fallback
# ---------------------------------------------------------------------------


def _step2_amount_match(
    prop: PropertyConfig,
    transactions: list[TransactionRecord],
    check_month: date,
) -> PropertyResult | None:
    """Find transactions whose amount matches expected rent (any category).

    Returns a PropertyResult flagged for manual review, else None.
    """
    # Step 2 searches all transactions in the deposit account (not just
    # property account), since miscategorised payments may be on any line.
    matches = [
        t for t in transactions
        if _amount_matches(t["amount"], prop.expected_rent, AMOUNT_TOLERANCE_PCT)
        and t["amount"] > 0  # income only
    ]

    if not matches:
        return None

    if len(matches) > 1:
        logger.warning(
            "%s: Step 2 found %d amount matches — using first, noting ambiguity",
            prop.name, len(matches),
        )

    txn = matches[0]
    multi_note = (
        f"{len(matches)} amount-matching transactions found; first shown. "
        if len(matches) > 1 else ""
    )

    deadline = _due_deadline(prop.due_day, prop.grace_period_days, check_month)

    return PropertyResult(
        property_name=prop.name,
        status=PaymentStatus.POSSIBLE_MATCH,
        matched_transaction=txn,
        notes=(
            f"{multi_note}"
            f"Amount ${txn['amount']:.2f} matches expected ${prop.expected_rent:.2f} "
            f"but category is {txn['category']!r} (not {prop.category_label!r}). "
            f"Received {txn['date']} (deadline {deadline}). "
            f"Transaction: {txn['description']!r}. "
            "MANUAL REVIEW RECOMMENDED — confirm this is the rent payment and "
            "re-categorize in Monarch if so."
        ),
        step_resolved_by=2,
    )


# ---------------------------------------------------------------------------
# Step 3 — LLM fallback
# ---------------------------------------------------------------------------


def _step3_llm_match(
    prop: PropertyConfig,
    transactions: list[TransactionRecord],
    config: "AppConfig",
    check_month: date,
) -> PropertyResult:
    """Ask Ollama whether any transaction could be the rent payment.

    Returns a PropertyResult with status LLM_SUGGESTED, MISSING, or
    LLM_SKIPPED_MISSING (if Ollama is unreachable).
    """
    # Filter out transactions already likely matched to other properties
    # and transactions that are expenses (negative amount)
    candidates = [t for t in transactions if t["amount"] > 0]

    if not candidates:
        return PropertyResult(
            property_name=prop.name,
            status=PaymentStatus.MISSING,
            matched_transaction=None,
            notes=(
                "No positive transactions found in the deposit account for "
                "this month after Steps 1 and 2."
            ),
            step_resolved_by=None,
        )

    prompt_template = config.prompts.get("rent_match", "")
    deadline = _due_deadline(prop.due_day, prop.grace_period_days, check_month)

    # Serialise candidates for the prompt (date as ISO string for LLM)
    candidates_data = [
        {
            "index": i,
            "date": t["date"].isoformat(),
            "description": t["description"],
            "amount": t["amount"],
            "account": t["account"],
            "category": t["category"],
        }
        for i, t in enumerate(candidates)
    ]

    prompt = (
        prompt_template
        .replace("{{property_name}}", prop.name)
        .replace("{{merchant_name}}", prop.merchant_name)
        .replace("{{expected_rent}}", f"{prop.expected_rent:.2f}")
        .replace("{{due_day}}", str(prop.due_day))
        .replace("{{grace_period_days}}", str(prop.grace_period_days))
        .replace("{{transactions_json}}", json.dumps(candidates_data, indent=2))
    )

    logger.debug(
        "Step 3 prompt for %s (%d candidates):\n%s",
        prop.name, len(candidates), prompt,
    )

    try:
        if not _check_ollama_reachable(config.ollama_endpoint):
            raise OllamaUnavailableError(
                f"Health check failed — /api/tags did not respond at "
                f"{config.ollama_endpoint} (5 s timeout)"
            )
        raw_response = _call_ollama(
            config.ollama_endpoint, config.ollama_model, prompt
        )
    except OllamaUnavailableError as exc:
        logger.warning(
            "Ollama unavailable for Step 3 (%s): %s. Marking as LLM_SKIPPED_MISSING.",
            prop.name, exc,
        )
        return PropertyResult(
            property_name=prop.name,
            status=PaymentStatus.LLM_SKIPPED_MISSING,
            matched_transaction=None,
            notes=(
                "LLM check skipped — Ollama unreachable. "
                "Steps 1 and 2 found no match. Manual review required."
            ),
            step_resolved_by=None,
        )

    logger.debug("Step 3 raw response for %s:\n%s", prop.name, raw_response)

    return _interpret_llm_response(prop, raw_response, candidates, deadline)


def _interpret_llm_response(
    prop: PropertyConfig,
    raw_response: str,
    candidates: list[TransactionRecord],
    deadline: date,
) -> PropertyResult:
    """Parse the LLM JSON response and build a PropertyResult."""
    parsed = _parse_json_response(raw_response)

    if parsed is None:
        logger.error(
            "Could not parse LLM response for %s. Raw: %r",
            prop.name, raw_response,
        )
        return PropertyResult(
            property_name=prop.name,
            status=PaymentStatus.MISSING,
            matched_transaction=None,
            notes=(
                "LLM response could not be parsed. "
                f"Raw response: {raw_response[:300]!r}"
            ),
            step_resolved_by=3,
        )

    match_found = parsed.get("match_found", False)
    reasoning = parsed.get("reasoning", "No reasoning provided.")
    indices = parsed.get("transaction_indices", [])
    confidence = parsed.get("confidence", "unknown")

    if not match_found or not indices:
        return PropertyResult(
            property_name=prop.name,
            status=PaymentStatus.MISSING,
            matched_transaction=None,
            notes=f"LLM found no match. Reasoning: {reasoning}",
            step_resolved_by=3,
        )

    # Use the first suggested index
    try:
        matched_txn = candidates[indices[0]]
    except IndexError:
        logger.error(
            "LLM returned out-of-range index %d for %s (have %d candidates)",
            indices[0], prop.name, len(candidates),
        )
        matched_txn = None

    multi_txn_note = ""
    if len(indices) > 1:
        total = sum(candidates[i]["amount"] for i in indices if i < len(candidates))
        multi_txn_note = (
            f"LLM suggests {len(indices)} transactions may be a split payment "
            f"(combined ${total:.2f}). "
        )

    return PropertyResult(
        property_name=prop.name,
        status=PaymentStatus.LLM_SUGGESTED,
        matched_transaction=matched_txn,
        notes=(
            f"{multi_txn_note}"
            f"LLM-suggested match (confidence: {confidence}). "
            f"Reasoning: {reasoning} "
            "MANUAL REVIEW REQUIRED."
        ),
        step_resolved_by=3,
    )


# ---------------------------------------------------------------------------
# Ollama HTTP client
# ---------------------------------------------------------------------------


def _check_ollama_reachable(endpoint: str) -> bool:
    """Return True if Ollama responds to /api/tags within 5 seconds."""
    url = f"{endpoint.rstrip('/')}/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def _call_ollama(endpoint: str, model: str, prompt: str) -> str:
    """Call Ollama generate API and return the response text.

    Args:
        endpoint: Ollama base URL, e.g. 'http://localhost:11434'
        model: Model name, e.g. 'qwen3:8b'
        prompt: Full prompt string.

    Returns:
        LLM response text.

    Raises:
        OllamaUnavailableError: If the service cannot be reached.
    """
    url = f"{endpoint.rstrip('/')}/api/generate"
    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False}
    ).encode("utf-8")

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "")
    except urllib.error.URLError as exc:
        raise OllamaUnavailableError(
            f"Cannot reach Ollama at {endpoint}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise OllamaUnavailableError(
            f"Ollama returned non-JSON response: {exc}"
        ) from exc


def _parse_json_response(text: str) -> dict | None:
    """Extract a JSON object from an LLM response.

    Handles:
      - Clean JSON
      - JSON wrapped in ```json ... ``` or ``` ... ``` fences
      - Leading/trailing prose around a JSON block
    """
    # Strip markdown code fences
    stripped = re.sub(r"```(?:json)?\s*", "", text).strip()
    stripped = re.sub(r"```\s*$", "", stripped).strip()

    # Try direct parse first
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Look for first {...} block
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Date/amount helpers
# ---------------------------------------------------------------------------


def _amount_matches(actual: float, expected: float, tolerance_pct: float) -> bool:
    """Return True if actual is within tolerance_pct of expected."""
    if expected == 0:
        return actual == 0
    return abs(actual - expected) / expected * 100 <= tolerance_pct


def _is_on_time(
    tx_date: date,
    due_day: int,
    grace_period_days: int,
    check_month: date,
) -> bool:
    """Return True if tx_date is on or before the grace period deadline."""
    deadline = _due_deadline(due_day, grace_period_days, check_month)
    return tx_date <= deadline


def _due_deadline(due_day: int, grace_period_days: int, check_month: date) -> date:
    """Compute the last on-time date for a rent payment."""
    base = date(check_month.year, check_month.month, due_day)
    return base + timedelta(days=grace_period_days)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse
    import sys

    from src.config_loader import load_config, ConfigError

    parser = argparse.ArgumentParser(
        description="Run the matching pipeline against a fixture file"
    )
    parser.add_argument(
        "--transactions-file",
        required=True,
        metavar="FILE",
        help="JSON file containing a list of TransactionRecord-compatible dicts",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        raw = json.loads(open(args.transactions_file, encoding="utf-8").read())
    except Exception as exc:
        print(f"Could not load transactions file: {exc}", file=sys.stderr)
        sys.exit(1)

    # Parse date strings to date objects
    txns: list[TransactionRecord] = []
    for row in raw:
        row["date"] = date.fromisoformat(row["date"])
        txns.append(TransactionRecord(**row))  # type: ignore[typeddict-item]

    results = match_properties(txns, cfg)

    print(f"\nMatching results ({date.today().strftime('%B %Y')}):\n")
    for r in results:
        txn_info = ""
        if r.matched_transaction:
            t = r.matched_transaction
            txn_info = f"  → {t['date']}  ${t['amount']:.2f}  {t['description']!r}"
        print(f"  [{r.status.value}] {r.property_name} (step {r.step_resolved_by})")
        if txn_info:
            print(txn_info)
        if r.notes:
            print(f"  Notes: {r.notes}")
        print()
