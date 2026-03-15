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
    │       └── Gmail SMTP (Python template — no LLM)
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

5. **Email generation.** `notifier` generates the HTML email body using a Python
   template. No LLM is involved; the template formats structured results directly.

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

**DD2: Confidence-Based Matching — Only Both Signals = Auto-Accept**
The matching pipeline applies a strict confidence rule: a payment is
auto-accepted (`PAID_ON_TIME` or `PAID_LATE`) only when **both** the category
label AND the amount match within tolerance. Any partial signal raises
`REVIEW_NEEDED` with a rationale for human review:

- **Step 1** (category match): Category matches but amount is outside tolerance
  → `REVIEW_NEEDED` (pipeline stops here; Step 2 is not reached).
- **Step 2** (amount fallback): Amount matches but category is wrong
  → `REVIEW_NEEDED`. Only reached when Step 1 found no category match at all.
- **Step 3** (LLM review): Any LLM-suggested match → `REVIEW_NEEDED`.
  Only reached when Steps 1 and 2 both produced no result.
- **Auto-accepted**: Step 1 finds correct category AND correct amount.

This prevents silent auto-acceptance of wrong-amount payments or miscategorised
deposits. The LLM (Anthropic claude-haiku primary, Ollama fallback) is only
called for genuinely unresolved properties.

The pipeline runs in two passes to prevent cross-property contamination.
**Pass 1** runs Steps 1 and 2 for all properties, tracking claimed transaction
IDs. **Pass 2** runs Step 3 only for unresolved properties using unclaimed
transactions. A transaction confirmed for one property is never offered as a
candidate for another.

**DD3: Persistent Browser Profile**
Operator logs in once manually; Playwright reuses the saved session. Avoids
credential management and MFA complexity. Session expiry requires a one-time
manual re-login.

**DD4: Config Split (Secrets / Business Rules / Prompts)**
`.env.json` (secrets + machine paths, gitignored), `agent_config.json`
(property rules, gitignored), `prompts/` (LLM templates, committed). See
[docs/config.md](config.md) for full schema.

**DD5: Anthropic Primary, Ollama Fallback for Step 3**
Step 3 tries Anthropic claude-haiku first (fast, remote). If the API key is
absent or the call fails, it falls back to local Ollama. If both are
unavailable, the property is marked `MISSING` with a note. Model names and
endpoints are in `.env.json`; a single config change swaps the model.

**DD6: Python-Only Email — No LLM**
The email body is generated by a Python template in `notifier.py`. Removing
the LLM from email generation eliminates a latency and availability dependency:
the email always uses the same format, is deterministic, and never requires
Ollama or Anthropic to be reachable.

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
    PAID_ON_TIME = "paid_on_time"    # Step 1: category + amount both match
    PAID_LATE = "paid_late"          # Step 1: category + amount match, past deadline
    REVIEW_NEEDED = "review_needed"  # Step 1/2/3: partial match — human must verify
    MISSING = "missing"              # Step 3: no match found, or LLM unavailable

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
