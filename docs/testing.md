# Test Plan

> Related: [docs/architecture.md](architecture.md) · [docs/risks.md](risks.md)

---

## Testing Boundaries

The Playwright scraper is the live external boundary. Everything downstream
consumes `list[TransactionRecord]` (plain Python dicts) and is fully testable
without a browser.

| Layer | Testability | Approach |
|-------|------------|----------|
| `monarch_scraper.py` — navigation | Not unit-testable | Playwright is mocked entirely |
| `monarch_scraper.py` — parsing | Fully testable offline | Feed captured API response JSON to `_parse_api_responses()` |
| `transaction_matcher.py` | Fully testable | Feed fixture transaction lists; mock Ollama HTTP calls |
| `notifier.py` | Fully testable | Mock `smtplib.SMTP` and Ollama HTTP calls |
| `config_loader.py` | Fully testable | Write fixture config files to `tmp_path` |
| `orchestrator.py` helper functions | Fully testable | Write fixture `run_history.json` to `tmp_path` |

All tests must run in under 5 seconds with **no real network calls**. External
dependencies that must be mocked: Ollama (mock `_call_ollama` and
`_check_ollama_reachable`), Gmail SMTP (mock `smtplib.SMTP`), file system
(use `tmp_path` for run_history files and config files).

---

## Test Case Table

### TRANSACTION MATCHER (`transaction_matcher.py`)

| ID | Scenario | Input | Expected |
|----|----------|-------|----------|
| M-01 | All 3 properties paid on time, correct amounts and categories | 3 transactions with matching categories and amounts, dated March 3 | All `PAID_ON_TIME`, `step_resolved_by=1`, LLM not called |
| M-02 | 1 of 3 paid, 2 missing | 1 matching transaction, 2 properties unresolvable | 1 `PAID_ON_TIME`, 2 `MISSING`, LLM called for unresolved 2 |
| M-03 | Zero transactions in current month | Empty transaction list | All `MISSING`, no crash, LLM not called (no candidates) |
| M-04 | Duplicate Zelle: same category, same amount twice | 2 transactions with identical category and amount | First transaction used; notes contain `"WARNING"` about duplicate |
| M-05 | Two transactions sum to expected rent, neither matches alone | 2 transactions each at 50% of expected rent | Steps 1 and 2 return None for each individually; Step 3 called; LLM may flag as split |
| M-06 | Payment dated Feb 28 in a March run (early payment window) | Transaction with `date=2026-02-28`, correct category | Step 1 matches; status `PAID_ON_TIME` (Feb 28 is before March 6 deadline) |
| M-07 | Payment dated March 7 with 5-day grace period | Transaction with `date=2026-03-07`, due_day=1, grace=5 (deadline=March 6) | Step 1 matches; status `PAID_LATE` |
| M-08 | Category correct, amount wrong ($1,500 vs $2,950) | Transaction with correct category label but wrong amount | Step 1: status `WRONG_AMOUNT`, `step_resolved_by=1` (does not fall through to LLM) |
| M-09 | Amount correct ($2,950), category wrong ("Transfer") | Transaction with correct amount but no category match | Step 1: no match. Step 2: `POSSIBLE_MATCH`, `step_resolved_by=2` |
| M-10 | Transaction claimed by Step 1 for property A; property B needs Step 3 | 1 transaction matching A by category; B has no candidates | A: `PAID_ON_TIME` (step 1). B: `MISSING` (step 3, no candidates because A's txn excluded). LLM not called. |
| M-11 | Category label has leading/trailing whitespace in transaction data | Transaction with `category="  Rental Income (Links Lane)  "` | Step 1 strips whitespace; still matches; status `PAID_ON_TIME` |

### LLM INTEGRATION (`transaction_matcher.py`, `notifier.py`)

| ID | Scenario | Input | Expected |
|----|----------|-------|----------|
| L-01 | Ollama returns valid JSON | Mock returns `{"match_found": true, "transaction_indices": [0], ...}` | `LLM_SUGGESTED`, `matched_transaction` set |
| L-02 | Ollama unreachable for Step 3 and email | `_check_ollama_reachable` returns False; properties need Step 3 | Deterministic matches returned normally; unresolved → `LLM_SKIPPED_MISSING`; email uses Python template |
| L-03 | Ollama returns markdown-fenced JSON | Mock returns ` ```json\n{...}\n``` ` | `_parse_json_response` strips fences and returns dict |
| L-04 | Ollama returns preamble prose + JSON | Mock returns `"Here is my analysis:\n{...}"` | `_parse_json_response` extracts JSON block via regex |
| L-05 | Ollama returns malformed JSON | Mock returns `"not json at all"` | `_parse_json_response` returns None; property marked `MISSING` with raw response in notes |
| L-06 | Ollama returns empty string | Mock returns `""` | `_parse_json_response` returns None → `MISSING`; in notifier, raises `ValueError` → fallback template |

### NOTIFICATION (`notifier.py`)

| ID | Scenario | Input | Expected |
|----|----------|-------|----------|
| N-01 | All paid on time | 3 `PropertyResult` with `PAID_ON_TIME` | Subject contains "All Received"; body contains amounts and dates |
| N-02 | Mixed results (paid + missing + flagged) | Mixed `PropertyResult` statuses | Subject contains "ACTION NEEDED"; email sent successfully |
| N-03 | SMTP failure | `smtplib.SMTP` raises `SMTPException` | `send_notification` returns `False`; no exception propagated |
| N-04 | LLM unavailable for email body | `_call_ollama_for_summary` raises connection error | Fallback body used; contains "LLM review…unavailable"; email still sent (returns `True`) |

### CONFIG LOADING (`config_loader.py`)

| ID | Scenario | Input | Expected |
|----|----------|-------|----------|
| C-01 | Missing `.env.json` | No file at the configured path | `ConfigError` with `"not found"` in message |
| C-02 | Missing `agent_config.json` | No `config/agent_config.json` | `ConfigError` with `"agent config not found"` |
| C-03 | Missing required property field | Property object missing `merchant_name` | `ConfigError` naming the missing field |
| C-04 | Invalid JSON syntax | `.env.json` contains `{not valid json` | `ConfigError` with "not valid JSON"; not a raw Python traceback |
| C-05 | Empty properties array | `properties: []` | `ConfigError` with "non-empty list" |

### RUN COORDINATOR (`orchestrator.py` helper functions)

| ID | Scenario | Input | Expected |
|----|----------|-------|----------|
| R-01 | Already ran successfully this month | `run_history.json` with `"completed"` for current month | `_check_already_run` returns `(True, "completed")` |
| R-02 | Previous run had errors | `run_history.json` with `"error"` for current month | `_check_already_run` returns `(False, None)` — allows re-run |
| R-03 | No `run_history.json` exists | File does not exist at log path | `_load_run_history` returns `[]` without raising |
| R-04 | Corrupted `run_history.json` | File contains `{this is not json` | `_load_run_history` returns `[]` with a warning |

### SCRAPER PARSING (`monarch_scraper.py`, fixture-based)

| ID | Scenario | Input | Expected |
|----|----------|-------|----------|
| S-01 | API response fixture with 3 transactions | `monarch_api_response.json` fed to `_parse_api_responses` | Exactly 3 `TransactionRecord` objects returned |
| S-02 | Amounts parsed as correct floats | Transaction with `amount: 2950.00` in fixture | `t["amount"] == 2950.0` (float, not string) |
| S-03 | Date string "Mar 3, 2026" → `date(2026, 3, 3)` | `_parse_date("Mar 3, 2026")` | Returns `date(2026, 3, 3)` |
| S-04 | Account name extracted from nested dict | Transaction with `account: {"displayName": "Chase Checking ••1230"}` | `t["account"] == "Chase Checking ••1230"` |
| S-05 | Category label extracted from nested dict | Transaction with `category: {"name": "Rental Income (Links Lane)"}` | `t["category"] == "Rental Income (Links Lane)"` |

---

## Fixture File Inventory

| Fixture file | Format | Used by tests |
|-------------|--------|--------------|
| `all_paid.json` | JSON array of TransactionRecords | M-01, N-01 |
| `partial_paid.json` | JSON array (1 match, 2 missing) | M-02, N-02 |
| `empty_month.json` | `[]` | M-03 |
| `duplicate_zelle.json` | JSON array (2 matching txns for same property) | M-04 |
| `split_payment.json` | JSON array (2 half-rent txns) | M-05 |
| `prior_month_payment.json` | JSON array (1 txn dated Feb 28) | M-06 |
| `late_payment.json` | JSON array (1 txn dated March 7) | M-07 |
| `category_mismatch.json` | JSON array (correct category, wrong amount) | M-08 |
| `amount_no_category.json` | JSON array (correct amount, wrong category) | M-09 |
| `ambiguous_match.json` | JSON array (1 txn, could match 2 properties) | M-10 |
| `messy_merchant.json` | JSON array (category with extra whitespace) | M-11 |
| `llm_valid_response.json` | JSON object (valid LLM JSON response) | L-01 |
| `llm_markdown_fenced.txt` | Text (LLM response with ` ```json ``` ` fences) | L-03 |
| `llm_preamble.txt` | Text (LLM response with prose before JSON) | L-04 |
| `llm_invalid.txt` | Text (non-JSON LLM response) | L-05 |
| `agent_config_valid.json` | Valid agent_config.json | C-01 to C-05 baseline |
| `agent_config_missing_field.json` | agent_config missing `merchant_name` | C-03 |
| `run_history_success.json` | JSON array with `"completed"` record | R-01 |
| `run_history_errors.json` | JSON array with `"error"` record | R-02 |
| `run_history_corrupt.json` | Invalid JSON text | R-04 |
| `monarch_api_response.json` | Monarch GraphQL API response structure | S-01 to S-05 |
| `monarch_page.html` | HTML placeholder with Monarch class structure | Manual scraper testing |

**Note on `monarch_page.html`:** This placeholder enables tests to run before a
real Monarch page dump is available. Replace with an actual page dump from
`logs/scraper_*.html` for higher-fidelity selector testing. The scraper's
transaction parsing is tested via `monarch_api_response.json` (the JSON API
response), not via HTML DOM parsing.

---

## Running Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_transaction_matcher.py -v

# Run a specific test by ID keyword
pytest -k "M_01 or M_02" -v

# Run with coverage
pytest --cov=src --cov-report=term-missing
```

All tests run with no real network calls — Ollama and SMTP are mocked.
Tests that use file I/O use `pytest`'s built-in `tmp_path` fixture.
