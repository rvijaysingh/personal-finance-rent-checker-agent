# Lessons Learned

This file captures issues encountered during development so they
are not repeated. Claude Code reads this file and should avoid
these known pitfalls.

## Monarch Scraper

### Working selectors — verified 2026-03-03

Monarch uses styled-components. Class names have hash suffixes that may change
between deployments (e.g. `SideBar__Root-sc-abc123 ejKmNp`), so all selectors
use `[class*="ComponentName"]` partial matches.

**Page load detection** (wait for one of these before extracting):
```
[class*="SideBar__Root"]
[class*="TransactionsList__ListContainer"]
```
There is NO `<nav>`, `<main>`, `role='navigation'`, or `data-testid='sidebar'`.
The original guess selectors all fail.

**Date / section header** — dates are NOT inside transaction rows. Monarch groups
rows under date section headers that are siblings of the rows:
```
[class*="TransactionsList__StyledSectionHeader"]  →  text: "March 3"
```
Processing order: scan all `SELECTOR_SECTION_HEADER, SELECTOR_TRANSACTION_ROW`
together in document order. Update `current_date` on each header; assign it to
all rows that follow until the next header.

**Transaction row** (one per transaction):
```
[class*="TransactionOverview__Root"]
```

**Fields within a transaction row**:
```
Description / merchant:  [class*="TransactionMerchantSelect"]
Amount:                  [class*="TransactionOverview__Amount"]
Category:                [class*="TransactionOverview__Category"]
Account name:            [class*="TransactionAccount__Name"]
Account container:       [class*="TransactionAccount__Root"]
```

**If selectors break again**: open the browser with `--no-headless`, right-click
a transaction row → Inspect, and look for the `TransactionOverview__` and
`TransactionsList__` component name prefixes in the class attributes.

## LLM Response Parsing
(Add entries here as you encounter Qwen 3 response format issues)

## Windows-Specific Issues
(Add entries here for path handling, Task Scheduler, etc.)