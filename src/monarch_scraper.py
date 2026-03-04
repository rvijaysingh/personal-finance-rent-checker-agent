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

MONARCH_TRANSACTIONS_URL = "https://app.monarch.com/transactions"
LOGIN_URL_FRAGMENT = "/login"

# Phase 1 load sentinel — sidebar renders before transaction data.
# Monarch uses styled-components; there is no <nav>, <main>, or role='navigation'.
SELECTOR_APP_LOADED = "[class*='SideBar__Root']"

# Phase 2 load sentinel — wait for actual transaction data to replace
# the loading skeleton. TransactionsListLoading__Root is shown while
# fetching; TransactionsList__ListContainer appears once data is ready.
SELECTOR_TRANSACTIONS_LOADED = (
    "[class*='TransactionsList__ListContainer'], "
    "[class*='TransactionOverview__Root']"
)

# The transactions list scrolls inside this container, not document.body.
# Must use JS scrollTo on this element, not window.scrollTo.
SELECTOR_SCROLL_CONTAINER = "[class*='Page__ScrollHeaderContainer']"

# Section headers separate transaction rows by date (e.g. "March 3").
# The date is on the HEADER, not inside the row. Headers and rows are
# siblings in the list container and must be processed in document order.
SELECTOR_SECTION_HEADER = "[class*='TransactionsList__StyledSectionHeader']"

# Individual transaction row.
SELECTOR_TRANSACTION_ROW = "[class*='TransactionOverview__Root']"

# Field selectors within a transaction row. Date is omitted — it comes
# from the section header above the row, not from the row itself.
SELECTORS_DESCRIPTION = [
    "[class*='TransactionMerchantSelect']",
]
SELECTORS_AMOUNT = [
    "[class*='TransactionOverview__Amount']",
]
SELECTORS_CATEGORY = [
    "[class*='TransactionOverview__Category']",
]
SELECTORS_ACCOUNT = [
    "[class*='TransactionAccount__Name']",
    "[class*='TransactionAccount__Root']",
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
    login_pause: bool = False,
) -> list[TransactionRecord]:
    """Extract all current-month transactions from Monarch Money.

    Args:
        config: Validated application configuration.
        headless_override: If provided, overrides config.headless.
        login_pause: If True, open the browser then wait for the user to
            press Enter before navigating. Use with --no-headless to log in
            manually on a new machine or after session expiry.

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
            if login_pause:
                print(
                    "Browser open. Log in to Monarch Money manually, "
                    "then press Enter to continue."
                )
                input()
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

    # Phase 1: wait for the sidebar — confirms the app shell has rendered.
    try:
        page.wait_for_selector(SELECTOR_APP_LOADED, timeout=PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        _dump_page_state(page, "app-load-failure")
        raise ScraperError(
            f"Monarch app shell did not render (selector: {SELECTOR_APP_LOADED!r}). "
            "This may indicate a Monarch UI change. Check LESSONS.md for selector updates."
        ) from exc

    # Re-check URL after dynamic navigation
    current_url = page.url
    if LOGIN_URL_FRAGMENT in current_url:
        raise ScraperError(
            "Monarch session expired after page load. Manual re-login required."
        )

    # Phase 2: wait for transaction data to replace the loading skeleton.
    # TransactionsListLoading__Root is shown while data fetches; this selector
    # fires only once actual rows or the list container appear.
    logger.info("App shell loaded; waiting for transaction data to render")
    try:
        page.wait_for_selector(SELECTOR_TRANSACTIONS_LOADED, timeout=PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        _dump_page_state(page, "transactions-load-failure")
        raise ScraperError(
            "Transaction list did not finish loading "
            f"(selector: {SELECTOR_TRANSACTIONS_LOADED!r}). "
            "Monarch may still be fetching data — try increasing PAGE_LOAD_TIMEOUT_MS."
        ) from exc

    logger.info("Transaction data rendered, beginning extraction")

    # Scroll to load all current-month transactions
    _scroll_to_load_transactions(page)

    # Extract all visible transaction rows
    transactions = _parse_all_rows(page)

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
    """Scroll the transaction list container to trigger infinite-scroll loading.

    Monarch's transactions scroll inside Page__ScrollHeaderContainer, not
    document.body. Scrolling window.scrollTo has no effect.
    """
    from playwright.sync_api import Page

    assert isinstance(page, Page)

    # Verify the scroll container exists; fall back to body if not found.
    container_found = page.query_selector(SELECTOR_SCROLL_CONTAINER) is not None
    if not container_found:
        logger.warning(
            "Scroll container %r not found — falling back to document.body. "
            "Update SELECTOR_SCROLL_CONTAINER if transactions are truncated.",
            SELECTOR_SCROLL_CONTAINER,
        )

    js_scroll = (
        f'() => {{'
        f' const el = document.querySelector("{SELECTOR_SCROLL_CONTAINER}");'
        f' (el || document.body).scrollTo(0, (el || document.body).scrollHeight);'
        f' }}'
    )
    js_height = (
        f'() => {{'
        f' const el = document.querySelector("{SELECTOR_SCROLL_CONTAINER}");'
        f' return (el || document.body).scrollHeight;'
        f' }}'
    )

    logger.debug(
        "Scrolling %s to load all transactions",
        SELECTOR_SCROLL_CONTAINER if container_found else "document.body",
    )

    previous_height = 0
    for attempt in range(MAX_SCROLL_ATTEMPTS):
        page.evaluate(js_scroll)
        page.wait_for_timeout(800)  # allow React to re-render

        new_height = page.evaluate(js_height)
        if new_height == previous_height:
            logger.debug("Scroll complete after %d attempts (no new content)", attempt + 1)
            break
        previous_height = new_height
    else:
        logger.warning(
            "Reached scroll limit (%d attempts); some older transactions may be missing",
            MAX_SCROLL_ATTEMPTS,
        )


def _parse_all_rows(page: object) -> list[TransactionRecord]:
    """Locate all section headers and transaction rows, parse each row.

    Monarch groups rows under date section headers
    (TransactionsList__StyledSectionHeader). The date is on the header, not
    inside the row. Headers and rows are queried together so document order
    is preserved; the current date is updated whenever a header is seen.
    """
    from playwright.sync_api import Page

    assert isinstance(page, Page)

    # Log individual selector counts to help diagnose partial failures.
    header_count = len(page.query_selector_all(SELECTOR_SECTION_HEADER))
    row_count = len(page.query_selector_all(SELECTOR_TRANSACTION_ROW))
    logger.debug(
        "Selector counts: section_headers=%d, transaction_rows=%d",
        header_count, row_count,
    )
    for sel, label in [
        (SELECTORS_DESCRIPTION[0], "description"),
        (SELECTORS_AMOUNT[0], "amount"),
        (SELECTORS_CATEGORY[0], "category"),
        (SELECTORS_ACCOUNT[0], "account"),
    ]:
        count = len(page.query_selector_all(sel))
        logger.debug("  %-15s selector (%s): %d elements", label, sel, count)

    # Combined selector preserves document order across both element types.
    all_elements = page.query_selector_all(
        f"{SELECTOR_SECTION_HEADER}, {SELECTOR_TRANSACTION_ROW}"
    )
    logger.debug("Combined query returned %d elements total", len(all_elements))

    current_date: date | None = None
    transactions: list[TransactionRecord] = []
    failed_rows = 0
    row_index = 0

    for element in all_elements:
        class_attr = element.get_attribute("class") or ""

        if "TransactionsList__StyledSectionHeader" in class_attr:
            date_str = element.inner_text().strip()
            parsed = _parse_date(date_str)
            if parsed:
                current_date = parsed
                logger.debug("Section header date: %s", current_date)
            else:
                logger.debug("Could not parse section header date: %r", date_str)
            continue

        # Transaction row — skip if no section header has been seen yet.
        if current_date is None:
            logger.debug("Row skipped — no section header date seen yet")
            continue

        try:
            txn = _parse_row(element, row_index, current_date)
            if txn is not None:
                transactions.append(txn)
        except Exception as exc:
            failed_rows += 1
            logger.debug("Row %d parse failed (skipping): %s", row_index, exc)
        row_index += 1

    if failed_rows > 0:
        logger.warning(
            "%d of %d rows failed to parse and were skipped",
            failed_rows,
            row_index,
        )

    return transactions


def _parse_row(row: object, index: int, row_date: date) -> TransactionRecord | None:
    """Parse a single transaction row element into a TransactionRecord.

    The date is supplied by the caller from the section header above this row.
    Returns None if the row does not look like a real transaction.
    """
    description = _get_text_from_selectors(row, SELECTORS_DESCRIPTION)
    amount_str = _get_text_from_selectors(row, SELECTORS_AMOUNT)
    category = _get_text_from_selectors(row, SELECTORS_CATEGORY) or ""
    account = _get_text_from_selectors(row, SELECTORS_ACCOUNT) or ""

    if not description or not amount_str:
        logger.debug(
            "Row %d skipped — missing fields: description=%r amount=%r",
            index, description, amount_str,
        )
        return None

    amount = _parse_amount(amount_str)
    if amount is None:
        logger.debug("Row %d: could not parse amount %r, skipping", index, amount_str)
        return None

    return TransactionRecord(
        date=row_date,
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
        txns = scrape_transactions(
            cfg,
            headless_override=(not args.no_headless),
            login_pause=args.no_headless,
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
