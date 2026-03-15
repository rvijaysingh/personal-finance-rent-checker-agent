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

### Matching confidence principle: only both signals = auto-accept

**Rule** — A payment is auto-accepted (`PAID_ON_TIME` or `PAID_LATE`) only when
**both** the category label and the amount match within tolerance. Any single
signal — correct category but wrong amount, correct amount but wrong category,
or LLM-identified — produces `REVIEW_NEEDED` with a rationale, never auto-acceptance.

**Why** — This prevents silent acceptance of:
- Tenants who short-pay or overpay (category matches but amount differs)
- Miscategorised deposits where Monarch puts the wrong label (amount matches but
  category is "Transfer" or similar)
- LLM suggestions that are plausible but wrong

**Step-level implications:**
- Step 1: category match + amount outside tolerance → `REVIEW_NEEDED` from Step 1.
  Pipeline stops here — Step 2 is NOT reached for that property.
- Step 2: amount match + wrong category → `REVIEW_NEEDED`. Only reached when
  Step 1 found NO category match at all.
- Step 3: any LLM suggestion → `REVIEW_NEEDED`. Never auto-accepted.

**Email format** — `REVIEW_NEEDED` entries show the status label highlighted in
orange, followed by an italic rationale line beneath the property line explaining
what partial signal was found and why human review is needed.

### Pagination: 25 transactions per page, scroll to load more

**Confirmed page size** — Monarch's GraphQL `allTransactions` query returns
exactly 25 transactions per response. The Feb 27 `Rental Income (Links Lane)`
payment was transaction #26 and was never captured by the initial load.

**Correct scroll approach** — JS `scrollTop = scrollHeight` does NOT trigger
pagination (sets position to bottom instantly, no incremental scroll events).
The working approach is `page.mouse.wheel(0, 3000)` after hovering over the
scroll container (`SELECTOR_SCROLL_CONTAINER`). This sends real wheel events
that cause Monarch's virtual list to request the next page via GraphQL.
`End` key is the fallback if mouse wheel fails.

**Scroll implementation** — `_scroll_for_pagination` loops up to
`MAX_SCROLL_ATTEMPTS` (10). Each attempt: hover container → `mouse.wheel(0,
3000)` → wait `SCROLL_WAIT_MS` (5 s) → flush. Stops early once
`_has_old_enough_transactions` finds a date ≤ `lookback_start` in any raw
response body. Stops after `MAX_SCROLL_NO_NEW` (3) consecutive attempts with
zero new responses.

### Direct GraphQL replay via page.evaluate() (not Python HTTP)

Scrolling the Monarch UI does not reliably trigger additional GraphQL API
calls. The correct approach is to intercept the *outgoing* request, capture
the query text, then replay it from inside the browser context using
`page.evaluate()` with a higher limit.

**HTTP 403 from external Python clients** — `urllib`/`requests` calls to
`https://api.monarch.com/graphql` return HTTP 403 even with the correct
`Authorization: Token <token>` header. Monarch validates cookie-based session
fields that an external client cannot replicate. **Use `page.evaluate()` to
run `fetch()` inside the Playwright browser context**, which includes all
cookies and session state automatically.

**page.evaluate() pattern:**
```python
js = """async (body) => {
    const resp = await fetch('https://api.monarch.com/graphql', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
    if (!resp.ok) { return {error: 'HTTP ' + resp.status}; }
    return await resp.json();
}"""
result = page.evaluate(js, graphql_body_dict)  # Playwright awaits async JS
```
Playwright's sync API handles the async JS automatically. The Python dict
argument is serialized to JS; the returned object is deserialized back to Python.

**Confirmed query:** `operationName = "Web_GetTransactionsList"`
**Confirmed endpoint:** `https://api.monarch.com/graphql`
**Confirmed variables for limit=100:**
```json
{"orderBy": "date", "limit": 100,
 "filters": {"transactionVisibility": "non_hidden_transactions_only"}}
```

**Exact operationName match required** — find the source request by
`operationName == "Web_GetTransactionsList"` (exact string), not by fuzzy
search. Fuzzy matching picks up other operations like
`Web_GetTransactionsSummaryCard` which share the endpoint but have different
response shapes.

**Request interception** — `page.on("request", handler)` registered before
navigation. `request.post_data` is synchronous and safe to call inside the
handler (unlike response body reading which must happen outside).

**Fallback: offset-based pagination** — if limit=100 is rejected, fall back
to `{"limit": 25, "offset": 25}`, `{"offset": 50}`, etc. via the same
`page.evaluate()` pattern until the oldest returned date ≤ `lookback_start`
or results are empty.

**HTTP 401 from page.evaluate (missing Authorization header)** — Even when
`fetch()` runs inside the Playwright browser context (which includes session
cookies), Monarch's GraphQL API returns HTTP 401 if the `Authorization: Token
<token>` header is absent. Cookies alone are not sufficient. Fix: capture the
Authorization header from the intercepted `Web_GetTransactionsList` request via
`request.headers` in the `_on_graphql_request` handler, then pass it explicitly
into the JS `fetch()` headers in every `page.evaluate()` call. Both the
limit=100 attempt and all offset-pagination pages must include it.

### Scroll must continue until the lookback window is covered

**Problem** — The initial Monarch API response only returns the most recent N
transactions. A Feb 27 payment (within `early_payment_days` window) was missed
because scrolling stopped before the API returned it.

**Fix** — `_scroll_for_pagination` now accepts `lookback_start: date`. After
each flush it calls `_has_old_enough_transactions`, a lightweight regex scan of
raw response bodies for ISO date strings `<= lookback_start.isoformat()`. When
found, scrolling stops immediately (don't wait for `MAX_SCROLL_NO_NEW`).

`lookback_start` must be computed BEFORE calling `_scroll_for_pagination` so
it can be passed in — it was previously computed only in the post-scroll filter.

### Composite dedup key for transactions without IDs

Transactions without a `"id"` field were not deduplicated across paginated API
responses. Fix: when `id` is absent, use `"date|amount|description"` as the
dedup key in `_parse_api_responses`. ID-based dedup is the fast path; composite
is the fallback.

### step_resolved_by must always be set to 3 inside _step3_llm_match

**Problem** — All early-return paths in `_step3_llm_match` (no candidates, Ollama
unavailable) used `step_resolved_by=None`, which shows as "(unresolved)" in logs
and is indistinguishable from Step 3 never being called.

**Fix** — Every `PropertyResult` returned from `_step3_llm_match` now sets
`step_resolved_by=3`. The "no candidates" note explicitly says Step 3 ran but
had nothing to evaluate.

### Steps 2 and 3 must be scoped to the deposit account

**Problem** — Step 2 searched all accounts by amount. In production this matched
a $3,394.46 "PAYMENT" on "Truist Calmar Mortgage" (an outgoing mortgage payment
to the lender) as a possible rent payment for property 505.

**Fix** — Step 2 and Step 3 both now filter candidates to `t["account"] == prop.account`
before considering a transaction. `prop.account` is the deposit account field
already configured per property (e.g. "Chase Checking ••1230"). Step 1 already
did this; Steps 2 and 3 were missing it.

Rule: all three steps must be scoped to the deposit account. Outgoing payments
(mortgages, expenses) on other accounts are never rent payments.

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

### Other notes
- Monarch's virtualized transaction list does not trigger new
  GraphQL requests on scroll. Direct API replay with captured
  auth token and increased limit is the only reliable way to
  get more than 25 transactions.

## LLM Response Parsing
(Add entries here as you encounter Qwen 3 response format issues)

## Test Infrastructure

### Running the test suite

```bash
python -m pytest tests/ -q               # fast, no output on pass
python -m pytest tests/ -v               # verbose, show each test name
python -m pytest tests/test_transaction_matcher.py -v   # single file
python -m pytest tests/ -k "m01"         # run by name fragment
```

All 148 tests run in ~11 seconds with no network calls. No Ollama, SMTP, or
Playwright required — all external dependencies are mocked.

### Test file inventory

| File | IDs | What it tests |
|---|---|---|
| `test_transaction_matcher.py` | M-01 to M-11 | Three-step matching pipeline |
| `test_llm_client.py` | L-01 to L-06 | `_parse_json_response`, `_step3_llm_match`, notifier fallback |
| `test_notifier.py` | N-01 to N-04 | Subject lines, fallback body, SMTP failure |
| `test_config_loader.py` | C-01 to C-05 | `load_config` error paths |
| `test_run_coordinator.py` | R-01 to R-04 | `_check_already_run`, `_load_run_history`, `_write_run_record` |
| `test_scraper_parsing.py` | S-01 to S-05 | `_parse_api_responses`, `_map_transaction`, `_parse_date` |

### Key patching patterns

**Step 3 (LLM) tests** — must patch BOTH functions or the health check makes
a real network call and the LLM is silently skipped:
```python
@patch("src.transaction_matcher._check_ollama_reachable", return_value=True)
@patch("src.transaction_matcher._call_ollama")
def test_xxx(mock_llm, mock_health):  # closest decorator = first arg
```

**Notifier email content** — `_send_smtp` produces a `MIMEMultipart("alternative")`
message. `msg.as_string()` base64-encodes the HTML body. Assertions against the
email body must decode the MIME message:
```python
from tests.conftest import decode_mime_body
body = decode_mime_body(call_args[0][2])   # call_args[0][2] = 3rd arg to sendmail
assert "expected string" in body.lower()
```

### Step 3 candidate requirement

`_step3_llm_match` filters candidates to `t["amount"] > 0 and t["account"] == prop.account`.
If no positive-amount transactions remain after Steps 1/2 claim their matches,
Step 3 returns MISSING immediately WITHOUT calling `_call_ollama`. Fixtures for
M-02-style tests must include at least one uncategorized positive-amount
transaction so the LLM is actually invoked.

### Account string format in fixtures

Test fixtures and conftest.py use `"Chase Checking \u20221230"` (single bullet:
`•1230`, U+2022). The production `agent_config.json` uses `"••1230"` (two bullets).
This is intentional: test fixtures are self-contained and internally consistent.
If you update the real config account format, update conftest.py to match.

## Windows-Specific Issues
(Add entries here for path handling, Task Scheduler, etc.)