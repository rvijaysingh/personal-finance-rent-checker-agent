# Personal Finance Agent: Rent Payment Checker

## Purpose
An automated agent that checks Monarch Money for tenant rent payments
across 3 rental properties, compares them against expected amounts
and due dates, and emails a summary of payment status (paid, late,
missing, wrong amount).

## Architecture Constraints
- Runtime: Windows 10/11 server, Python 3.13
- LLM: Ollama running locally at http://localhost:11434
  (model name specified in config, currently qwen3:8b).
  Used only in Step 3 (LLM fallback) for properties that Steps 1
  and 2 could not match. Also used to generate notification email.
- Browser automation: Playwright with persistent browser profile
  (credentials saved in browser, no programmatic login needed each run)
- Email: Gmail SMTP (credentials in .env.json)
- No external APIs for Monarch (browser scraping only)
- See docs/architecture.md for identified risks and mitigations.
  All modules should be built defensively against the risks
  identified in that document.

## Project Structure
personal-finance-rent-checker-agent/
  CLAUDE.md
  README.md
  LESSONS.md
  .gitignore
  config/
    agent_config.json          # Business rules (gitignored, machine-local)
    agent_config.json.example  # Template with placeholder values (committed)
    .env.json.example          # Template for secrets (committed)
  prompts/
    rent_match.md              # Transaction matching prompt
    payment_summary.md         # Summary generation prompt
  src/
    __init__.py
    config_loader.py           # Loads all config sources
    monarch_scraper.py         # Playwright: extract transactions
    transaction_matcher.py     # Hybrid: deterministic + LLM matching
    notifier.py                # Gmail SMTP email
    orchestrator.py            # Main entry point
    models.py                  # Shared data structures (TransactionRecord,
                               # PropertyConfig, PropertyResult, PaymentStatus).
                               # Lives in its own module so scraper and matcher
                               # can both use the same types without importing
                               # from each other.
  tests/
    test_matcher.py
    test_config.py
  docs/
    architecture.md
  logs/run_history.json 

## Configuration Design
Three config sources:
1. .env.json (gitignored, machine-local): secrets only.
   Path resolved from ENV_CONFIG_PATH environment variable,
   falling back to ../config/.env.json relative to repo root.
2. config/agent_config.json (gitignored, machine-local): business rules,
   property definitions, thresholds. config/agent_config.json.example
   is committed as a template showing the required schema with
   placeholder values.
3. prompts/ directory (committed): LLM prompt templates
   loaded at runtime with variable substitution.
4. logs/run_history.json (gitignored, machine-local): run history
   used to track successful monthly checks and provide an audit trail.



## Business Rules

### Run Logic
1. On each run, first check if this month's most recent check was already
  completed fully and successfully (via a local run log, e.g., logs/run_history.json). If already completed this month, skip and log "already checked."
2. If not yet run this month, proceed with the payment check.

### Payment Matching (Three Steps)
Step 1 - Category Match:
- Navigate to Monarch's Transactions page.
- For each property, search this month's transactions for an exact
  match on the Monarch category label:
  - "Rental Income (Links Lane)" - expected $X
  - "Rental Income (Calmar)" - expected $X
  - "Rental Income (505)" - expected $X
- All rent payments arrive into either Chase Checking account.
- A payment is "on time" if received by due_day + grace_period_days.
- A payment is "received but late" if the category matches but the
  date is past due_day + grace_period_days.
- A payment is "wrong amount" if the category matches but the amount
  does not match expected rent (within amount_tolerance_percent).

Step 2 - Amount Fallback (only for properties not found in Step 1):
- If a property has no category match, search ALL transactions in the
  deposit account for the current month where the amount matches
  expected rent (within tolerance).
- Flag any matches as "possible rent payment - needs manual review"
  and include the transaction description in the output.
- This catches cases where Monarch categorization was missed or wrong.

Step 3 - LLM Fallback (only for properties not found in Steps 1 or 2):
- If neither Step 1 nor Step 2 finds a match, send all remaining
  unmatched transactions for the month to the LLM (Ollama/Qwen 3)
  with the property details (tenant name, expected rent, due date).
- Ask the LLM to evaluate whether any transaction could plausibly
  be the rent payment (e.g., a Zelle or bank payment 
  for a different amount, or a payment split across two transactions).
- Flag any LLM-identified candidates as "LLM-suggested match -
  needs manual review" with the LLM's reasoning included.
- If the LLM finds nothing or is unavailable, flag the property as
  "MISSING - no payment found."
- If Ollama is unreachable, skip Step 3 and flag as "MISSING" with
  a note that LLM check was skipped.


### Notification
- Email a status summary after every run, whether all payments are
  found or not.
- Subject line indicates overall status:
  "[Rent Check] {date} - All Received" or
  "[Rent Check] {date} - ACTION NEEDED" (in bold)
- The email body is generated by the LLM (Ollama/Qwen 3) using the
  prompts/payment_summary.md template. The structured matching results
  from Steps 1-2 and the LLM review notes from Step 3 are passed to
  the LLM, which produces a clear, readable summary.
- If Ollama is unavailable, fall back to a simple Python template
  that presents the same per-property data in a basic tabular format.
  The email must still be sent regardless of LLM availability.
- Body includes per-property: status (paid on time, paid late,
  wrong amount, possible match, LLM-suggested match, or missing),
  matched transaction details, and LLM review notes.
- Flag any anomalies prominently at the top of the email.
- When a property was resolved by Step 2 (not the primary category
  match), the email must clearly indicate this and that manual
  review is recommended.
- When Step 3 identified a possible match for a missing property,
  include the LLM's reasoning so the recipient can evaluate it.
- When Ollama was unavailable, note "LLM review and email generation
  were unavailable - showing raw results only" in the email.

### Logging
- Log every run to logs/run_history.json with: date and time, per-property
  results (status, matched transaction, amount, date), overall status,
  and any errors.
- This file serves double duty: it is the run log for debugging AND
  the "already completed this month" check in Step 1.

## Risks and Mitigations
These are cross-cutting risks that should influence how every module
is built. Claude Code should build defensively against these.

### Monarch UI Changes (Likelihood: High)
Monarch Money can change their HTML structure at any time without
notice. When this happens, Playwright selectors break and the
scraper returns no data or wrong data.
- Mitigation: The scraper must validate that it found a reasonable
  number of transactions (not zero, not thousands). If the result
  looks wrong, log the error with the page HTML snippet and send
  a failure notification email rather than reporting "no payments
  found."
- Mitigation: Document all working selectors in LESSONS.md so
  fixes are fast.
- Mitigation: Keep the scraper as simple as possible. The fewer
  selectors it depends on, the fewer breakage points.

### Monarch Session Expiry (Likelihood: Medium)
The persistent browser profile may lose its login session. The
scraper would either see a login page or get redirected.
- Mitigation: The scraper must detect when it is on a login page
  rather than the transactions page. If detected, log the error
  and send a failure notification: "Monarch session expired,
  manual re-login required."
- Mitigation: Do not attempt automated login. Credentials in
  the browser profile are the auth strategy.

### Ollama Unavailable (Likelihood: Medium)
The Ollama service on the server may not be running, may have
crashed, or the model may not be loaded.
- Mitigation: Steps 1 and 2 work without Ollama. Only Step 3
  (LLM review) and email generation require it.
- Mitigation: If Ollama is unreachable, skip Step 3, generate
  the email using the Python template fallback, and note in the
  email that LLM review was unavailable.
- Mitigation: Check Ollama connectivity once at startup before
  running the pipeline. Log a warning if unavailable.

### Transaction Categorization Missing (Likelihood: Low)
Monarch may not categorize a transaction with the expected
"Rental Income (X)" label. This could happen with a new tenant,
a bank change, or a Monarch update.
- Mitigation: This is exactly what Step 2 (amount fallback) and
  Step 3 (LLM fallback) exist for. The three-step matching
  design is the mitigation.
- Mitigation: The email should make it obvious which step resolved
  each property so the user knows when categorization is missing.

### Duplicate or Split Payments (Likelihood: Low)
A tenant might pay rent in two installments, or a duplicate
transaction might appear.
- Mitigation: Step 3 (LLM review) should flag multiple transactions
  that could be related to the same property.
- Mitigation: If Step 1 finds two category-matched transactions
  for the same property in the same month, flag as "multiple
  payments found - needs review" rather than silently picking one.

### Email Delivery Failure (Likelihood: Low)
Gmail SMTP could reject the email (bad credentials, rate limit,
app password expired).
- Mitigation: Log the full SMTP error.
- Mitigation: The run should still be logged to run_history.json
  as completed (since the check itself succeeded), with a note
  that email delivery failed. This prevents the agent from
  re-running the check but never emailing.
- Mitigation: Consider the run "completed with email failure"
  so the next scheduled run will detect it has not successfully
  notified and retry the email.

## Monarch Scraper Notes
- Monarch Money does not have a public API. Data extraction is via
  Playwright browser automation.
- Use persistent browser profiles so login state is preserved
  between runs.
- The scraper should extract all transactions for the current month
  from the deposit account (Chase Checking 1230). Return the full
  set with: date, description, amount, account, category.
- Filtering happens downstream in the matcher, not in the scraper.
  The scraper's job is to extract raw transaction data. The matcher
  applies Step 1 (category match) and Step 2 (amount fallback).
  This keeps the scraper simple and reusable for other check types.
- Monarch's HTML structure may change. Prefer data-testid attributes
  or aria labels over CSS class selectors. When selectors break,
  update them and document the fix in LESSONS.md.
- Always run in headless mode for production; include a --no-headless
  flag for debugging.


## Future Scope (Do NOT Build Now)
The following are out of scope for the current build. Do not build
abstractions or frameworks for these. Just avoid hardcoding decisions
that would make them difficult later.

- LLM will be passed tenant name in case it needs to check the
  transaction detail to try and identify a rent payment
- Mortgage payment checking: Will reuse the same scraper and matching
  pipeline. The matching steps should accept different "check types"
  from config (each with their own category labels, expected amounts,
  and schedules), not be rent-specific in core logic. run_history.json
  should track mortgage checks alongside rent checks so a single
  "already completed this month" check covers both.
- Gmail-to-Trello agent: Separate orchestrator, but should reuse
  config_loader and notifier. Keep those modules generic.
- Alternative LLM providers: Model name and endpoint are in config.
  No additional abstraction needed.
- Alternative data sources (CSV import, API if Monarch releases one):
  The scraper returns a standard transaction format (list of dicts
  with date, description, amount, account, category). Any new data
  source should produce the same format and plug into the matcher
  unchanged.
- Multiple notification channels (Slack, SMS): The notifier receives
  structured results and produces output. Adding a channel should mean
  adding a new notifier module, not rewriting the existing one.
- Scheduling and monitoring: Not yet set up. The agent will need to
  run on a schedule on a Windows server. The approach will be
  determined during implementation. May later add health checks,
  run history dashboards, or retry logic. The orchestrator should
  log enough (start, end, results, errors) to support this without
  code changes.
- Multi-month lookback or historical analysis: Currently checks
  the current month only. run_history.json accumulates over time
  and could support trend analysis later (e.g., which tenants are
  consistently late). Do not build reporting now, but do not purge
  run history either.