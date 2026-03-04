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
MAX_SCROLL_ATTEMPTS = 30
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
                # Navigate first so the user can see whether they need to log in.
                logger.info("Navigating to %s before login pause", MONARCH_TRANSACTIONS_URL)
                try:
                    page.goto(MONARCH_TRANSACTIONS_URL, timeout=PAGE_LOAD_TIMEOUT_MS)
                except Exception as exc:
                    logger.warning("Pre-pause navigation failed (%s); user can navigate manually", exc)
                print(
                    "\nBrowser is open at Monarch Money. "
                    "If you see a login screen, log in now. "
                    "Press Enter once you are on the Monarch app.\n"
                )
                input()
                # Re-navigate after login in case a redirect left us on the login page.
                logger.info("Login pause complete; re-navigating to %s", MONARCH_TRANSACTIONS_URL)
                try:
                    page.goto(MONARCH_TRANSACTIONS_URL, timeout=PAGE_LOAD_TIMEOUT_MS)
                except Exception as exc:
                    logger.warning("Post-pause re-navigation failed: %s", exc)
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

    # Scroll back to top so the first section header appears before its rows
    # in the DOM traversal (Monarch may place the first date header after its
    # rows when the page is scrolled mid-list).
    page.evaluate(
        f'() => {{ const el = document.querySelector("{SELECTOR_SCROLL_CONTAINER}");'
        f' (el || document.body).scrollTo(0, 0); }}'
    )
    page.wait_for_timeout(300)

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
    """Scroll to trigger Monarch's lazy-loading of older transactions.

    Uses both JS scrollTop and page.mouse.wheel() so React's scroll event
    listeners are triggered (JS-only scrollTo does not always fire them).
    Waits for network idle after each scroll so Monarch's API has time to
    return the next batch of rows. Stops when the transaction row count
    stops increasing for MAX_SCROLL_NO_CHANGE consecutive attempts.
    """
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

    assert isinstance(page, Page)

    # Log scroll container info to help diagnose future breakage.
    container_info = page.evaluate(
        f"""() => {{
            const el = document.querySelector("{SELECTOR_SCROLL_CONTAINER}");
            if (!el) return "NOT FOUND";
            const s = window.getComputedStyle(el);
            return `found — overflowY=${{s.overflowY}} scrollHeight=${{el.scrollHeight}} clientHeight=${{el.clientHeight}}`;
        }}"""
    )
    logger.debug("Scroll container [%s]: %s", SELECTOR_SCROLL_CONTAINER, container_info)

    # Hover near the centre of the transactions list for mouse wheel events.
    # Monarch's main content is to the right of the sidebar, so 60% across
    # and 50% down is a safe landing point.
    viewport = page.viewport_size or {"width": 1280, "height": 720}
    hover_x = int(viewport["width"] * 0.6)
    hover_y = int(viewport["height"] * 0.5)
    page.mouse.move(hover_x, hover_y)

    MAX_SCROLL_NO_CHANGE = 3  # stop after this many consecutive no-new-row attempts
    consecutive_no_change = 0

    for attempt in range(MAX_SCROLL_ATTEMPTS):
        rows_before = len(page.query_selector_all(SELECTOR_TRANSACTION_ROW))

        # JS scroll: directly set scrollTop on the container.
        page.evaluate(
            f'() => {{ const el = document.querySelector("{SELECTOR_SCROLL_CONTAINER}");'
            f' if (el) el.scrollTop = el.scrollHeight; }}'
        )
        # Mouse wheel: fires the native wheel event which React's scroll
        # listeners pick up — this is what actually triggers the lazy-load.
        page.mouse.wheel(0, 5000)

        # Wait for Monarch's API request (triggered by scroll) to complete.
        # networkidle = no network activity for 500 ms.
        # Fall back to a fixed wait if the page has background polling that
        # prevents networkidle from ever settling.
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except PlaywrightTimeout:
            page.wait_for_timeout(2000)

        rows_after = len(page.query_selector_all(SELECTOR_TRANSACTION_ROW))
        logger.debug(
            "Scroll attempt %d: rows %d → %d",
            attempt + 1, rows_before, rows_after,
        )

        if rows_after > rows_before:
            consecutive_no_change = 0
        else:
            consecutive_no_change += 1
            logger.debug(
                "No new rows (%d / %d consecutive no-change attempts)",
                consecutive_no_change, MAX_SCROLL_NO_CHANGE,
            )
            if consecutive_no_change >= MAX_SCROLL_NO_CHANGE:
                logger.debug(
                    "Scroll complete after %d attempt(s) — row count stable at %d",
                    attempt + 1, rows_after,
                )
                break
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
    orphan_rows: list[object] = []  # rows seen before the first section header
    transactions: list[TransactionRecord] = []
    failed_rows = 0
    row_index = 0

    for element in all_elements:
        class_attr = element.get_attribute("class") or ""

        if "TransactionsList__StyledSectionHeader" in class_attr:
            date_str = element.inner_text().strip()
            parsed = _parse_date(date_str)
            if parsed:
                if current_date is None and orphan_rows:
                    # Assign rows that appeared before any header to this date.
                    logger.debug(
                        "Assigning %d orphan row(s) before first header to date %s",
                        len(orphan_rows), parsed,
                    )
                    for orphan in orphan_rows:
                        try:
                            txn = _parse_row(orphan, row_index, parsed)
                            if txn is not None:
                                transactions.append(txn)
                        except Exception as exc:
                            failed_rows += 1
                            logger.debug("Orphan row parse failed: %s", exc)
                        row_index += 1
                    orphan_rows = []
                current_date = parsed
                logger.debug("Section header date: %s", current_date)
            else:
                logger.debug("Could not parse section header date: %r", date_str)
            continue

        # Transaction row — buffer if no section header has been seen yet.
        if current_date is None:
            logger.debug("Row buffered as orphan (no section header date seen yet)")
            orphan_rows.append(element)
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
    description_raw = _get_text_from_selectors(row, SELECTORS_DESCRIPTION)
    amount_str = _get_text_from_selectors(row, SELECTORS_AMOUNT)
    category_raw = _get_text_from_selectors(row, SELECTORS_CATEGORY) or ""
    account = _get_text_from_selectors(row, SELECTORS_ACCOUNT) or ""

    if not description_raw or not amount_str:
        logger.debug(
            "Row %d skipped — missing fields: description=%r amount=%r",
            index, description_raw, amount_str,
        )
        return None

    description = _clean_description(description_raw)
    category = _clean_category(category_raw)

    amount = _parse_amount(amount_str)
    if amount is None:
        logger.debug("Row %d: could not parse amount %r, skipping", index, amount_str)
        return None

    return TransactionRecord(
        date=row_date,
        description=description,
        amount=amount,
        account=account.strip(),
        category=category,
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

    Section headers include a daily total on the second line, e.g.:
      "March 2, 2026\n$8,972.11"
    Only the first line is the date. Split on newline before parsing.

    Handles formats:
      - "March 2, 2026"  (full month name with year — primary Monarch format)
      - "Mar 3, 2026"
      - "Mar 3"          (current year implied)
      - "March 3"        (current year implied)
      - "2026-03-03"     (ISO)
      - "03/03/2026"     (US)
    """
    text = text.strip().split("\n")[0].strip()
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


def _clean_description(text: str) -> str:
    """Remove icon characters and normalize whitespace from a description.

    Monarch's TransactionMerchantSelect element contains icon glyphs from
    Private Use Area fonts (e.g. \uf156, \uf110, \uf104) mixed with the
    merchant name text. Strip all Unicode PUA characters (U+E000–U+F8FF),
    then collapse the remaining newline-separated parts into a single string.
    """
    # Remove Unicode Private Use Area characters (icon fonts like Font Awesome)
    cleaned = re.sub(r"[\ue000-\uf8ff]", "", text)
    # Split on newlines, discard empty segments, rejoin
    parts = [p.strip() for p in cleaned.split("\n") if p.strip()]
    return " ".join(parts)


def _clean_category(text: str) -> str:
    """Strip leading emoji prefix, trailing newlines, and sub-item lines.

    Monarch prepends emoji to category names, e.g. "🏦Mortgage Payment (505)".
    The element may also contain trailing newlines or additional text nodes
    after a newline (e.g. "Transfer\n\n" or "Rental Income\nSub-label").
    Strip leading non-ASCII/non-alphanumeric chars, then take the first
    non-empty line only.
    """
    # Strip leading emoji / icon characters
    stripped = re.sub(r"^[^a-zA-Z0-9]+", "", text.strip())
    # Take only the first non-empty line (discard trailing newlines or sub-items)
    lines = [line.strip() for line in stripped.split("\n") if line.strip()]
    return lines[0] if lines else ""


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
