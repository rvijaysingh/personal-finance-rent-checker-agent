# Configuration Reference

> Related: [docs/architecture.md](architecture.md) · [docs/risks.md](risks.md)

---

## Overview

Configuration is split across three sources with different access and iteration
rules. All three are loaded and validated at startup by `config_loader.py`.

| Source | Gitignored | Purpose |
|--------|-----------|---------|
| `config/.env.json` | Yes | Secrets, machine paths, Ollama settings |
| `config/agent_config.json` | Yes | Project business rules (properties, thresholds) |
| `prompts/*.md` | No | LLM prompt templates |

Committed templates:
- `config/.env.json.example` — schema with placeholder values
- `config/agent_config.json.example` — schema with placeholder values

---

## Setup on a New Machine

1. Copy `config/.env.json.example` to `config/.env.json` (or to the path you
   set in `ENV_CONFIG_PATH`), then fill in real values.
2. Copy `config/agent_config.json.example` to `config/agent_config.json` and
   update property names, amounts, and due dates.
3. Set `ENV_CONFIG_PATH` environment variable if you want the `.env.json` to
   live outside the repo (e.g., shared across multiple projects on the same host).
4. Run `python -m src.config_loader` to validate both files. Any missing or
   invalid field is reported with the exact field name.

---

## `.env.json` Schema

Machine-local file: secrets, machine-specific paths, and Ollama settings.
Treated as a shared machine-level file — all projects on the same host can
point to the same `.env.json` via `ENV_CONFIG_PATH`.

**Path resolution:**
1. If `ENV_CONFIG_PATH` environment variable is set, use that path.
2. Otherwise, default to `<repo_root>/config/.env.json`.

```json
{
  "gmail_sender":                   "your-address@gmail.com",
  "gmail_password":                 "your-16-char-app-password",
  "gmail_recipient":                "your-address@gmail.com",
  "monarch_browser_profile_path":   "C:\\Users\\YOU\\AppData\\Local\\playwright-rent-profile",
  "ollama_endpoint":                "http://localhost:11434",
  "ollama_model":                   "qwen3:8b"
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `gmail_sender` | string | Yes | Gmail address used to send |
| `gmail_password` | string | Yes | Gmail App Password (16-char) |
| `gmail_recipient` | string | Yes | Notification destination |
| `monarch_browser_profile_path` | string | Yes | Absolute path to Playwright persistent profile directory |
| `ollama_endpoint` | string | Yes | Must start with `http`. e.g. `http://localhost:11434` |
| `ollama_model` | string | Yes | e.g. `qwen3:8b` |

**Gmail setup:** Use a Gmail App Password (not your account password). Go to
Google Account → Security → 2-Step Verification → App passwords.

**Browser profile setup:** The profile directory does not need to exist in
advance — Playwright creates it. Open it once manually (`--no-headless`) and
log in to Monarch Money to seed the session.

---

## `agent_config.json` Schema

Project-specific business rules. Lives in `config/agent_config.json` relative
to the repo root. Contains only project-level settings (no machine-specific
secrets or paths).

```json
{
  "scraper_headless": true,
  "early_payment_days": 3,
  "email_subject_prefix": "[Agent - Rent Check]",
  "properties": [
    {
      "name": "Links Lane",
      "merchant_name": "JANE DOE",
      "expected_rent": 2950.00,
      "due_day": 1,
      "grace_period_days": 5,
      "category_label": "Rental Income (Links Lane)",
      "account": "Chase Checking ••1230"
    },
    {
      "name": "Calmar",
      "merchant_name": "BOB SMITH",
      "expected_rent": 3100.00,
      "due_day": 1,
      "grace_period_days": 5,
      "category_label": "Rental Income (Calmar)",
      "account": "Chase Checking ••1230"
    },
    {
      "name": "505",
      "merchant_name": "ALICE JOHNSON",
      "expected_rent": 2800.00,
      "due_day": 1,
      "grace_period_days": 5,
      "category_label": "Rental Income (505)",
      "account": "Chase Checking ••1230"
    }
  ]
}
```

### Top-Level Fields

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `scraper_headless` | bool | No | `true` | Set `false` to show browser window |
| `early_payment_days` | int | No | `3` | Days before the 1st to include (lookback into prior month) |
| `email_subject_prefix` | string | No | `"[Agent - Rent Check]"` | Prepended to all email subjects |
| `properties` | array | Yes | — | Must be non-empty; see below |

### Property Object Fields

| Field | Type | Required | Constraints |
|-------|------|----------|------------|
| `name` | string | Yes | Display name (e.g. `"Links Lane"`) |
| `merchant_name` | string | Yes | Tenant name as shown in Monarch (used by LLM in Step 3) |
| `expected_rent` | number | Yes | Must be `> 0` |
| `due_day` | int | Yes | Day of month (1–28) |
| `grace_period_days` | int | Yes | Must be `>= 0`. Deadline = `due_day + grace_period_days` |
| `category_label` | string | Yes | Exact Monarch category label for Step 1 match |
| `account` | string | Yes | Monarch account `displayName` (scopes all three steps) |

---

## Prompt Templates (`prompts/`)

Both files are required. Committed to git and versioned independently.

| File | Used by | Variables |
|------|---------|-----------|
| `prompts/rent_match.md` | `transaction_matcher.py` Step 3 | `{{property_name}}` `{{merchant_name}}` `{{expected_rent}}` `{{due_day}}` `{{grace_period_days}}` `{{transactions_json}}` |
| `prompts/payment_summary.md` | `notifier.py` | `{{check_date}}` `{{results_json}}` |

---

## Derived Paths (not in any config file)

These are always computed relative to the repo root by `config_loader.py`:

| Path | Value |
|------|-------|
| `log_path` | `<repo_root>/logs/run_history.json` |
| `prompts_dir` | `<repo_root>/prompts/` |

---

## `logs/run_history.json` Format

Gitignored, machine-local. Auto-created on first run. Append-only JSON array.

```json
[
  {
    "run_date": "2026-03-01T10:05:32.123456",
    "check_month": "2026-03",
    "overall_status": "completed",
    "email_sent": true,
    "errors": [],
    "property_results": [
      {
        "property_name": "Links Lane",
        "status": "paid_on_time",
        "step_resolved_by": 1,
        "notes": "Received 2026-03-01 — on time (deadline 2026-03-06).",
        "matched_transaction": {
          "date": "2026-03-01",
          "description": "Zelle From JANE DOE",
          "amount": 2950.00,
          "account": "Chase Checking ••1230",
          "category": "Rental Income (Links Lane)"
        }
      }
    ]
  }
]
```

`overall_status` values:

| Value | Meaning |
|-------|---------|
| `completed` | Check and email both succeeded |
| `completed_email_failed` | Check succeeded; email failed; retry on next run |
| `action_needed` | Check ran; one or more properties need attention |
| `error` | Scraper or matcher failed; check did not complete |

---

## `ENV_CONFIG_PATH` Setup

Set this environment variable to point the agent at a `.env.json` outside the
repo directory (useful when the same `.env.json` is shared across multiple
agent projects on the same host).

**Windows Task Scheduler / PowerShell:**
```
[System.Environment]::SetEnvironmentVariable("ENV_CONFIG_PATH",
  "C:\Users\YOU\secrets\rent-agent-env.json", "User")
```

**Windows CMD:**
```
setx ENV_CONFIG_PATH "C:\Users\YOU\secrets\rent-agent-env.json"
```

If `ENV_CONFIG_PATH` is not set, the agent falls back to
`<repo_root>/config/.env.json`.
