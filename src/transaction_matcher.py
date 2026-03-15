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

    No date filtering is applied here. The scraper already narrows the list
    to the early-payment window (e.g. Feb 26 onward for March rent). The
    matcher searches ALL transactions it receives so that early payments
    landing in the previous month are found by Steps 1 and 2.

    Two-pass design:
      Pass 1 — deterministic Steps 1 and 2 for every property. Collects the
               object IDs of matched transactions so they cannot be reused.
      Pass 2 — LLM Step 3 only for properties not resolved in Pass 1, using
               only transactions that were not matched by another property.

    Args:
        transactions: All transactions in the scraper's date window.
        config: Validated application configuration.

    Returns:
        One PropertyResult per property in config.properties, in order.
    """
    today = date.today()

    # Log the date range the matcher actually received so we can confirm
    # early-payment transactions (e.g. Feb 27) are present before matching.
    if transactions:
        dates = sorted({t["date"] for t in transactions})
        logger.info(
            "Matcher received %d transaction(s), date range %s → %s",
            len(transactions), dates[0], dates[-1],
        )
        early = [t for t in transactions if t["date"].month != today.month]
        if early:
            logger.info(
                "  Including %d transaction(s) from previous month: %s",
                len(early),
                sorted({t["date"] for t in early}),
            )
    else:
        logger.warning("Matcher received zero transactions — all properties will be MISSING")

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
    # Account-filter diagnostic — logged before any step so we can see whether
    # Monarch is appending suffixes (e.g. "...1829") that break exact matching.
    acct_filtered = [t for t in transactions if prop.account in t["account"]]
    logger.debug(
        "Account filter [%s]: %d total transaction(s) → %d match account %r",
        prop.name, len(transactions), len(acct_filtered), prop.account,
    )
    if not acct_filtered and transactions:
        unique_accts = sorted({t["account"] for t in transactions})[:5]
        logger.debug(
            "Account filter [%s]: ZERO matches — first 5 account names in "
            "transaction set: %s",
            prop.name, unique_accts,
        )

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
    # Diagnostic block — visible at DEBUG level (--verbose).
    if transactions:
        txn_dates = sorted({t["date"] for t in transactions})
        logger.debug(
            "Step 1 [%s]: searching %d transaction(s), date range %s → %s  "
            "(NO date filtering — all transactions received from scraper are searched)",
            prop.name, len(transactions), txn_dates[0], txn_dates[-1],
        )
    else:
        logger.debug("Step 1 [%s]: search set is EMPTY — no transactions to search", prop.name)
    logger.debug("Step 1 [%s]: looking for category_label=%r  account=%r",
                 prop.name, prop.category_label, prop.account)
    all_cats = sorted({t["category"] for t in transactions})
    logger.debug("Step 1 [%s]: unique categories in set: %s", prop.name, all_cats)
    all_accts = sorted({t["account"] for t in transactions})
    logger.debug("Step 1 [%s]: unique accounts in set: %s", prop.name, all_accts)

    matches = [
        t for t in transactions
        if t["category"].strip() == prop.category_label
        and prop.account in t["account"]
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

    # Evaluate amount — category match but wrong amount → REVIEW_NEEDED with rationale
    if not _amount_matches(txn["amount"], prop.expected_rent, AMOUNT_TOLERANCE_PCT):
        abs_diff = abs(txn["amount"] - prop.expected_rent)
        pct = abs_diff / prop.expected_rent * 100 if prop.expected_rent != 0 else 0
        sign = "+" if txn["amount"] > prop.expected_rent else "-"
        rationale = (
            f"Category matches ({prop.category_label}) but amount "
            f"${txn['amount']:,.2f} differs from expected "
            f"${prop.expected_rent:,.2f} by {sign}${abs_diff:,.2f} "
            f"({pct:.1f}%, exceeds {AMOUNT_TOLERANCE_PCT:.0f}% tolerance)."
        )
        logger.debug(
            "Step 1 [%s]: category match but amount mismatch → REVIEW_NEEDED. %s",
            prop.name, rationale,
        )
        return PropertyResult(
            property_name=prop.name,
            status=PaymentStatus.REVIEW_NEEDED,
            matched_transaction=txn,
            notes=f"{duplicate_note}{rationale}",
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
    """Find transactions whose amount matches expected rent when no category match exists.

    Scoped to prop.account (same deposit account used by Step 1) so that
    transactions from other accounts (e.g. mortgage payments going out of
    a different account) are never mistaken for incoming rent.

    A pure amount match is a partial signal — the category mismatch means it
    cannot be auto-accepted. Returns REVIEW_NEEDED with a rationale, or None.
    """
    matches = [
        t for t in transactions
        if _amount_matches(t["amount"], prop.expected_rent, AMOUNT_TOLERANCE_PCT)
        and t["amount"] > 0  # income only
        and prop.account in t["account"]
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

    rationale = (
        f"Amount ${txn['amount']:,.2f} matches expected rent "
        f"(within {AMOUNT_TOLERANCE_PCT:.0f}%), but category is "
        f"{txn['category']!r} not {prop.category_label!r}. "
        f"Account: {txn['account']}."
    )
    return PropertyResult(
        property_name=prop.name,
        status=PaymentStatus.REVIEW_NEEDED,
        matched_transaction=txn,
        notes=f"{multi_note}{rationale}",
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
    """Ask an LLM whether any transaction could be the rent payment.

    Uses Anthropic (claude-haiku) as the primary LLM if an API key is
    configured, falling back to Ollama if Anthropic is unavailable or not
    configured. If both are unavailable, returns MISSING.

    Sends ALL unmatched positive-amount transactions to the LLM — no account
    filter. The LLM's job is to find matches the deterministic rules missed,
    including transactions from unexpected accounts or with non-standard
    categorisation.

    Returns a PropertyResult with status REVIEW_NEEDED or MISSING.
    """
    candidates = [t for t in transactions if t["amount"] > 0]

    if not candidates:
        return PropertyResult(
            property_name=prop.name,
            status=PaymentStatus.MISSING,
            matched_transaction=None,
            notes=(
                "Step 3 ran but found no positive-amount transactions to review. "
                "All positive transactions were matched by Steps 1/2 for other "
                "properties, or none were present in the lookback window."
            ),
            step_resolved_by=3,
        )

    prompt_template = config.prompts.get("rent_match", "")
    deadline = _due_deadline(prop.due_day, prop.grace_period_days, check_month)

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
        .replace("{{category_label}}", prop.category_label)
        .replace("{{account}}", prop.account)
        .replace("{{transactions_json}}", json.dumps(candidates_data, indent=2))
    )

    logger.debug(
        "Step 3 prompt for %s (%d candidates):\n%s",
        prop.name, len(candidates), prompt,
    )

    # --- Anthropic primary ---
    raw_response: str | None = None
    if getattr(config, "anthropic_api_key", ""):
        try:
            raw_response = _call_anthropic(
                config.anthropic_api_key, config.anthropic_model, prompt
            )
            logger.debug("Step 3 Anthropic response for %s:\n%s", prop.name, raw_response)
        except Exception as exc:
            logger.warning(
                "Anthropic unavailable for Step 3 (%s): %s. Falling back to Ollama.",
                prop.name, exc,
            )

    # --- Ollama fallback ---
    if raw_response is None:
        try:
            if not _check_ollama_reachable(config.ollama_endpoint):
                raise OllamaUnavailableError(
                    f"Health check failed — /api/tags did not respond at "
                    f"{config.ollama_endpoint} (5 s timeout)"
                )
            raw_response = _call_ollama(
                config.ollama_endpoint, config.ollama_model, prompt
            )
            logger.debug("Step 3 Ollama response for %s:\n%s", prop.name, raw_response)
        except OllamaUnavailableError as exc:
            logger.warning(
                "Ollama unavailable for Step 3 (%s): %s. Marking as MISSING.",
                prop.name, exc,
            )
            return PropertyResult(
                property_name=prop.name,
                status=PaymentStatus.MISSING,
                matched_transaction=None,
                notes=(
                    "LLM check unavailable — Anthropic and Ollama both unreachable. "
                    "Steps 1 and 2 found no match. Manual review required."
                ),
                step_resolved_by=3,
            )

    return _interpret_llm_response(prop, raw_response, candidates, deadline)


def _interpret_llm_response(
    prop: PropertyConfig,
    raw_response: str,
    candidates: list[TransactionRecord],
    deadline: date,
) -> PropertyResult:
    """Parse the LLM JSON response and build a PropertyResult.

    Expected format:
        {
            "status": "likely_match" | "no_match_found",
            "matched_transaction_index": <int or null>,
            "confidence": "high" | "medium" | "low",
            "rationale": "<1-2 sentence explanation>"
        }
    """
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

    status_val = parsed.get("status", "no_match_found")
    rationale = parsed.get("rationale", "No rationale provided.")
    index = parsed.get("matched_transaction_index")
    confidence = parsed.get("confidence", "unknown")

    if status_val != "likely_match" or index is None:
        return PropertyResult(
            property_name=prop.name,
            status=PaymentStatus.MISSING,
            matched_transaction=None,
            notes=f"LLM found no match. Rationale: {rationale}",
            step_resolved_by=3,
        )

    try:
        matched_txn = candidates[index]
    except (IndexError, TypeError):
        logger.error(
            "LLM returned out-of-range or non-integer index %r for %s "
            "(have %d candidates)",
            index, prop.name, len(candidates),
        )
        matched_txn = None

    return PropertyResult(
        property_name=prop.name,
        status=PaymentStatus.REVIEW_NEEDED,
        matched_transaction=matched_txn,
        notes=(
            f"LLM-suggested match (confidence: {confidence}). "
            f"Rationale: {rationale} "
            "HUMAN REVIEW REQUIRED."
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


def _call_anthropic(api_key: str, model: str, prompt: str) -> str:
    """Call Anthropic Messages API and return the response text.

    Args:
        api_key: Anthropic API key.
        model: Model ID, e.g. 'claude-haiku-4-5-20251001'
        prompt: Full prompt string.

    Returns:
        LLM response text.

    Raises:
        Exception: On any HTTP or network failure.
    """
    url = "https://api.anthropic.com/v1/messages"
    payload = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")

    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        content = data.get("content", [])
        for block in content:
            if block.get("type") == "text":
                return block["text"]
        return ""


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
