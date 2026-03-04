# Lessons Learned

This file captures issues encountered during development so they
are not repeated. Claude Code reads this file and should avoid
these known pitfalls.

## Monarch Scraper

### Extraction approach: API response interception (not DOM scraping)

**Why DOM scraping does not work** — Monarch renders its transaction list with a
virtualised list component. Only rows currently visible in the viewport exist
in the DOM. Scrolling removes earlier rows from the DOM as new ones are added.
It is impossible to have all transactions in the DOM simultaneously. Attempting
DOM scraping via Playwright will capture at most one viewport's worth of rows
(~10–15 transactions) regardless of how much you scroll.

**Correct approach: intercept API responses** — Monarch's React app fetches all
transaction data from an internal JSON/GraphQL API. Register a Playwright
response handler (`page.on("response", handler)`) BEFORE navigating. The handler
queues response objects. After page load waits complete, flush the queue by
reading response bodies outside the handler (avoids sync/async issues in
Playwright's event callback context). Scroll to trigger paginated API calls
and flush again after each scroll.

**API endpoint (confirmed)** — Monarch uses GraphQL. Transaction data comes
through GraphQL API responses. In a real run the scraper captured 59 total
JSON responses and extracted 25 unique transactions (17 in the current month).

**Response noise** — `datadoghq.com` responses are analytics/telemetry noise.
They match the JSON content-type filter but contain no transaction data. To
avoid processing them, filter out responses whose URL contains `datadoghq.com`
in `_on_response` before queuing:
```python
if "json" in content_type and "datadoghq.com" not in response.url:
    pending.append(response)
```

**Confirmed JSON transaction structure**:
```json
{
  "data": {
    "allTransactions": {
      "results": [
        {
          "id": "...",
          "date": "2026-03-03",
          "amount": 1500.00,
          "merchant": { "name": "Zelle Payment from Alice" },
          "category": { "name": "Rental Income (Links Lane)" },
          "account": { "displayName": "Chase Checking 1230" }
        }
      ]
    }
  }
}
```
`_TRANSACTION_ARRAY_PATHS` path `["data", "allTransactions", "results"]`
matches. `_map_transaction()` field names (`date`, `amount`, `merchant.name`,
`category.name`, `account.displayName`) are confirmed correct.

**Amount sign convention** — positive = income (credit to account), negative =
expense. `TransactionRecord.amount` follows this convention.

**Two-phase page load selectors (still used for timing)**:
- Phase 1: `[class*='SideBar__Root']` — app shell ready.
- Phase 2: `[class*='TransactionsList__ListContainer']` or
  `[class*='TransactionOverview__Root']` — initial API call returned and React
  has rendered the list. Flush captured responses immediately after phase 2 fires.

**Scroll container** (used to trigger pagination, not for DOM extraction):
```
[class*='Page__ScrollHeaderContainer']
```
Scroll this element to the bottom after phase 2 to trigger any paginated API
calls. Stop scrolling when no new JSON responses arrive for `MAX_SCROLL_NO_NEW`
consecutive scroll attempts.

**If no transactions are found** — check the DEBUG log:
- Are any JSON responses being captured at all? If not, the content-type filter
  or URL is wrong.
- What URLs are being captured? Add the correct URL pattern to the filter.
- What are the top-level keys? Update `_TRANSACTION_ARRAY_PATHS` to match.
- What are the first-transaction keys? Update `_map_transaction()` field names.

## Transaction Matcher

### Two-pass matching is required to prevent cross-property contamination

**Problem** — `match_properties` originally ran all three steps per property in
a single sequential loop. A transaction matched by Step 2 for one property was
still offered as a candidate in Step 3 for the next property. In practice,
"Rental Income (Calmar)" was sent as a Step 3 candidate for Links Lane.

**Fix** — `match_properties` now does two passes:
1. Run Steps 1 and 2 for **all** properties, collecting the Python object IDs
   (`id(txn)`) of every matched transaction.
2. Build `unmatched_txns` by excluding claimed transactions, then run Step 3
   **only** for properties not resolved in pass 1, using only `unmatched_txns`.

This guarantees a transaction confirmed for one property is never offered as a
candidate for another.

### Early payment window

Tenants may pay 1–3 days before the 1st, meaning the transaction lands in the
previous month. `early_payment_days` (agent_config.json, default 3) controls
how many days before the 1st are included. The scraper's date filter is:
```
lookback_start = date(year, month, 1) - timedelta(days=early_payment_days)
```
The matcher's `_is_on_time` already handles this correctly (Feb 26 < Mar 5
deadline → on time). No additional date logic is needed in the matcher.

## Ollama

### First-call model load timeout

Qwen3 8B may take > 120 s to load into GPU memory on the first call after
a cold start. The `_call_ollama` timeout has been raised to 300 s.

### Health check before Step 3

Before committing to a potentially long `_call_ollama` wait, hit `/api/tags`
with a 5 s timeout. If that fails, raise `OllamaUnavailableError` immediately
and mark the property as `LLM_SKIPPED_MISSING` rather than waiting 300 s to
time out. The check is inside the existing `OllamaUnavailableError` try/except
block in `_step3_llm_match`.

## LLM Response Parsing
(Add entries here as you encounter Qwen 3 response format issues)

## Windows-Specific Issues
(Add entries here for path handling, Task Scheduler, etc.)