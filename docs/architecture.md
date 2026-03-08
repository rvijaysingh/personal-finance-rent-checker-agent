# Architecture: Rent Payment Checker Agent

> See also: [docs/risks.md](risks.md) · [docs/config.md](config.md) · [docs/testing.md](testing.md)

---

## System Overview

Automated monthly rent verification across three rental properties. Scrapes
Monarch Money for transactions, applies a three-step matching pipeline, and
emails a payment status summary. Runs on a schedule; no web interface or API.

---

## Pipeline

```
[orchestrator.py]
    │
    ├── [config_loader.py]
    │       ├── reads: config/.env.json          (secrets + machine paths + Ollama)
    │       ├── reads: config/agent_config.json  (properties, thresholds)
    │       └── reads: prompts/*.md              (LLM templates)
    │
    ├── [monarch_scraper.py]
    │       └── Playwright (persistent browser profile)
    │               └── → List[TransactionRecord]
    │
    ├── [transaction_matcher.py]
    │       ├── Step 1: category label match     (deterministic)
    │       ├── Step 2: amount fallback match    (deterministic)
    │       └── Step 3: LLM fallback             (optional, Ollama)
    │               └── → List[PropertyResult]
    │
    ├── [notifier.py]
    │       ├── LLM email body (Ollama, optional)
    │       │       └── fallback: Python template
    │       └── Gmail SMTP
    │
    └── [logs/run_history.json]
            ├── read:  idempotency check
            └── write: audit trail
```

---

## Data Flow

1. **Idempotency check.** Read `run_history.json`. If this month is `completed`,
   exit. If `completed_email_failed`, log the prior failure and fall through to
   the full pipeline — scrape fresh data and retry notification. Otherwise proceed.

2. **Config load.** `config_loader` validates all three sources. Any missing
   field raises `ConfigError` immediately naming the field.

2a. **Ollama warm-up.** Send a tiny prompt to force the model into GPU memory
   before the scraper starts. Non-fatal: if Ollama is unreachable, log a warning
   and continue. Steps 1 and 2 are fully deterministic and do not need Ollama.
   Qwen 3 8B can take up to 2 minutes on a cold start; doing this now means
   that wait happens before, not during, the matching pipeline.

3. **Scrape.** `monarch_scraper` navigates Monarch Money via Playwright,
   intercepts the internal GraphQL API responses, and returns
   `List[TransactionRecord]` for the lookback window (current month + up to
   `early_payment_days` into the previous month). No filtering here.

4. **Match.** `transaction_matcher` runs in two passes:
   - **Pass 1** (deterministic): For each property, try Step 1 (category label),
     then Step 2 (amount fallback). Track matched transaction IDs.
   - **Pass 2** (LLM): For unresolved properties, offer only unclaimed
     transactions to Ollama (Step 3). Returns `List[PropertyResult]`.

5. **Email generation.** `notifier` calls Ollama with `payment_summary.md`
   template. Falls back to Python template if Ollama unavailable.

6. **Email send.** Gmail SMTP. On failure, orchestrator writes
   `completed_email_failed` to `run_history.json` for retry on next invocation.

7. **Run log write.** Append full record to `run_history.json` with timestamp,
   per-property results, overall status, and any errors.

---

## Module Responsibilities

| Module | Inputs | Outputs | Key constraint |
|--------|--------|---------|----------------|
| `config_loader.py` | 3 config files | `AppConfig` | Fails fast on any invalid field |
| `monarch_scraper.py` | `AppConfig` | `List[TransactionRecord]` | No business logic |
| `transaction_matcher.py` | transactions + config | `List[PropertyResult]` | Two-pass to prevent double-match |
| `notifier.py` | results + config | (side effect: email) | Always sends; LLM optional |
| `orchestrator.py` | CLI args | exit code | Writes run_history.json |
| `models.py` | — | type definitions | No logic |

---

## Key Design Decisions

**DD1: API Response Interception (not DOM scraping)**
DOM scraping is unworkable: Monarch uses a virtualised list that only keeps
viewport-visible rows in the DOM. The scraper intercepts Monarch's internal
GraphQL responses via `page.on("response")`, then replays
`Web_GetTransactionsList` from inside the browser context using
`page.evaluate()` with `limit=100`. External HTTP clients get 403; browser
context includes cookies but still requires the `Authorization: Token` header
captured from the original outgoing request.

**DD2: Three-Step Hybrid Matching with Two-Pass Evaluation**
Category match (Step 1) → amount fallback (Step 2) → LLM review (Step 3).
Each property exits the pipeline as soon as resolved. LLM is only called for
genuinely ambiguous cases, keeping cost and latency low.

The pipeline runs in two passes to prevent cross-property transaction
contamination. **Pass 1** runs Steps 1 and 2 for all properties, tracking
claimed transaction object IDs. **Pass 2** runs Step 3 only for unresolved
properties, and only offers transactions not already claimed. For example: if
Pass 1 matches a $2,950 deposit to Links Lane via Step 2, that same transaction
is never offered to Calmar's Step 3 evaluation.

**DD3: Persistent Browser Profile**
Operator logs in once manually; Playwright reuses the saved session. Avoids
credential management and MFA complexity. Session expiry requires a one-time
manual re-login.

**DD4: Config Split (Secrets / Business Rules / Prompts)**
`.env.json` (secrets + machine paths, gitignored), `agent_config.json`
(property rules, gitignored), `prompts/` (LLM templates, committed). See
[docs/config.md](config.md) for full schema.

**DD5: Local Ollama**
Financial data never leaves the machine. Zero inference cost. Model and endpoint
are in `.env.json`; a single config change swaps the model.

**DD6: LLM Email with Python Fallback**
Ollama generates prose email body from `payment_summary.md`. If unavailable, a
Python template formats the same structured data. Email always goes out.

**DD7: `run_history.json` as Dual-Purpose Log**
Single JSON file serves as idempotency guard (has this month run?) and audit
trail. Human-readable; no database dependency.

**DD8: Scraper / Matcher Separation**
Scraper returns all transactions without filtering. Matcher owns all business
logic (category, amount, date evaluation). Scraper is reusable for future check
types without modification.

**DD9: Shared `models.py`**
All cross-module types (`TransactionRecord`, `PropertyConfig`, `PropertyResult`,
`PaymentStatus`) live in one file. Prevents circular imports; types import
nothing from `src/`.

**DD10: `completed_email_failed` Status + Full Pipeline Retry**
SMTP failure writes `completed_email_failed` to `run_history.json`. The next
invocation detects this status, logs the prior failure, and re-runs the full
scrape + match + email pipeline rather than reconstructing from stored results.
This ensures the retry uses fresh transaction data and avoids deserialisation
complexity. A new `completed` record is appended on success. See
[docs/risks.md](risks.md) for failure mode details.

---

## Key Data Structures

All types in `src/models.py`:

```python
class PaymentStatus(Enum):
    PAID_ON_TIME = "paid_on_time"
    PAID_LATE = "paid_late"
    WRONG_AMOUNT = "wrong_amount"
    POSSIBLE_MATCH = "possible_match"         # Step 2
    LLM_SUGGESTED = "llm_suggested"          # Step 3
    MISSING = "missing"
    LLM_SKIPPED_MISSING = "llm_skipped_missing"  # Ollama down

class TransactionRecord(TypedDict):
    date: date; description: str; amount: float; account: str; category: str

@dataclass class PropertyConfig:
    name: str; merchant_name: str; expected_rent: float
    due_day: int; grace_period_days: int; category_label: str; account: str

@dataclass class PropertyResult:
    property_name: str; status: PaymentStatus
    matched_transaction: TransactionRecord | None
    notes: str; step_resolved_by: int | None  # 1, 2, 3, or None
```

`run_history.json` serialises dates as ISO strings and `PaymentStatus` as
string values for human-readable audit trail and future multi-month analysis.
