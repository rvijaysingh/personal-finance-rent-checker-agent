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

PAGE_LOAD_TIMEOUT_MS = 30_000

# Sanity bound on total transactions returned.
MAX_EXPECTED_TRANSACTIONS = 500

# Direct GraphQL API endpoint — used to replay the transaction query with a
# higher limit rather than relying on UI-triggered pagination.
MONARCH_GRAPHQL_URL = "https://api.monarch.com/graphql"

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

        # Capture outgoing GraphQL requests to api.monarch.com so we can
        # inspect pagination variables and replay requests directly if needed.
        # Unlike response bodies, request.post_data is safe to read inside
        # the event handler (it is synchronous and does not block).
        graphql_requests: list[dict[str, Any]] = []

        def _on_graphql_request(request: Any) -> None:
            """Capture GraphQL requests for query/variable discovery and replay."""
            if "api.monarch.com/graphql" not in request.url:
                return
            try:
                graphql_requests.append({
                    "url": request.url,
                    "body": request.post_data or "",
                    "headers": dict(request.headers),
                })
                logger.debug("Captured GraphQL request (%d bytes)", len(request.post_data or ""))
            except Exception as exc:
                logger.debug("Could not capture GraphQL request: %s", exc)

        try:
            page = context.pages[0] if context.pages else context.new_page()

            # Register BEFORE any navigation so the first API calls are captured.
            page.on("response", _on_response)
            page.on("request", _on_graphql_request)

            captured: list[dict[str, Any]] = []
            transactions = _extract_transactions(page, config, pending, captured, graphql_requests)
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
    graphql_requests: list[dict[str, Any]],
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

    # Log the captured GraphQL request bodies for diagnostics.
    _log_graphql_requests(graphql_requests)

    today = date.today()
    lookback_start = date(today.year, today.month, 1) - timedelta(
        days=config.early_payment_days
    )

    # Replay Web_GetTransactionsList from inside the browser context via
    # page.evaluate(), with limit=100.  Falls back to offset-based pagination
    # if the server rejects the non-standard limit.
    # _parse_api_responses deduplicates against the initial 25 by transaction ID.
    _fetch_transactions_direct(page, graphql_requests, captured, lookback_start)

    # Log all captured API URLs for discovery / post-mortem debugging.
    logger.info("Total JSON responses captured: %d", len(captured))
    for i, resp in enumerate(captured):
        logger.debug("  [%d] %s (%d bytes)", i + 1, resp["url"], len(resp["body"]))

    # Parse all captured responses.
    transactions = _parse_api_responses(captured)

    # DEBUG: log every unique transaction date so we can see how far back
    # the captured data goes. If the oldest date is after lookback_start
    # we know pagination did not reach the Feb 27 payment.
    if transactions:
        all_dates = sorted({t["date"] for t in transactions})
        logger.info(
            "Date range captured: %s → %s  (%d unique dates, %d total transactions)",
            all_dates[0], all_dates[-1], len(all_dates), len(transactions),
        )
        for d in all_dates:
            count = sum(1 for t in transactions if t["date"] == d)
            logger.debug("  %s: %d transaction(s)", d, count)
        if all_dates[0] > lookback_start:
            logger.warning(
                "OLDEST captured transaction (%s) is newer than lookback_start (%s). "
                "The Feb 27 payment was NOT captured — pagination did not reach it.",
                all_dates[0], lookback_start,
            )

    if not transactions:
        _dump_page_state(page, "no-transactions")
        raise ScraperError(
            "No transactions found in any captured API response. "
            "Check DEBUG logs for the list of captured URLs and response structure. "
            "The API endpoint or JSON schema may have changed — update LESSONS.md."
        )

    # Filter to the lookback window (computed above before scrolling).
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

    # Full transaction dump — visible at DEBUG level (--verbose).
    # One line per transaction so the exact captured set can be verified.
    logger.debug("Full transaction list returned to caller (%d):", len(in_window))
    for t in in_window:
        sign = "+" if t["amount"] >= 0 else ""
        logger.debug(
            "  %s  %s$%.2f  cat=%r  acct=%r  desc=%r",
            t["date"],
            sign,
            abs(t["amount"]),
            t["category"],
            t["account"],
            t["description"],
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


def _eval_graphql(
    page: Any,
    graphql_body: dict[str, Any],
    auth_header: str,
) -> dict[str, Any] | None:
    """Run a GraphQL request inside the browser context via page.evaluate().

    The Authorization header must be passed explicitly — omitting it causes
    HTTP 401 even though the fetch runs inside the browser with valid session
    cookies. Monarch authenticates GraphQL requests via the Authorization
    header, not cookies alone.

    Returns {'status': <int>, 'data': <dict>} on any HTTP response (check
    status == 200 for success), {'error': <str>} on network/JS exception,
    or None if page.evaluate() itself raised.
    """
    js = """async ({body, authHeader}) => {
    try {
        const resp = await fetch('https://api.monarch.com/graphql', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': authHeader
            },
            body: JSON.stringify(body)
        });
        return {status: resp.status, data: await resp.json()};
    } catch (e) {
        return {error: String(e)};
    }
}"""
    try:
        return page.evaluate(js, {"body": graphql_body, "authHeader": auth_header})  # type: ignore[return-value]
    except Exception as exc:
        logger.warning("page.evaluate GraphQL call raised: %s", exc)
        return None


def _fetch_with_offset_pagination(
    page: Any,
    op_name: str,
    query_text: str,
    auth_header: str,
    captured: list[dict[str, Any]],
    lookback_start: date,
) -> bool:
    """Fetch additional transaction pages via offset pagination using page.evaluate().

    Starts at offset=25 (the initial 25 are already in `captured`) and
    increments by 25 per page. Stops when:
      - A response contains a transaction dated <= lookback_start, or
      - A response returns no transactions (end of data), or
      - The 10-page safety limit is hit.

    Returns True if at least one page was successfully appended to `captured`.
    """
    LIMIT = 25
    MAX_PAGES = 10
    cutoff_str = lookback_start.isoformat()
    any_added = False

    for page_num in range(1, MAX_PAGES + 1):
        offset = page_num * LIMIT
        graphql_body = {
            "operationName": op_name,
            "variables": {
                "orderBy": "date",
                "limit": LIMIT,
                "offset": offset,
                "filters": {"transactionVisibility": "non_hidden_transactions_only"},
            },
            "query": query_text,
        }

        logger.info("Offset pagination page %d (offset=%d)", page_num, offset)
        result = _eval_graphql(page, graphql_body, auth_header)

        if result is None or "error" in result:
            err = (result or {}).get("error", "None response")
            logger.warning(
                "  Page %d failed: %s — stopping pagination", page_num, err
            )
            return any_added

        status = result.get("status", 0)
        data = result.get("data", {})
        logger.info("  Page %d: HTTP %d", page_num, status)
        if status != 200:
            logger.warning(
                "  Page %d: HTTP %d — stopping pagination", page_num, status
            )
            return any_added

        url = f"{MONARCH_GRAPHQL_URL}?offset={offset}"
        captured.append({"url": url, "body": json.dumps(data)})
        any_added = True

        raw_txns = _find_transaction_list(data, url) or []
        dates = sorted(
            t.get("date", "") for t in raw_txns
            if isinstance(t, dict) and t.get("date")
        )
        oldest = dates[0] if dates else None
        covered = oldest is not None and oldest <= cutoff_str

        logger.info(
            "  → %d transaction candidate(s), oldest date: %s, covered: %s",
            len(raw_txns), oldest, covered,
        )

        if not raw_txns:
            logger.info("  No transactions on page %d — end of data", page_num)
            return any_added

        if covered:
            logger.info(
                "  Oldest date %s <= lookback_start %s — pagination complete",
                oldest, lookback_start,
            )
            return any_added

    logger.warning(
        "Offset pagination reached safety limit (%d pages) without covering %s",
        MAX_PAGES, lookback_start,
    )
    return any_added


def _fetch_transactions_direct(
    page: Any,
    graphql_requests: list[dict[str, Any]],
    captured: list[dict[str, Any]],
    lookback_start: date,
) -> bool:
    """Fetch all transactions by replaying the GraphQL query from the browser context.

    Uses page.evaluate() so all cookies and session state are included
    automatically. External HTTP clients return HTTP 403.

    The Authorization header is extracted from the captured request and passed
    explicitly into every page.evaluate() call — Monarch requires it even in
    browser context; cookies alone cause HTTP 401.

    Strategy:
      1. Find the captured Web_GetTransactionsList request by exact operationName.
      2. Extract the Authorization header from that request.
      3. Replay with limit=100 to get all recent transactions in one call.
      4. If limit=100 fails, fall back to offset-based pagination (limit=25,
         offset=25 / 50 / 75 / ...) until lookback_start is covered.

    Returns True if at least one additional response was appended to `captured`.
    """
    # Find Web_GetTransactionsList by exact operationName — not a fuzzy match.
    source_req: dict[str, Any] | None = None
    source_body: dict[str, Any] | None = None
    for req in graphql_requests:
        body_str = req.get("body") or ""
        if not body_str:
            continue
        try:
            body = json.loads(body_str)
        except json.JSONDecodeError:
            continue
        if body.get("operationName") == "Web_GetTransactionsList":
            source_req = req
            source_body = body
            break

    if source_body is None:
        all_ops = []
        for r in graphql_requests:
            try:
                all_ops.append(json.loads(r.get("body") or "{}").get("operationName"))
            except json.JSONDecodeError:
                pass
        logger.warning(
            "Direct fetch: Web_GetTransactionsList not found in %d captured "
            "GraphQL request(s). Captured operationNames: %s",
            len(graphql_requests), all_ops,
        )
        return False

    # Extract the Authorization header (case-insensitive) from the source request.
    headers = (source_req or {}).get("headers", {})
    auth_header = next(
        (v for k, v in headers.items() if k.lower() == "authorization"),
        "",
    )
    if not auth_header:
        logger.warning(
            "Direct fetch: Authorization header not found in captured request headers. "
            "Available header keys: %s. Proceeding without auth — may get HTTP 401.",
            list(headers.keys()),
        )
    else:
        logger.debug("Direct fetch: Authorization header captured (%d chars)", len(auth_header))

    op_name = source_body["operationName"]
    query_text = source_body.get("query", "")

    # Attempt 1: single request with limit=100.
    graphql_body_100 = {
        "operationName": op_name,
        "variables": {
            "orderBy": "date",
            "limit": 100,
            "filters": {"transactionVisibility": "non_hidden_transactions_only"},
        },
        "query": query_text,
    }

    logger.info("Direct GraphQL fetch via page.evaluate: op=%r limit=100", op_name)
    result = _eval_graphql(page, graphql_body_100, auth_header)

    if result is not None and "error" not in result:
        status = result.get("status", 0)
        data = result.get("data", {})
        logger.info("Direct fetch (limit=100): HTTP %d", status)
        if status == 200:
            raw_txns = _find_transaction_list(data, MONARCH_GRAPHQL_URL) or []
            dates = sorted(
                t.get("date", "") for t in raw_txns
                if isinstance(t, dict) and t.get("date")
            )
            logger.info(
                "Direct fetch (limit=100) succeeded: %d transaction candidate(s), "
                "date range: %s → %s",
                len(raw_txns),
                dates[0] if dates else "n/a",
                dates[-1] if dates else "n/a",
            )
            captured.append({"url": MONARCH_GRAPHQL_URL + "?limit=100", "body": json.dumps(data)})
            return True
        logger.warning(
            "Direct fetch limit=100 returned HTTP %d — falling back to offset pagination",
            status,
        )
    else:
        err = (result or {}).get("error", "None response")
        logger.warning(
            "Direct fetch limit=100 failed (%s) — falling back to offset pagination",
            err,
        )

    # Attempt 2: offset-based pagination starting at offset=25.
    return _fetch_with_offset_pagination(
        page, op_name, query_text, auth_header, captured, lookback_start
    )


# ---------------------------------------------------------------------------
# GraphQL request discovery
# ---------------------------------------------------------------------------


def _log_graphql_requests(graphql_requests: list[dict[str, Any]]) -> None:
    """Log captured GraphQL request bodies for pagination variable discovery.

    Prints the operationName, variables, and auth header names for every
    request whose body mentions 'allTransactions'. Call this after the
    initial page-load flush so the first-page query is visible in logs.

    Auth header *values* are logged only at DEBUG level to avoid writing
    credentials to INFO logs in production.
    """
    all_txn_reqs = [
        r for r in graphql_requests
        if "allTransactions" in (r.get("body") or "")
    ]

    logger.info(
        "GraphQL requests captured: %d total, %d mention allTransactions",
        len(graphql_requests), len(all_txn_reqs),
    )

    for i, req in enumerate(all_txn_reqs, start=1):
        logger.info("=== allTransactions GraphQL request #%d ===", i)
        body_str = req.get("body") or ""
        try:
            body = json.loads(body_str)
        except json.JSONDecodeError:
            logger.info("  (body is not valid JSON — raw: %s)", body_str[:500])
            continue

        logger.info("  operationName : %s", body.get("operationName", "(none)"))
        variables = body.get("variables", {})
        logger.info("  variables     : %s", json.dumps(variables, indent=4))

        # Log auth header names at INFO; values only at DEBUG.
        headers = req.get("headers", {})
        auth_keys = [k for k in headers if k.lower() in (
            "authorization", "cookie", "x-api-key", "x-monarch-token",
        )]
        logger.info("  auth headers present: %s", auth_keys or "(none found)")
        for k in auth_keys:
            logger.debug("  %s: %s", k, headers[k])

        # Log the query string at DEBUG only — it can be very long.
        query = body.get("query", "")
        logger.debug("  query:\n%s", query)


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

        # Log GraphQL operation names (the keys under "data" in a GraphQL
        # response correspond to the query's operation name). Also log
        # "operationName" if Monarch echoes it back in the response.
        op_name = data.get("operationName")
        data_node = data.get("data")
        if isinstance(data_node, dict):
            logger.debug(
                "  GraphQL data keys (operation names): %s%s",
                list(data_node.keys()),
                f"  operationName={op_name!r}" if op_name else "",
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
                continue  # fast path: skip before mapping
            mapped = _map_transaction(raw)
            if mapped is None:
                continue
            # Use ID as dedup key when available; fall back to a composite
            # key so transactions without IDs are not double-counted across
            # paginated responses.
            if txn_id:
                key = txn_id
            else:
                key = f"{mapped['date']}|{mapped['amount']}|{mapped['description']}"
                if key in seen_ids:
                    continue
            seen_ids.add(key)
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
