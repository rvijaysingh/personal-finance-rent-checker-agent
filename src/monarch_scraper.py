"""Monarch Money transaction scraper.

Uses Playwright with a persistent browser profile (pre-authenticated by the
operator) to extract all transactions for the current month from the deposit
account.

The scraper returns raw data only. No business logic or filtering is applied
here — that is the responsibility of transaction_matcher.py.

Selectors in this file are the most likely breakage point. When they break,
document the fix in LESSONS.md and update the constants below.

Run standalone for manual debugging:
    python -m src.monarch_scraper --no-headless
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config_loader import AppConfig

from src.models import TransactionRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Monarch Money URL and selector constants
# UPDATE THESE IN LESSONS.md AND HERE WHEN MONARCH'S UI CHANGES
# ---------------------------------------------------------------------------

MONARCH_TRANSACTIONS_URL = "https://app.monarchmoney.com/transactions"
LOGIN_URL_FRAGMENT = "/login"

# Page load sentinel — wait for this before attempting extraction.
# If this selector breaks, Monarch likely restructured the main nav.
SELECTOR_APP_LOADED = "nav, [role='navigation'], main, [data-testid='sidebar']"

# Transaction list container. If zero rows are found, try inspecting the
# page and updating this selector.
SELECTOR_TRANSACTION_ROW = (
    "[data-testid='transaction-row'], "
    "[class*='TransactionRow'], "
    "[class*='transaction-row']"
)

# Field selectors within each row. These are tried in order; first match wins.
SELECTORS_DATE = [
    "[data-testid='transaction-date']",
    "[class*='TransactionDate']",
    "time",
]
SELECTORS_DESCRIPTION = [
    "[data-testid='transaction-merchant-name']",
    "[data-testid='transaction-description']",
    "[class*='MerchantName']",
    "[class*='merchant']",
]
SELECTORS_AMOUNT = [
    "[data-testid='transaction-amount']",
    "[class*='TransactionAmount']",
    "[class*='amount']",
]
SELECTORS_CATEGORY = [
    "[data-testid='transaction-category']",
    "[class*='CategoryName']",
    "[class*='category']",
]
SELECTORS_ACCOUNT = [
    "[data-testid='transaction-account']",
    "[class*='AccountName']",
    "[class*='account-name']",
]

# Monarch shows ~50 rows by default. Scroll this many times to load more.
MAX_SCROLL_ATTEMPTS = 20
# Sanity bounds — if outside these, something is wrong with extraction.
MIN_EXPECTED_TRANSACTIONS = 1
MAX_EXPECTED_TRANSACTIONS = 500

PAGE_LOAD_TIMEOUT_MS = 30_000
ROW_TIMEOUT_MS = 10_000


class ScraperError(Exception):
    """Raised when the scraper cannot extract transactions."""


def scrape_transactions(
    config: "AppConfig",
    headless_override: bool | None = None,
) -> list[TransactionRecord]:
    """Extract all current-month transactions from Monarch Money.

    Args:
        config: Validated application configuration.
        headless_override: If provided, overrides config.headless.

    Returns:
        List of TransactionRecord for the current month. May include
        transactions from all accounts; the matcher applies account filtering.

    Raises:
        ScraperError: On navigation failure, login redirect, selector
            breakage, or implausible result count.
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

        try:
            page = context.pages[0] if context.pages else context.new_page()
            transactions = _extract_transactions(page, config)
        finally:
            context.close()

    logger.info("Scrape complete: %d transactions extracted", len(transactions))
    return transactions


def _extract_transactions(page: object, config: "AppConfig") -> list[TransactionRecord]:
    """Navigate to Monarch and extract transactions. Internal helper."""
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

    assert isinstance(page, Page)

    # Navigate to transactions page
    logger.info("Navigating to %s", MONARCH_TRANSACTIONS_URL)
    try:
        page.goto(MONARCH_TRANSACTIONS_URL, timeout=PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        raise ScraperError(
            f"Timed out navigating to {MONARCH_TRANSACTIONS_URL}: {exc}. "
            "Check network connectivity."
        ) from exc
    except Exception as exc:
        raise ScraperError(f"Navigation failed: {exc}") from exc

    # Detect session expiry (login redirect)
    current_url = page.url
    if LOGIN_URL_FRAGMENT in current_url:
        raise ScraperError(
            f"Monarch redirected to login page ({current_url!r}). "
            "The browser session has expired. Open the browser manually at "
            f"{config.browser_profile_path} and log in to Monarch Money again."
        )

    # Wait for the app to render
    try:
        page.wait_for_selector(SELECTOR_APP_LOADED, timeout=PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        _dump_page_state(page, "app-load-failure")
        raise ScraperError(
            f"Monarch app did not finish loading (selector: {SELECTOR_APP_LOADED!r}). "
            "This may indicate a Monarch UI change. Check LESSONS.md for selector updates."
        ) from exc

    # Re-check URL after dynamic navigation
    current_url = page.url
    if LOGIN_URL_FRAGMENT in current_url:
        raise ScraperError(
            "Monarch session expired after page load. Manual re-login required."
        )

    logger.info("Monarch loaded, beginning transaction extraction")

    # Scroll to load all current-month transactions
    _scroll_to_load_transactions(page)

    # Extract all visible transaction rows
    transactions = _parse_all_rows(page, config.deposit_account)

    # Filter to current month
    today = date.today()
    current_month_txns = [
        t for t in transactions
        if t["date"].year == today.year and t["date"].month == today.month
    ]

    logger.info(
        "Extracted %d total rows, %d in current month (%d-%02d)",
        len(transactions),
        len(current_month_txns),
        today.year,
        today.month,
    )

    # Sanity check
    if len(current_month_txns) == 0 and len(transactions) > 0:
        logger.warning(
            "Extracted %d total transactions but none match current month %d-%02d. "
            "This may indicate a date parsing failure. Returning all transactions.",
            len(transactions), today.year, today.month,
        )
        return transactions  # Return all so caller can investigate

    if len(current_month_txns) > MAX_EXPECTED_TRANSACTIONS:
        raise ScraperError(
            f"Extracted {len(current_month_txns)} transactions for current month — "
            f"exceeds maximum of {MAX_EXPECTED_TRANSACTIONS}. "
            "This likely indicates a selector is matching unintended elements."
        )

    if len(current_month_txns) == 0:
        _dump_page_state(page, "no-transactions")
        raise ScraperError(
            "No transactions found for the current month. "
            "Possible causes: (1) Monarch UI selector change, (2) session expired, "
            "(3) account has no transactions this month. "
            "Check LESSONS.md for selector updates. Page state saved to logs/."
        )

    return current_month_txns


def _scroll_to_load_transactions(page: object) -> None:
    """Scroll down repeatedly to trigger infinite-scroll loading."""
    from playwright.sync_api import Page

    assert isinstance(page, Page)
    logger.debug("Scrolling to load all transactions")

    previous_height = 0
    for attempt in range(MAX_SCROLL_ATTEMPTS):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)  # allow React to re-render

        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == previous_height:
            logger.debug("Scroll complete after %d attempts (no new content)", attempt + 1)
            break
        previous_height = new_height
    else:
        logger.warning(
            "Reached scroll limit (%d attempts); some older transactions may be missing",
            MAX_SCROLL_ATTEMPTS,
        )


def _parse_all_rows(page: object, deposit_account: str) -> list[TransactionRecord]:
    """Locate all transaction rows on the page and parse each one."""
    from playwright.sync_api import Page

    assert isinstance(page, Page)

    rows = page.query_selector_all(SELECTOR_TRANSACTION_ROW)
    logger.debug("Found %d candidate transaction rows", len(rows))

    transactions: list[TransactionRecord] = []
    failed_rows = 0

    for i, row in enumerate(rows):
        try:
            txn = _parse_row(row, i)
            if txn is not None:
                transactions.append(txn)
        except Exception as exc:
            failed_rows += 1
            logger.debug("Row %d parse failed (skipping): %s", i, exc)

    if failed_rows > 0:
        logger.warning(
            "%d of %d rows failed to parse and were skipped",
            failed_rows,
            len(rows),
        )

    return transactions


def _parse_row(row: object, index: int) -> TransactionRecord | None:
    """Parse a single transaction row element into a TransactionRecord.

    Returns None if the row does not look like a real transaction.
    """
    date_str = _get_text_from_selectors(row, SELECTORS_DATE)
    description = _get_text_from_selectors(row, SELECTORS_DESCRIPTION)
    amount_str = _get_text_from_selectors(row, SELECTORS_AMOUNT)
    category = _get_text_from_selectors(row, SELECTORS_CATEGORY) or ""
    account = _get_text_from_selectors(row, SELECTORS_ACCOUNT) or ""

    # Skip rows missing the essential fields
    if not date_str or not description or not amount_str:
        logger.debug(
            "Row %d skipped — missing fields: date=%r description=%r amount=%r",
            index, date_str, description, amount_str,
        )
        return None

    parsed_date = _parse_date(date_str)
    if parsed_date is None:
        logger.debug("Row %d: could not parse date %r, skipping", index, date_str)
        return None

    amount = _parse_amount(amount_str)
    if amount is None:
        logger.debug("Row %d: could not parse amount %r, skipping", index, amount_str)
        return None

    return TransactionRecord(
        date=parsed_date,
        description=description.strip(),
        amount=amount,
        account=account.strip(),
        category=category.strip(),
    )


def _get_text_from_selectors(element: object, selectors: list[str]) -> str | None:
    """Try each selector in order, return first non-empty text match."""
    for selector in selectors:
        try:
            child = element.query_selector(selector)  # type: ignore[union-attr]
            if child:
                text = child.inner_text().strip()
                if text:
                    return text
        except Exception:
            continue
    return None


def _parse_date(text: str) -> date | None:
    """Parse Monarch's date display into a date object.

    Handles formats:
      - "Mar 3"          (current year implied)
      - "Mar 3, 2026"
      - "2026-03-03"     (ISO)
      - "03/03/2026"     (US)
    """
    text = text.strip()
    today = date.today()

    # ISO format
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        pass

    # US format
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        pass

    # "Mar 3, 2026"
    try:
        return datetime.strptime(text, "%b %d, %Y").date()
    except ValueError:
        pass

    # "March 3, 2026"
    try:
        return datetime.strptime(text, "%B %d, %Y").date()
    except ValueError:
        pass

    # "Mar 3" — assume current year
    try:
        parsed = datetime.strptime(text, "%b %d")
        return parsed.replace(year=today.year).date()
    except ValueError:
        pass

    # "March 3" — assume current year
    try:
        parsed = datetime.strptime(text, "%B %d")
        return parsed.replace(year=today.year).date()
    except ValueError:
        pass

    logger.debug("Unrecognised date format: %r", text)
    return None


def _parse_amount(text: str) -> float | None:
    """Parse Monarch's amount display into a signed float.

    Income (credits) are positive; expenses are negative.
    Monarch typically shows income in green without a leading minus,
    and expenses with a minus or in red. We rely on the sign in the text.
    """
    text = text.strip()

    # Detect negation before stripping symbols
    is_negative = text.startswith("-") or text.endswith("-")

    # Strip currency symbols, commas, spaces
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None

    try:
        value = float(cleaned)
    except ValueError:
        return None

    return -value if is_negative else value


def _dump_page_state(page: object, label: str) -> None:
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
        logger.error(
            "Page state saved to %s (URL: %s)", html_path, page.url
        )
    except Exception as dump_exc:
        logger.debug("Could not dump page state: %s", dump_exc)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    from src.config_loader import load_config, ConfigError

    parser = argparse.ArgumentParser(description="Run Monarch scraper and print results")
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
        txns = scrape_transactions(cfg, headless_override=(not args.no_headless))
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
