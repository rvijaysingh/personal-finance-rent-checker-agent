# Personal Finance Agent: Rent Payment Checker

## Purpose
An automated agent that checks Monarch Money for tenant rent payments
across 3 rental properties, compares them against expected amounts
and due dates, and emails a summary of payment status (paid, late,
missing, wrong amount).

## Architecture Constraints
- Runtime: Windows 10/11 server, Python 3.13
- LLM: Ollama running locally at http://localhost:11434
  (model name specified in config, currently qwen3:8b)
- Browser automation: Playwright with persistent browser profile
  (credentials saved in browser, no programmatic login needed each run)
- Email: Gmail SMTP (credentials in .env.json)
- No external APIs for Monarch (browser scraping only)

## Project Structure
personal-finance-agent/
  CLAUDE.md
  README.md
  LESSONS.md
  .gitignore
  config/
    agent_config.json          # Business rules (committed)
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
  tests/
    test_matcher.py
    test_config.py
  docs/
    architecture.md

## Configuration Design
Three config sources:
1. .env.json (gitignored, machine-local): secrets only.
   Path resolved from ENV_CONFIG_PATH environment variable,
   falling back to ../config/.env.json relative to repo root.
2. config/agent_config.json (committed): business rules,
   property definitions, thresholds.
3. prompts/ directory (committed): LLM prompt templates
   loaded at runtime with variable substitution.

## Business Rules
- 3 properties, each with: name, tenant_name, expected_rent,
  due_day_of_month, account_name_in_monarch.
- Payment is "on time" if received by due_day + grace_period_days.
- Payment matching: deterministic first (amount within tolerance
  + date range + account match). LLM only if multiple candidates
  match the deterministic criteria.
- Email summary must clearly state per property: status
  (paid / late / missing / partial), amount, date received,
  and any anomalies.

## Monarch Scraper Notes
- Monarch Money does not have a public API. Data extraction
  is via Playwright browser automation.
- Use persistent browser profiles so login state is preserved
  between runs.
- Monarch's HTML structure may change. Prefer data-testid
  attributes or aria labels over CSS class selectors.
- Always run in headless mode for production; include a
  --no-headless flag for debugging.

## Design for Extension (Do NOT Build Now)
The following are out of scope for the current build. Do not build
abstractions or frameworks for these. Just avoid hardcoding
decisions that would make them difficult later.

- Mortgage payment checking: Will reuse the same scraper and matcher
  pipeline. The matcher should accept different "check types" from
  config, not be rent-specific in its core logic.
- Gmail-to-Trello agent: Separate orchestrator, but should reuse
  config_loader and notifier. Keep those modules generic.
- Alternative LLM providers: Model name and endpoint are already
  in config per the global standards. No additional abstraction needed.
- Alternative data sources (CSV import, API if Monarch releases one):
  The scraper returns a standard transaction format (list of dicts
  with date, description, amount, account). Any new data source
  should produce the same format and plug into the matcher unchanged.
- Multiple notification channels (Slack, SMS): The notifier receives
  structured results and produces output. Adding a channel should mean
  adding a new notifier module, not rewriting the existing one.
- Scheduling and monitoring: Currently runs via Windows Task Scheduler.
  May later add health checks, run history tracking, or a simple
  dashboard. The orchestrator should log enough (start, end, results,
  errors) to support this without code changes.