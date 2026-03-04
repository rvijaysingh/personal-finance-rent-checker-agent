# Lessons Learned

This file captures issues encountered during development so they
are not repeated. Claude Code reads this file and should avoid
these known pitfalls.

## Monarch Scraper

### Working selectors and structure — verified 2026-03-03

**Base URL** — Monarch redirects to `app.monarch.com`, not `app.monarchmoney.com`:
```
https://app.monarch.com/transactions
```

**Styled-components pattern** — no `data-testid` attributes anywhere. Class names
carry a hash suffix that changes per Monarch build (e.g.
`TransactionOverview__Root-sc-6s1ps1-1 abcXYZ`). Always use `[class*="Name"]`
partial matching.

**Two-phase page load** — the page renders in two stages:
1. Sidebar appears first (`SideBar__Root`) — app shell is ready.
2. Transactions fetch from the API and replace a loading skeleton
   (`TransactionsListLoading__Root`). Must wait for `TransactionsList__ListContainer`
   or `TransactionOverview__Root` before attempting extraction. Extracting
   after phase 1 only yields 0 rows (skeleton, not real data).

**Scroll container** — transactions scroll inside `Page__ScrollHeaderContainer`
(which has `overflow-y: scroll`), NOT `document.body`. Using `window.scrollTo`
or `document.body.scrollHeight` has no effect. Use JS to target the container:
```js
const el = document.querySelector("[class*='Page__ScrollHeaderContainer']");
(el || document.body).scrollTo(0, (el || document.body).scrollHeight);
```

**Date headers** — dates are NOT inside transaction rows. Monarch groups rows
under section header siblings:
```
[class*="TransactionsList__StyledSectionHeader"]
```
The header's `inner_text()` includes a daily total on the second line:
```
"March 2, 2026\n$8,972.11"
```
Split on `\n` and parse the **first line only**. Date format is
`"March 2, 2026"` — full month name with year, matching `strptime("%B %d, %Y")`.
Failing to split causes `_parse_date` to return `None` for every header,
`current_date` stays `None`, all rows are skipped, and 0 transactions
are returned (triggering the `no-transactions` failure dump).

Process headers and rows together in document order; propagate `current_date`
from each header to subsequent rows until the next header.

**Transaction row and field selectors**:
```
Row:          [class*="TransactionOverview__Root"]
Description:  [class*="TransactionMerchantSelect"]
Amount:       [class*="TransactionOverview__Amount"]
Category:     [class*="TransactionOverview__Category"]
Account name: [class*="TransactionAccount__Name"]
Account root: [class*="TransactionAccount__Root"]
List wrapper: [class*="TransactionsList__ListContainer"]
```

**Amount formats found**: `-1.25`, `+$2,950.00`, `$8,972.11`
`_parse_amount` handles all of these (strips symbols, detects leading `-`).

**Description field contains icon Unicode** — `TransactionMerchantSelect`
`inner_text()` includes icon-font glyphs from the Unicode Private Use Area
(e.g. `\uf156`, `\uf110`, `\uf104`), mixed with the merchant name and
newlines. Raw example: `'\uf156\nPAYMENT\n\uf110\n\uf104'`. Strip all
characters in range U+E000–U+F8FF, then split on newlines and rejoin.
`_clean_description()` handles this.

**Category text has emoji prefix and trailing newline** — category
`inner_text()` returns e.g. `"🏦Mortgage Payment (505)\n"`. Emoji prefixes
seen: 🏦, 🍏, 💳, ❓, 🔁. Category names always start with an ASCII letter;
strip leading non-ASCII/non-alphanumeric characters and trailing whitespace.
`_clean_category()` handles this.

**Lazy-load scroll strategy** — JS `scrollTo`/`scrollHeight` change detection
alone is not sufficient. Monarch's React scroll listeners also need a native
wheel event to trigger the API call that fetches more transactions. Use both:
1. `el.scrollTop = el.scrollHeight` (JS direct)
2. `page.mouse.wheel(0, 5000)` (native wheel event at viewport 60%×50%)
3. `page.wait_for_load_state("networkidle", timeout=4000)` then fallback 2s wait
Stop when transaction row count stops increasing for 3 consecutive attempts
(not scrollHeight — height may not change if virtual DOM is used).

**Account name formats found**: `"Chase Checking 1230"`,
`"Total Checking (First Republic) (...1829)"`.

**If selectors break again**: run `--no-headless`, right-click a transaction
row → Inspect, search for `TransactionOverview__` and `TransactionsList__`
component name prefixes in the class attributes. Update constants in
`monarch_scraper.py` and this file.

**Browser navigation with `--no-headless`** — when `login_pause=True`:
1. Navigate to the transactions URL **before** the prompt so the user can see
   whether they need to log in (browser should not open to a blank page).
2. Print the login prompt and call `input()`.
3. After Enter, **re-navigate** to the transactions URL in case a login redirect
   left the page elsewhere.
4. Then call `_extract_transactions()` which navigates a final time — harmless
   since the user is now authenticated.

## LLM Response Parsing
(Add entries here as you encounter Qwen 3 response format issues)

## Windows-Specific Issues
(Add entries here for path handling, Task Scheduler, etc.)