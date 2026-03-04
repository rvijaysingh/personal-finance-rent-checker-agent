"""Monarch Money transaction scraper.

Uses Playwright with a persistent browser profile (pre-authenticated by the
operator) to extract all transactions for the current month from the deposit
account.

Extraction method: API response interception. Monarch's React app fetches
transaction data from an internal JSON/GraphQL API. This scraper intercepts
those network responses and parses the structured JSON directly — no DOM
scraping is required.

Why not DOM scraping: Monarch renders its transaction list using a virtualised
list component. Only rows currently visible in the viewport exist in the DOM
at any time. Scrolling removes earlier rows. DOM scraping can therefore never
capture a complete month of transactions.

Why API interception is better: the JSON responses contain the full structured
data (date, amount, merchant, category, account) regardless of what is visible.
The approach is also more resilient to Monarch UI changes because it targets
the data layer, not the presentation layer.

Run standalone for manual debugging:
    python -m src.monarch_scraper --no-headless
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.config_loader import AppConfig

from src.models import TransactionRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Monarch Money URL and selector constants
# ---------------------------------------------------------------------------

MONARCH_TRANSACTIONS_URL = "https://app.monarch.com/transactions"
LOGIN_URL_FRAGMENT = "/login"

# Phase 1 load sentinel — sidebar renders before transaction data.
SELECTOR_APP_LOADED = "[class*='SideBar__Root']"

# Phase 2 load sentinel — transaction list container appears once the
# initial API call returns. Waiting for this guarantees the first batch
# of API responses has arrived before we flush captured bodies.
SELECTOR_TRANSACTIONS_LOADED = (
    "[class*='TransactionsList__ListContainer'], "
    "[class*='TransactionOverview__Root']"
)

# Used to scroll the transactions container and trigger paginated API calls.
SELECTOR_SCROLL_CONTAINER = "[class*='Page__ScrollHeaderContainer']"

PAGE_LOAD_TIMEOUT_MS = 30_000

# Scroll pagination: how many scroll attempts before giving up, and how long
# to wait after each scroll for Monarch's API to respond.
MAX_SCROLL_ATTEMPTS = 10
SCROLL_WAIT_MS = 3_000

# Stop pagination scrolling after this many consecutive scrolls with no
# new API responses captured.
MAX_SCROLL_NO_NEW = 3

# Sanity bound on total transactions returned.
MAX_EXPECTED_TRANSACTIONS = 500

# Candidate JSON paths to the transaction array within an API response.
# Tried in order; first path that yields a non-empty list of transaction-
# like objects is used. These cover Monarch's known GraphQL schema variants.
_TRANSACTION_ARRAY_PATHS: list[list[str]] = [
    ["data", "allTransactions", "results"],
    ["data", "getTransactions", "results"],
    ["data", "transactions", "results"],
    ["data", "recentTransactions", "results"],
    ["data", "allTransactions"],
    ["data", "transactions"],
    ["transactions"],
    ["results"],
    ["data"],
]


class ScraperError(Exception):
    """Raised when the scraper cannot extract transactions."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def scrape_transactions(
    config: "AppConfig",
    headless_override: bool | None = None,
) -> list[TransactionRecord]:
    """Extract all current-month transactions from Monarch Money.

    Registers a network response listener BEFORE navigating so that no API
    responses are missed. Navigates to the transactions page, waits for the
    initial data load, scrolls to trigger any paginated API calls, then
    parses all captured JSON responses into TransactionRecord objects.

    Args:
        config: Validated application configuration.
        headless_override: If provided, overrides config.headless.

    Returns:
        List of TransactionRecord for the current month.

    Raises:
        ScraperError: On navigation failure, login redirect, no transactions
            found in any API response, or implausible result count.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScraperError(
            "playwright is not installed. Run: pip install playwright && "
            "playwright install chromium"
        ) from exc

    headless = headless_override if headless_override is not None else config.headless
    profile_path = str(config.browser_profile_path)
    logger.info(
        "Starting Playwright (headless=%s, profile=%s)", headless, profile_path
    )

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                headless=headless,
                slow_mo=50 if not headless else 0,
            )
        except Exception as exc:
            raise ScraperError(
                f"Failed to launch browser with profile at {profile_path!r}: {exc}. "
                "Ensure the profile path exists and was created by running "
                "python -m src.monarch_scraper --no-headless --setup-profile"
            ) from exc

        # Collect response objects in the handler (body not read here to avoid
        # potential deadlocks when calling sync wrappers inside event callbacks).
        pending: list[Any] = []

        def _on_response(response: Any) -> None:
            """Queue JSON API responses for body extraction after page load."""
            content_type = response.headers.get("content-type", "")
            if "json" in content_type and "datadoghq.com" not in response.url:
                pending.append(response)
                logger.debug("Queued JSON response: %s", response.url)

        try:
            page = context.pages[0] if context.pages else context.new_page()

            # Register BEFORE any navigation so the first API calls are captured.
            page.on("response", _on_response)

            captured: list[dict[str, Any]] = []
            transactions = _extract_transactions(page, config, pending, captured)
        finally:
            context.close()

    logger.info("Scrape complete: %d transactions extracted", len(transactions))
    return transactions


# ---------------------------------------------------------------------------
# Internal extraction pipeline
# ---------------------------------------------------------------------------


def _extract_transactions(
    page: Any,
    config: "AppConfig",
    pending: list[Any],
    captured: list[dict[str, Any]],
) -> list[TransactionRecord]:
    """Navigate, wait for API responses, scroll for pagination, parse."""
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

    assert isinstance(page, Page)

    # Navigate to transactions page.
    logger.info("Navigating to %s", MONARCH_TRANSACTIONS_URL)
    try:
        page.goto(MONARCH_TRANSACTIONS_URL, timeout=PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        raise ScraperError(
            f"Timed out navigating to {MONARCH_TRANSACTIONS_URL}. "
            "Check network connectivity."
        ) from exc
    except Exception as exc:
        raise ScraperError(f"Navigation failed: {exc}") from exc

    # Detect login redirect.
    if LOGIN_URL_FRAGMENT in page.url:
        raise ScraperError(
            f"Monarch redirected to login page ({page.url!r}). "
            "The browser session has expired. Open the browser manually and "
            "log in to Monarch Money, then run with --no-headless to re-authenticate."
        )

    # Phase 1: wait for sidebar (app shell rendered).
    try:
        page.wait_for_selector(SELECTOR_APP_LOADED, timeout=PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        _dump_page_state(page, "app-load-failure")
        raise ScraperError(
            f"Monarch app shell did not render (selector: {SELECTOR_APP_LOADED!r}). "
            "This may indicate a Monarch UI change or session expiry."
        ) from exc

    if LOGIN_URL_FRAGMENT in page.url:
        raise ScraperError(
            "Monarch session expired after page load. Manual re-login required."
        )

    # Phase 2: wait for transaction list container to appear.
    # By the time this selector fires, the initial API call has returned and
    # its response object is in `pending`.
    logger.info("App shell loaded; waiting for transaction data")
    try:
        page.wait_for_selector(SELECTOR_TRANSACTIONS_LOADED, timeout=PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        _dump_page_state(page, "transactions-load-failure")
        raise ScraperError(
            "Transaction list did not finish loading. "
            "Check LESSONS.md. Page state saved to logs/."
        ) from exc

    # Flush pending response objects now that the initial API call is complete.
    _flush_pending(pending, captured)
    logger.info(
        "Initial API responses captured: %d total JSON response(s)", len(captured)
    )

    # Scroll to trigger any paginated API calls.
    _scroll_for_pagination(page, pending, captured)

    # Log all captured API URLs for discovery / post-mortem debugging.
    logger.info("Total JSON responses captured: %d", len(captured))
    for i, resp in enumerate(captured):
        logger.debug("  [%d] %s (%d bytes)", i + 1, resp["url"], len(resp["body"]))

    # Parse all captured responses.
    transactions = _parse_api_responses(captured)

    if not transactions:
        _dump_page_state(page, "no-transactions")
        raise ScraperError(
            "No transactions found in any captured API response. "
            "Check DEBUG logs for the list of captured URLs and response structure. "
            "The API endpoint or JSON schema may have changed — update LESSONS.md."
        )

    # Filter to the current month plus the early-payment lookback window.
    # A tenant may pay 1–N days before the 1st, so that transaction lands in
    # the previous month but should still be matched against this month's rent.
    today = date.today()
    lookback_start = date(today.year, today.month, 1) - timedelta(
        days=config.early_payment_days
    )
    in_window = [t for t in transactions if t["date"] >= lookback_start]

    logger.info(
        "Parsed %d total transactions, %d in window (from %s, early_payment_days=%d)",
        len(transactions),
        len(in_window),
        lookback_start,
        config.early_payment_days,
    )

    if not in_window and transactions:
        logger.warning(
            "Parsed %d transactions but none fall within the lookback window "
            "(from %s). This may indicate a date parsing failure. Returning all.",
            len(transactions), lookback_start,
        )
        return transactions

    if len(in_window) > MAX_EXPECTED_TRANSACTIONS:
        raise ScraperError(
            f"Parsed {len(in_window)} transactions in the current window — "
            f"exceeds sanity maximum of {MAX_EXPECTED_TRANSACTIONS}. "
            "Check _find_transaction_list() paths."
        )

    return in_window


def _flush_pending(
    pending: list[Any],
    captured: list[dict[str, Any]],
) -> None:
    """Read body text from queued response objects and move them to captured.

    Bodies are read here (outside event handlers) to avoid sync/async issues.
    Responses whose body cannot be read are silently skipped.
    """
    while pending:
        resp = pending.pop(0)
        try:
            body = resp.text()
            captured.append({"url": resp.url, "body": body})
        except Exception as exc:
            logger.debug("Could not read body for %s: %s", resp.url, exc)


def _scroll_for_pagination(
    page: Any,
    pending: list[Any],
    captured: list[dict[str, Any]],
) -> None:
    """Scroll to the bottom of the transactions list to trigger paginated API calls.

    Monarch may only include the first N transactions in the initial response
    and load more as the user scrolls. Each scroll triggers a new API request.
    Stops when no new JSON responses arrive after MAX_SCROLL_NO_NEW scrolls.
    """
    from playwright.sync_api import Page

    assert isinstance(page, Page)

    js_scroll = (
        f'() => {{ const el = document.querySelector("{SELECTOR_SCROLL_CONTAINER}");'
        f' if (el) {{ el.scrollTop = el.scrollHeight; }}'
        f' else {{ window.scrollTo(0, document.body.scrollHeight); }} }}'
    )

    no_new_count = 0

    for attempt in range(MAX_SCROLL_ATTEMPTS):
        count_before = len(captured) + len(pending)
        page.evaluate(js_scroll)
        page.wait_for_timeout(SCROLL_WAIT_MS)

        # Flush any responses that arrived during the wait.
        _flush_pending(pending, captured)
        count_after = len(captured)

        new_count = count_after - (count_before - len(pending))
        logger.debug(
            "Pagination scroll %d/%d: %d new API response(s) (total: %d)",
            attempt + 1, MAX_SCROLL_ATTEMPTS, new_count, count_after,
        )

        if new_count > 0:
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= MAX_SCROLL_NO_NEW:
                logger.debug(
                    "No new API responses after %d consecutive scrolls — done",
                    MAX_SCROLL_NO_NEW,
                )
                break
    else:
        logger.warning(
            "Reached scroll limit (%d); some older transactions may be missing",
            MAX_SCROLL_ATTEMPTS,
        )


# ---------------------------------------------------------------------------
# API response parsing
# ---------------------------------------------------------------------------


def _parse_api_responses(
    captured: list[dict[str, Any]],
) -> list[TransactionRecord]:
    """Parse all captured JSON API responses and extract TransactionRecords.

    Tries multiple field paths to locate the transaction array because
    Monarch's internal API schema is discovered at runtime on the first run.
    Logs response structure at DEBUG level to support schema identification.
    Deduplicates by transaction id across paginated responses.
    """
    all_transactions: list[TransactionRecord] = []
    seen_ids: set[str] = set()

    for resp in captured:
        url = resp["url"]
        body = resp["body"]

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping non-JSON body from %s: %s", url, exc)
            continue

        if not isinstance(data, dict):
            logger.debug("Skipping non-object JSON from %s", url)
            continue

        # Always log top-level keys — essential for schema discovery on first run.
        logger.debug(
            "Response from %s — top-level keys: %s",
            url, list(data.keys()),
        )

        raw_txns = _find_transaction_list(data, url)
        if not raw_txns:
            continue

        logger.info(
            "Found %d transaction candidate(s) in response from %s",
            len(raw_txns), url,
        )

        # Log the first transaction's structure for field-mapping visibility.
        first = raw_txns[0] if raw_txns and isinstance(raw_txns[0], dict) else None
        if first:
            logger.debug(
                "First transaction — keys: %s | sample: %s",
                list(first.keys()),
                {k: first[k] for k in list(first.keys())[:8]},
            )

        for raw in raw_txns:
            if not isinstance(raw, dict):
                continue
            txn_id = str(raw.get("id", ""))
            if txn_id and txn_id in seen_ids:
                continue
            if txn_id:
                seen_ids.add(txn_id)
            mapped = _map_transaction(raw)
            if mapped is not None:
                all_transactions.append(mapped)

    logger.info(
        "Parsed %d unique transaction(s) from %d captured response(s)",
        len(all_transactions), len(captured),
    )
    return all_transactions


def _find_transaction_list(data: dict, url: str) -> list | None:
    """Search known JSON paths for a list of transaction-like objects.

    Returns the first non-empty list found whose first element contains
    an 'amount' or 'date' field (indicating it looks like transaction data).
    Returns None if no path matches.
    """
    for path in _TRANSACTION_ARRAY_PATHS:
        node: Any = data
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)

        if isinstance(node, list) and node and isinstance(node[0], dict):
            if any(k in node[0] for k in ("amount", "date", "transactionDate")):
                logger.debug(
                    "Transaction list at path %s in %s (%d items)",
                    path, url, len(node),
                )
                return node

    logger.debug("No transaction list found in response from %s", url)
    return None


def _map_transaction(raw: dict) -> TransactionRecord | None:
    """Map a raw JSON transaction object to a TransactionRecord.

    Field names are tried in priority order. Monarch's exact field names
    are confirmed by examining the DEBUG log output on the first run and
    updating this function accordingly.
    """
    # --- Date ---
    date_str = (
        raw.get("date")
        or raw.get("transactionDate")
        or raw.get("createdAt")
        or raw.get("postedDate")
    )
    if not date_str:
        logger.debug("Transaction skipped — no date field. Keys: %s", list(raw.keys()))
        return None
    parsed_date = _parse_date(str(date_str))
    if parsed_date is None:
        logger.debug("Transaction skipped — could not parse date: %r", date_str)
        return None

    # --- Amount ---
    # Monarch convention: positive = income (credit), negative = expense (debit).
    amount_raw = raw.get("amount") if raw.get("amount") is not None else raw.get("value")
    if amount_raw is None:
        logger.debug(
            "Transaction skipped — no amount field. Keys: %s", list(raw.keys())
        )
        return None
    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        # Fall back to string parsing for amounts like "$1,500.00"
        parsed = _parse_amount(str(amount_raw))
        if parsed is None:
            logger.debug("Transaction skipped — could not parse amount: %r", amount_raw)
            return None
        amount = parsed

    # --- Description / merchant ---
    merchant = raw.get("merchant") or raw.get("merchantName") or {}
    if isinstance(merchant, dict):
        description = (
            merchant.get("name") or merchant.get("merchantName") or ""
        )
    else:
        description = str(merchant)
    if not description:
        description = (
            raw.get("description") or raw.get("note") or raw.get("name") or ""
        )
    description = _clean_description(str(description))

    # --- Category ---
    category = raw.get("category") or {}
    if isinstance(category, dict):
        category_name = category.get("name") or ""
    else:
        category_name = str(category)
    category_name = _clean_category(category_name)

    # --- Account ---
    account = raw.get("account") or {}
    if isinstance(account, dict):
        account_name = (
            account.get("displayName")
            or account.get("name")
            or account.get("accountName")
            or ""
        )
    else:
        account_name = str(account)
    account_name = account_name.strip()

    return TransactionRecord(
        date=parsed_date,
        description=description,
        amount=amount,
        account=account_name,
        category=category_name,
    )


# ---------------------------------------------------------------------------
# Field parsing helpers
# ---------------------------------------------------------------------------


def _parse_date(text: str) -> date | None:
    """Parse a date string from an API response into a date object.

    Handles formats:
      - "2026-03-03"   (ISO — primary format in JSON APIs)
      - "March 2, 2026" / "Mar 2, 2026"
      - "03/03/2026"   (US)
    """
    text = text.strip().split("T")[0].strip()  # strip ISO datetime suffix
    today = date.today()

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    # Short formats without year — assume current year.
    for fmt in ("%b %d", "%B %d"):
        try:
            return datetime.strptime(text, fmt).replace(year=today.year).date()
        except ValueError:
            pass

    logger.debug("Unrecognised date format: %r", text)
    return None


def _parse_amount(text: str) -> float | None:
    """Parse a currency string into a signed float (fallback for string amounts)."""
    text = text.strip()
    is_negative = text.startswith("-") or text.endswith("-")
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if is_negative else value


def _clean_description(text: str) -> str:
    """Remove Unicode PUA icon characters and normalise whitespace."""
    cleaned = re.sub(r"[\ue000-\uf8ff]", "", text)
    parts = [p.strip() for p in cleaned.split("\n") if p.strip()]
    return " ".join(parts)


def _clean_category(text: str) -> str:
    """Strip leading emoji prefix and take the first non-empty line."""
    stripped = re.sub(r"^[^a-zA-Z0-9]+", "", text.strip())
    lines = [line.strip() for line in stripped.split("\n") if line.strip()]
    return lines[0] if lines else ""


def _dump_page_state(page: Any, label: str) -> None:
    """Save page HTML and URL to logs/ for post-mortem debugging."""
    from pathlib import Path

    try:
        from playwright.sync_api import Page

        assert isinstance(page, Page)
        logs_dir = Path(__file__).parent.parent / "logs"
        logs_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = logs_dir / f"scraper_{label}_{timestamp}.html"
        html_path.write_text(page.content(), encoding="utf-8")
        logger.error("Page state saved to %s (URL: %s)", html_path, page.url)
    except Exception as dump_exc:
        logger.debug("Could not dump page state: %s", dump_exc)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    from src.config_loader import load_config, ConfigError

    parser = argparse.ArgumentParser(
        description="Run Monarch scraper and print results"
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window (useful for debugging)",
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
        txns = scrape_transactions(
            cfg,
            headless_override=(not args.no_headless),
        )
    except ScraperError as exc:
        print(f"Scraper error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"\nExtracted {len(txns)} transaction(s) for current month:\n")
    for t in txns:
        sign = "+" if t["amount"] >= 0 else ""
        print(
            f"  {t['date']}  {sign}${t['amount']:.2f}  "
            f"{t['description']!r}  [{t['category']}]  ({t['account']})"
        )
