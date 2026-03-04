# Risks and Mitigations

> Related: [docs/architecture.md](architecture.md) · [docs/config.md](config.md)

---

## Risk Index

| # | Risk | Likelihood |
|---|------|-----------|
| R1 | Monarch API schema change | High |
| R2 | Monarch session expiry | Medium |
| R3 | Ollama unavailable | Medium |
| R4 | Transaction categorisation missing | Low |
| R5 | Duplicate or split payments | Low |
| R6 | Email delivery failure | Low |
| R7 | run_history.json corrupted | Low |
| R8 | Step 2/3 matches wrong account | Low |
| R9 | LLM returns malformed JSON | Medium |

---

## R1: Monarch API Schema Change

**Likelihood:** High — Monarch changes their UI without notice.

**What goes wrong:** Playwright selectors fail to find the transaction list, or
`_map_transaction()` extracts wrong/empty fields (e.g., amounts always 0,
categories always empty). The scraper may silently return stale or empty data.

**Mitigations:**
- Scraper validates result count: if zero transactions are found in all captured
  API responses, raise `ScraperError` rather than returning an empty list.
- Sanity maximum: if `len(in_window) > MAX_EXPECTED_TRANSACTIONS` (500), raise
  `ScraperError` — the schema change may be producing garbage paths.
- All working selectors and JSON field paths are documented in `LESSONS.md` so
  fixes are fast: update `_TRANSACTION_ARRAY_PATHS` and `_map_transaction()`.
- Orchestrator catches `ScraperError`, sends an error notification email, and
  writes `overall_status: "error"` — the next invocation will retry.

**Diagnosis:** Run with `--no-headless --verbose`. Check DEBUG logs for:
- URLs of captured JSON responses
- Top-level keys and transaction field names
Update `_TRANSACTION_ARRAY_PATHS` and `_map_transaction()` field names to match.

---

## R2: Monarch Session Expiry

**Likelihood:** Medium — sessions expire on inactivity or forced re-auth.

**What goes wrong:** Playwright navigates to a login redirect instead of the
transactions page. Without detection, the scraper returns empty data or
misidentifies login-page elements as transactions.

**Mitigations:**
- Scraper checks `page.url` after navigation and after `wait_for_selector`. If
  `LOGIN_URL_FRAGMENT` (`"/login"`) is in the URL, raise `ScraperError` with
  the message "session expired — manual re-login required."
- Do NOT attempt automated login (credentials and MFA are too fragile).
- Orchestrator sends error notification so operator can manually re-login.

---

## R3: Ollama Unavailable

**Likelihood:** Medium — local service may be down, model not loaded, or cold start
timeout exceeded.

**What goes wrong:** Step 3 matching hangs or fails; email body falls back to
plain template; operator receives a less useful summary.

**Mitigations:**
- Orchestrator warms up Ollama before scraping (a small prompt forces model into
  GPU memory, preventing cold-start delays during Step 3).
- Health check (`/api/tags`, 5 s timeout) before each Step 3 call. If it fails,
  raise `OllamaUnavailableError` immediately rather than waiting 300 s to time out.
- Steps 1 and 2 are fully deterministic and run without Ollama. Only Step 3 and
  email generation require it.
- If Ollama is unreachable: Step 3 properties → `LLM_SKIPPED_MISSING`.
  Email body → Python fallback template. Email still sent.
- Note in email body: "LLM review and email generation were unavailable — showing
  raw results only."
- First-call model load can exceed 120 s on a cold start. `_call_ollama` timeout
  is 300 s to accommodate this.

---

## R4: Transaction Categorisation Missing

**Likelihood:** Low — Monarch may miscategorise or not categorise new payment methods.

**What goes wrong:** Step 1 (category match) fails for a property even though
the payment arrived. Without a fallback, the operator is incorrectly notified
that rent is missing.

**Mitigations:**
- Step 2 (amount fallback) and Step 3 (LLM review) exist specifically for this
  scenario. All three steps are scoped to the deposit account.
- Email clearly indicates which step resolved each property so the operator
  knows when categorisation was missing (Step 2 or Step 3 result).
- Step 2 results are flagged "MANUAL REVIEW RECOMMENDED" so the operator can
  re-categorise the transaction in Monarch.

---

## R5: Duplicate or Split Payments

**Likelihood:** Low — tenant pays in two instalments, or a duplicate transaction
appears (bank processing error).

**What goes wrong:** Step 1 finds two category-matched transactions for the same
property; or two small transactions sum to the expected rent but neither matches
individually.

**Mitigations:**
- Step 1: if multiple category-matched transactions are found in the same month,
  the result notes contain `"WARNING: N category-matched transactions found"`.
  First transaction is used; operator can verify.
- Step 3 (LLM): prompt instructs the model to flag multiple transactions that
  could be a split payment. When `transaction_indices` has more than one entry,
  the result notes include "LLM suggests N transactions may be a split payment
  (combined $X.XX)."

---

## R6: Email Delivery Failure

**Likelihood:** Low — Gmail app password expires, rate limit, or transient error.

**What goes wrong:** The check succeeded and matching results are ready, but the
operator never receives the summary. Naive failure handling either drops the
results or causes unnecessary re-scraping.

**Mitigations:**
- `send_notification` catches all SMTP exceptions, logs the full error
  (recipient, subject, host, error message), and returns `False`.
- Orchestrator detects `False` return and writes `overall_status:
  "completed_email_failed"` to `run_history.json`, serialising the full
  `PropertyResult` list alongside the status.
- On the next scheduled invocation, orchestrator detects
  `completed_email_failed`, deserialises stored results, and calls
  `send_notification` directly — bypassing scraper and matcher.
- On successful retry, record is upgraded to `completed`.
- The check itself is considered done regardless of email status, preventing
  unnecessary re-scraping.

---

## R7: run_history.json Corrupted or Missing

**Likelihood:** Low — disk error, partial write, or first run on a new machine.

**What goes wrong:** If `run_history.json` cannot be read and the orchestrator
treats this as a fatal error, the agent stops working entirely. If it treats a
corrupted file as "already completed", checks are silently skipped.

**Mitigations:**
- `_load_run_history` treats any read failure (missing file, `json.JSONDecodeError`,
  non-list content) as empty history, logging a warning.
- The safe default is "not yet run this month" — the agent proceeds with the
  check rather than skipping it.
- The write path uses atomic-style overwrite (write full history, not append) to
  minimise corruption risk from partial writes.

---

## R8: Amount Match on Wrong Account (Steps 2 and 3)

**Likelihood:** Low — outgoing mortgage payments have similar amounts to rent.

**What goes wrong:** Step 2 or Step 3 matches an outgoing mortgage payment (on a
different account) as a "possible rent payment," producing a false positive.

**Mitigations (confirmed fix):**
- Step 2 filters candidates to `t["account"] == prop.account` (income-side
  deposit account only) before comparing amounts.
- Step 3 similarly filters: `t["amount"] > 0 and t["account"] == prop.account`.
- Step 1 already scoped by account. All three steps now require account match.
- Documented in `LESSONS.md` with the specific example (Truist mortgage payment
  falsely matched as 505 rent).

---

## R9: LLM Returns Malformed JSON

**Likelihood:** Medium — local models frequently produce preamble text, markdown
fences, or truncated output.

**What goes wrong:** `_parse_json_response` fails to extract JSON; the property
is marked `MISSING` even if the LLM identified a match.

**Mitigations:**
- `_parse_json_response` handles three cases:
  1. Clean JSON — direct parse.
  2. Markdown fences (` ```json ... ``` `) — strip fences, then parse.
  3. Prose preamble with embedded JSON block — regex-extract first `{...}` block.
- If all three attempts fail, log the raw response at ERROR level and return
  `MISSING` with the first 300 chars of the raw response in `notes`.
- Log every raw LLM response at DEBUG level for offline diagnosis.

---

## Error Handling Strategy (By Dependency)

| Dependency | Failure mode | System behaviour |
|-----------|-------------|-----------------|
| Monarch API | Schema change / no data | `ScraperError` → error email + `run_history: error` |
| Monarch session | Login redirect | `ScraperError` with re-login instructions |
| Ollama (Step 3) | Unreachable | `LLM_SKIPPED_MISSING` for unresolved properties |
| Ollama (email) | Unreachable | Python fallback template; email still sent |
| Gmail SMTP | Delivery failure | `completed_email_failed` + stored results for retry |
| `run_history.json` | Corrupt / missing | Treat as empty; log warning; proceed |
| Config files | Missing / invalid | `ConfigError` at startup; no external calls made |
