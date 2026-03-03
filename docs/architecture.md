# Architecture: Rent Payment Checker Agent

## 1. Purpose & Scope

This system automates monthly rent payment verification across three rental
properties by scraping transaction data from Monarch Money, applying a
three-step matching pipeline (category match → amount fallback → LLM review),
and emailing a structured status summary to the operator.

The system does NOT expose a web interface, API, or dashboard. It does NOT
manage scheduling (invoked externally), provide historical reporting, or
handle any check types other than rent payments in the current implementation.

---

## 2. Component Diagram

```
[orchestrator.py]
    │
    ├── [config_loader.py]
    │       ├── reads: config/.env.json          (secrets)
    │       ├── reads: config/agent_config.json  (business rules)
    │       └── reads: prompts/*.md              (LLM templates)
    │
    ├── [monarch_scraper.py]
    │       └── Playwright (persistent browser profile)
    │               └── → List[TransactionRecord]
    │
    ├── [transaction_matcher.py]
    │       ├── Step 1: category label match     (deterministic)
    │       ├── Step 2: amount fallback match    (deterministic)
    │       └── Step 3: LLM fallback             (optional)
    │               └── [Ollama HTTP API]        (local, optional)
    │                       └── → List[PropertyResult]
    │
    ├── [notifier.py]
    │       ├── [Ollama HTTP API]                (email body generation, optional)
    │       │       └── fallback: Python template
    │       └── Gmail SMTP
    │
    └── [logs/run_history.json]
            ├── read:  idempotency check (already run this month?)
            └── write: audit trail (results, timestamps, errors)
```

---

## 3. Data Flow (End-to-End)

1. **Idempotency check.** Orchestrator reads `logs/run_history.json`. If a
   successful run for the current month already exists, it logs "already
   checked this month" and exits without taking any further action.

2. **Configuration load & validation.** `config_loader` reads all three
   config sources (`.env.json`, `agent_config.json`, `prompts/`). Any
   missing or invalid field raises immediately with the field name in the
   error message. No further steps proceed if config is invalid.

3. **Transaction extraction.** `monarch_scraper` launches a Playwright
   browser using the persistent profile, navigates to the Monarch Money
   Transactions page, and returns all transactions for the current month
   from the Chase Checking account as `List[TransactionRecord]`. No
   filtering or business logic is applied here.

4. **Three-step matching.** `transaction_matcher` receives the full
   `List[TransactionRecord]` and the list of `PropertyConfig` entries.
   - **Step 1** (category match): For each property, search for a
     transaction whose Monarch category label exactly matches the
     configured label (e.g., "Rental Income (Links Lane)"). Evaluate
     amount (within tolerance) and date (on time vs. late).
   - **Step 2** (amount fallback): For properties not resolved in Step 1,
     search all transactions for the month where the amount matches the
     expected rent within tolerance. Flag as "possible match - needs
     manual review."
   - **Step 3** (LLM fallback): For properties not resolved in Steps 1 or
     2, send remaining unmatched transactions to Ollama with the property
     details. The LLM evaluates whether any transaction could plausibly be
     the rent payment. If Ollama is unreachable, skip Step 3 and flag as
     MISSING with a note.
   Returns `List[PropertyResult]`.

5. **Email generation.** Orchestrator passes `List[PropertyResult]` to
   `notifier`. The notifier calls Ollama with the `payment_summary.md`
   prompt template to generate a readable prose email body. If Ollama is
   unavailable, a Python fallback template formats the same data in a
   basic tabular layout.

6. **Email send.** Notifier sends via Gmail SMTP. If SMTP fails, the error
   is logged and raised — the run is not marked successful.

7. **Run log write.** Orchestrator appends the full run record to
   `logs/run_history.json` (timestamp, per-property results, overall
   status, any errors). This record is what prevents duplicate runs in
   future invocations.

---

## 4. Design Decisions

---

### DD1: Data Extraction — Browser Scraping vs. Other Approaches

**Decision:** Extract Monarch Money transaction data via Playwright browser
automation.

**Context:** Monarch Money does not provide a public API. Transaction data
must come from somewhere — the choices are browser automation, reverse-
engineered private API calls, or manual CSV export.

**Options Considered:**
- *Playwright browser automation:* Automate a real browser session that
  navigates Monarch's UI and reads the rendered transaction table.
- *Private API reverse engineering:* Intercept and replay the GraphQL or
  REST calls that Monarch's web app makes internally.
- *Manual CSV export:* Monarch supports CSV export; the operator uploads
  it on each run.

**Chosen Approach:** Playwright browser automation with a persistent profile.

**Tradeoffs:**
- *Optimizes for:* No legal or ToS risk from API reverse-engineering; no
  manual operator steps per run; leverages the browser session Monarch
  already trusts.
- *Sacrifices:* Stability — Monarch UI changes (CSS structure, page
  routing, element IDs) will break selectors without warning. Selector
  maintenance is an ongoing cost.
- *Cost/speed:* Browser startup adds 5–15 seconds per run. Acceptable for
  a monthly background task.
- *Revisit if:* Monarch releases a public API, an actively maintained
  unofficial API client becomes available, or Monarch adds bot detection
  that makes browser automation unreliable.

---

### DD2: Three-Step Hybrid Matching Pipeline

**Decision:** Apply matching in three sequential steps: exact category label
match → amount-based fallback → LLM review for unresolved properties.

**Context:** Rent payments arrive in various ways (tenant bank transfer,
Zelle, check). Monarch may or may not assign the correct category label to
each transaction. A purely deterministic match on category will miss
miscategorized payments; a purely LLM-based match is slow and non-
deterministic; no matching at all requires full manual review.

**Options Considered:**
- *Category-only:* Fast and deterministic but misses miscategorized
  transactions.
- *Amount-only:* Catches miscategorized transactions but produces false
  positives if multiple transactions have similar amounts.
- *LLM-only:* Flexible but slow, non-deterministic, and requires Ollama to
  be running.
- *Three-step hybrid:* Deterministic steps first; LLM only for genuinely
  ambiguous cases.

**Chosen Approach:** Three-step hybrid, applied sequentially. Each property
exits the pipeline as soon as it is resolved.

**Tradeoffs:**
- *Optimizes for:* Accuracy (deterministic when data is clean), auditability
  (Step 1 and Step 2 results are fully explainable), and cost (LLM inference
  only for hard cases).
- *Sacrifices:* Implementation complexity — three distinct matching strategies
  must be maintained. Step 2 and Step 3 results require manual review,
  adding operator burden.
- *Accuracy vs. cost:* LLM inference is local (no API cost), but LLM calls
  add latency. The cascade ensures LLM is called rarely in steady state.
- *Revisit if:* Monarch category labels become systematically unreliable
  (making Step 1 rarely useful), or if the LLM's match quality significantly
  exceeds deterministic matching and operator review burden justifies always
  using it.

---

### DD3: Persistent Browser Profile (No Programmatic Login)

**Decision:** Use a Playwright persistent browser profile where the operator
has logged in manually once, preserving session cookies for all future runs.

**Context:** Monarch Money requires authentication and may use MFA. Handling
credentials and MFA programmatically is complex and fragile. The alternative
is to persist the browser's authenticated session state.

**Options Considered:**
- *Programmatic login:* Store credentials in config; script fills login form
  each run. Breaks on MFA, CAPTCHA, or login flow changes.
- *Persistent profile:* Operator logs in once manually; Playwright reuses the
  browser profile (cookies, local storage) for all subsequent automated runs.
- *OAuth token:* Not available without a public API.

**Chosen Approach:** Playwright persistent browser profile stored at a
configurable path on the local machine.

**Tradeoffs:**
- *Optimizes for:* No credentials in code or config, no MFA complexity, no
  login flow fragility.
- *Sacrifices:* Initial setup on a new machine requires a manual login step.
  If Monarch invalidates the session (long inactivity, forced re-auth), the
  operator must manually log in again. This is not fully automated end-to-end.
- *Security:* The browser profile directory contains session cookies and must
  be protected at the OS level (not committed to git).
- *Revisit if:* Monarch adds aggressive bot detection that invalidates stored
  sessions frequently enough to make manual re-login operationally burdensome.

---

### DD4: Configuration Split (Secrets / Business Rules / Prompts)

**Decision:** Separate configuration into three distinct sources with
different gitignore and iteration rules.

**Context:** A single config file mixes concerns: secrets cannot be committed,
business rules change rarely and benefit from version history, prompt templates
need frequent iteration and experimentation.

**Options Considered:**
- *Single config file:* Simple but cannot be committed if it contains secrets.
- *Environment variables only:* Works for secrets but poor for structured
  business rules (property lists, thresholds).
- *Three-source split:* `.env.json` for secrets (gitignored), `agent_config.json`
  for business rules (gitignored, machine-local with committed example), `prompts/`
  for LLM templates (committed, iterated independently).

**Chosen Approach:** Three-source split loaded and validated by `config_loader`.

**Tradeoffs:**
- *Optimizes for:* Secrets never in git history; prompt templates versioned and
  diffable; business rules (rent amounts, due dates) can be updated without
  touching code.
- *Sacrifices:* A developer must manage three files rather than one. `.example`
  files must be kept in sync with actual schema.
- *Revisit if:* Configuration complexity grows significantly (many check types,
  many properties) to the point where a database or structured config management
  tool is warranted.

---

### DD5: Local Ollama LLM vs. Cloud API

**Decision:** Use a locally running Ollama instance (model: qwen3:8b) for all
LLM calls. Model name and endpoint are in config.

**Context:** LLM calls are used for Step 3 matching and email body generation.
Financial transaction data (amounts, property names, tenant context) is passed
in prompts. The system runs on a local Windows machine.

**Options Considered:**
- *Cloud LLM API (OpenAI, Anthropic, etc.):* Higher capability, no local
  resource requirement, but sends financial data to external services and
  incurs per-call cost.
- *Local Ollama:* Data stays local, zero inference cost, but smaller model
  capability and requires local GPU or CPU resources.
- *No LLM at all:* Simplest, but Step 3 and prose email generation are lost.

**Chosen Approach:** Local Ollama with qwen3:8b. Model name and endpoint URL
are in `agent_config.json` so a single config change swaps the model.

**Tradeoffs:**
- *Optimizes for:* Financial data privacy (never leaves the machine), zero
  inference cost, no external API dependency.
- *Sacrifices:* Model capability is lower than frontier models; Step 3 match
  quality and email prose quality are constrained by the local model's ability.
  Requires the local machine to have sufficient CPU/GPU resources to run
  inference at acceptable speed.
- *Cost:* No monetary cost per call, but local inference adds latency
  (seconds to tens of seconds depending on hardware).
- *Revisit if:* Step 3 match quality is unacceptably poor, LLM-generated
  emails require frequent manual editing, or the machine lacks sufficient
  resources to run inference reliably.

---

### DD6: LLM-Generated Email Body with Python Fallback

**Decision:** Use Ollama to generate the email body from a prompt template.
If Ollama is unavailable, fall back to a Python string template that presents
the same data in a basic tabular format. Email is always sent regardless of
LLM availability.

**Context:** A readable prose summary is more useful to the operator than raw
data, but LLM availability cannot be guaranteed. The notification must always
go out.

**Options Considered:**
- *LLM-only email:* Best prose quality but fails silently if Ollama is down.
- *Python template only:* Always works, but output is mechanical and harder
  to scan.
- *LLM primary with Python fallback:* Best of both — rich output when
  available, reliable delivery always.

**Chosen Approach:** LLM primary (`payment_summary.md` template), Python
template fallback. Fallback is explicitly noted in the email body.

**Tradeoffs:**
- *Optimizes for:* Operator always receives a notification; rich prose when
  LLM is healthy.
- *Sacrifices:* Non-determinism — LLM email body varies across runs even for
  identical inputs. Two code paths must be maintained and tested.
- *Revisit if:* Ollama outages are frequent enough that the fallback becomes
  the de facto path (at which point, remove the LLM email path and use the
  template exclusively), or if LLM-generated emails are often misleading and
  require correction.

---

### DD7: run_history.json as Dual-Purpose Log

**Decision:** A single JSON file (`logs/run_history.json`) serves as both the
idempotency guard (has this month already been checked?) and the audit trail
(what were the results of each run?).

**Context:** The system must not run twice in one month. It also needs a
persistent record of results for debugging. A database adds operational
complexity. A single flat file handles both needs at this scale.

**Options Considered:**
- *Separate files:* One file for idempotency flag, one for audit log. More
  modular but doubles the file management surface.
- *SQLite database:* Queryable, supports future reporting, but requires a
  schema, migrations, and a driver dependency.
- *Single JSON file:* Simple, human-readable, no dependencies. Idempotency
  check reads the most recent entry; audit trail is the full array.

**Chosen Approach:** Single `logs/run_history.json` file, gitignored,
machine-local.

**Tradeoffs:**
- *Optimizes for:* Simplicity, no database dependency, human-readable audit
  trail inspectable with any text editor.
- *Sacrifices:* No query capability for historical analysis (e.g., "which
  tenant is consistently late"). The file grows unbounded over time (small
  in practice: ~12 entries/year per check type).
- *Revisit if:* Multi-check-type queries (rent + mortgage in the same
  idempotency check), historical trend reporting, or concurrent writes become
  requirements.

---

### DD8: Scraper/Matcher Separation of Concerns

**Decision:** `monarch_scraper` returns all transactions for the month without
filtering. All business logic (category matching, amount comparison, date
evaluation) lives in `transaction_matcher`.

**Context:** The scraper could be made more efficient by fetching only
transactions matching certain criteria. But coupling business rules into the
scraper makes it harder to reuse for other check types (e.g., mortgage
payments) and harder to test independently.

**Options Considered:**
- *Coupled scraper:* Scraper accepts filter parameters (category, amount
  range) and returns only matching transactions. Fewer bytes transferred but
  business logic is split across modules.
- *Decoupled scraper:* Scraper fetches all current-month transactions;
  matcher handles all filtering and comparison.

**Chosen Approach:** Decoupled scraper. The scraper's contract is: return
`List[TransactionRecord]` for the current month from the configured account.

**Tradeoffs:**
- *Optimizes for:* Single responsibility per module; scraper is reusable for
  rent, mortgage, and future check types without modification; matcher is
  independently testable with mock transaction data.
- *Sacrifices:* Efficiency — the scraper fetches and parses more rows than
  strictly needed for rent checking alone. In practice, a month of
  transactions is a small dataset (tens to low hundreds of rows).
- *Revisit if:* Scraping performance becomes a bottleneck (unlikely given
  monthly run frequency), or if the transaction volume is large enough that
  targeted extraction meaningfully reduces scrape time.

---

## 5. Module Descriptions

### `config_loader.py`
**Responsibility:** Load, merge, and validate all configuration at startup.
Raise immediately on any missing or invalid field.

**Inputs:**
- `config/.env.json` (path from `ENV_CONFIG_PATH` env var or default)
- `config/agent_config.json` (path relative to repo root)
- `prompts/` directory (all `.md` files)

**Outputs:**
- `AppConfig` dataclass containing secrets, business rules, and prompt
  templates as named fields.

**Key behavior:** Validates required fields before returning. Never returns
a partially populated config object. Prompt templates are loaded as raw
strings and returned in a dict keyed by filename stem.

---

### `monarch_scraper.py`
**Responsibility:** Open Playwright with the persistent browser profile,
navigate to the Monarch Money Transactions page, extract all transactions
for the current month from the configured account, and return them as a
structured list.

**Inputs:**
- `AppConfig` (browser profile path, account name, headless flag)

**Outputs:**
- `List[TransactionRecord]`

**Key behavior:** Applies no filtering or business logic. Raises on
navigation failure, selector mismatch, or timeout. Supports `--no-headless`
flag for manual debugging. Selector strategy: prefer `data-testid` and
`aria-label` attributes over CSS class names. Document selector changes
in `LESSONS.md`.

---

### `transaction_matcher.py`
**Responsibility:** Apply the three-step matching pipeline to determine the
payment status of each configured property.

**Inputs:**
- `List[TransactionRecord]` (from scraper)
- `List[PropertyConfig]` (from config)
- `AppConfig` (thresholds, Ollama endpoint, prompt template)

**Outputs:**
- `List[PropertyResult]`

**Key behavior:**
- Step 1: Exact category label match. Evaluates amount (within
  `amount_tolerance_percent`) and date (within `due_day + grace_period_days`).
- Step 2: Amount match on all transactions for properties not resolved in
  Step 1. Marks result as requiring manual review.
- Step 3: LLM call via Ollama HTTP for properties not resolved in Steps 1–2.
  If Ollama is unreachable, skips Step 3 and marks property MISSING with note.
  Logs full prompt and raw response at DEBUG level.

---

### `notifier.py`
**Responsibility:** Generate an email body and send it via Gmail SMTP.

**Inputs:**
- `List[PropertyResult]`
- `AppConfig` (Gmail credentials, Ollama endpoint, summary prompt template)

**Outputs:** None (side effect: email sent)

**Key behavior:**
- Calls Ollama to generate prose email body from `payment_summary.md`
  template. If Ollama is unavailable, uses Python string template fallback.
- Subject line: `[Rent Check] {date} - All Received` or
  `[Rent Check] {date} - ACTION NEEDED`.
- Raises on SMTP failure — never swallows the error.
- Notes in email body whether LLM email generation was unavailable.

---

### `orchestrator.py`
**Responsibility:** Main entry point. Coordinates all modules in sequence,
handles top-level errors, and writes the run record to `run_history.json`.

**Inputs:** Command-line arguments (`--no-headless`, `--force` to bypass
idempotency check).

**Outputs:** Exit code (0 = success, 1 = failure). Side effects: email sent,
`run_history.json` updated.

**Key behavior:**
1. Check `run_history.json` for existing successful run this month.
2. Load and validate config.
3. Run scraper.
4. Run matcher.
5. Run notifier.
6. Write run record to `run_history.json` (only on success).
7. On any unhandled exception: log error, attempt to send error notification
   email, do NOT write success record.

---

## 6. Key Data Structures

```python
from dataclasses import dataclass
from typing import TypedDict
from datetime import date
from enum import Enum

class PaymentStatus(Enum):
    PAID_ON_TIME        = "paid_on_time"
    PAID_LATE           = "paid_late"
    WRONG_AMOUNT        = "wrong_amount"
    POSSIBLE_MATCH      = "possible_match"      # Step 2 result
    LLM_SUGGESTED       = "llm_suggested"       # Step 3 result
    MISSING             = "missing"
    LLM_SKIPPED_MISSING = "llm_skipped_missing" # Step 3 unavailable


class TransactionRecord(TypedDict):
    date:        date
    description: str
    amount:      float   # positive = credit (income)
    account:     str
    category:    str


class PropertyConfig(TypedDict):
    name:                str    # e.g. "Links Lane"
    tenant_name:         str
    expected_rent:       float
    due_day:             int    # day of month rent is due
    grace_period_days:   int
    category_label:      str    # Monarch category to match in Step 1
    account:             str    # account name to scope Step 1 search


@dataclass
class PropertyResult:
    property_name:       str
    status:              PaymentStatus
    matched_transaction: TransactionRecord | None
    notes:               str             # human-readable context for email
    step_resolved_by:    int | None      # 1, 2, 3, or None if unresolved
```

---

## 7. Error Handling Strategy

### Monarch UI unavailable / selector breaks
The scraper raises a descriptive exception (e.g., `ScraperError: timeout
waiting for transaction table`). The orchestrator catches it, logs the full
traceback, attempts to send an error notification email to the operator, and
exits with code 1. It does NOT write a success record to `run_history.json`,
so the next invocation will attempt the run again.

### Ollama unavailable (Step 3 matching)
`transaction_matcher` catches the connection error. Properties not resolved
by Steps 1 or 2 are assigned status `LLM_SKIPPED_MISSING` with a note:
"LLM check skipped — Ollama unreachable." The matcher completes and returns
results. Matching continues without Step 3.

### Ollama unavailable (email generation)
`notifier` catches the connection error and falls back to the Python string
template. The email is still sent. The email body includes: "LLM review and
email generation were unavailable — showing raw results only."

### Gmail SMTP failure
`notifier` raises after logging the error with full context (recipient,
subject, SMTP host, error message). The orchestrator propagates this as a
run failure. It does NOT write a success record. The operator must
investigate the SMTP credentials or Gmail account.

### `run_history.json` missing or malformed
`orchestrator` treats a missing or unreadable file as "not yet run this
month" — the safe default that allows the run to proceed. It logs a warning
noting the file was not found or could not be parsed.

### Config invalid at startup
`config_loader` raises `ConfigError: missing required field: <field_name>`
immediately, before any external calls are made. No scraping, matching,
or email sending occurs. The operator sees a clear error naming the exact
missing or invalid field.
