"""Main orchestrator for the rent payment checker agent.

Coordinates all modules in sequence:
  1. Idempotency check (already run this month?)
  2. Load and validate configuration
  3. Scrape transactions from Monarch Money
  4. Run three-step matching pipeline
  5. Send notification email
  6. Write run record to run_history.json

Usage:
    python -m src.orchestrator [flags]

Flags:
    --dry-run           Run everything but do not send email and do not
                        write to run_history.json. Prints email to stdout.
    --no-scrape         Skip scraping. Use --transactions-file to supply
                        transactions, or leave empty for a "no transactions"
                        test of the email pipeline.
    --transactions-file FILE
                        Path to a JSON file containing TransactionRecord
                        dicts. Used with --no-scrape for testing.
    --no-headless       Show the browser window during scraping.
    --verbose           Set log level to DEBUG.
    --force             Bypass the idempotency check and re-run even if
                        already completed this month.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config_loader import AppConfig

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Run the rent payment checker. Returns exit code (0=success, 1=failure)."""
    args = _parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    today = date.today()
    logger.info("=== Rent Payment Checker starting for %s ===", today.strftime("%B %Y"))

    # --- Step 1: Load config (before idempotency so we have the log path) ---
    from src.config_loader import load_config, ConfigError

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        # Cannot send error email without config — just exit
        return 1

    # --- Step 2: Idempotency check ---
    if not args.force and not args.dry_run:
        already_run, prior_status = _check_already_run(config.log_path, today)
        if already_run:
            if prior_status == "completed_email_failed":
                logger.info(
                    "Previous run this month completed but email failed. "
                    "Will retry email delivery."
                )
                return _retry_email(config, today, dry_run=args.dry_run)
            logger.info(
                "Already completed successfully this month (%s). "
                "Use --force to re-run.",
                today.strftime("%B %Y"),
            )
            return 0
    elif args.force:
        logger.info("--force flag set; bypassing idempotency check")

    # --- Step 3: Scrape or load transactions ---
    from src.monarch_scraper import scrape_transactions, ScraperError

    transactions = []
    scrape_error: str | None = None

    if args.no_scrape:
        if args.transactions_file:
            try:
                transactions = _load_transactions_file(args.transactions_file)
                logger.info(
                    "--no-scrape: loaded %d transactions from %s",
                    len(transactions), args.transactions_file,
                )
            except Exception as exc:
                logger.error("Could not load transactions file: %s", exc)
                return 1
        else:
            logger.warning(
                "--no-scrape set without --transactions-file; "
                "using empty transaction list. All properties will be MISSING."
            )
    else:
        try:
            transactions = scrape_transactions(
                config,
                headless_override=(not args.no_headless) if args.no_headless else None,
            )
            logger.info("Scrape complete: %d transactions", len(transactions))
        except ScraperError as exc:
            scrape_error = str(exc)
            logger.error("Scraper failed: %s", exc)
            # Attempt error notification before exiting
            from src.notifier import send_error_notification

            send_error_notification(
                config,
                error_message=f"Scraper error: {exc}",
                run_date=today,
                dry_run=args.dry_run,
            )
            _write_run_record(
                config.log_path,
                run_date=today,
                results=[],
                overall_status="error",
                errors=[scrape_error],
                email_sent=False,
                dry_run=args.dry_run,
            )
            return 1

    # --- Step 4: Match payments ---
    from src.transaction_matcher import match_properties

    try:
        results = match_properties(transactions, config)
    except Exception as exc:
        error_msg = f"Matching pipeline failed: {exc}\n{traceback.format_exc()}"
        logger.error(error_msg)
        from src.notifier import send_error_notification

        send_error_notification(
            config,
            error_message=error_msg,
            run_date=today,
            dry_run=args.dry_run,
        )
        _write_run_record(
            config.log_path,
            run_date=today,
            results=[],
            overall_status="error",
            errors=[error_msg],
            email_sent=False,
            dry_run=args.dry_run,
        )
        return 1

    # --- Step 5: Send notification ---
    from src.notifier import send_notification
    from src.models import PaymentStatus

    ATTENTION_STATUSES = {
        PaymentStatus.PAID_LATE,
        PaymentStatus.WRONG_AMOUNT,
        PaymentStatus.POSSIBLE_MATCH,
        PaymentStatus.LLM_SUGGESTED,
        PaymentStatus.MISSING,
        PaymentStatus.LLM_SKIPPED_MISSING,
    }
    needs_attention = any(r.status in ATTENTION_STATUSES for r in results)
    overall_status = "action_needed" if needs_attention else "completed"

    email_sent = send_notification(
        results,
        config,
        run_date=today,
        dry_run=args.dry_run,
    )

    if not email_sent and not args.dry_run:
        logger.error("Email delivery failed. Recording run as completed_email_failed.")
        overall_status = "completed_email_failed"

    # --- Step 6: Write run record ---
    _write_run_record(
        config.log_path,
        run_date=today,
        results=results,
        overall_status=overall_status,
        errors=[],
        email_sent=email_sent,
        dry_run=args.dry_run,
    )

    _log_summary(results)
    logger.info(
        "=== Run complete: %s (email_sent=%s) ===",
        overall_status, email_sent,
    )
    return 0


def _retry_email(config: "AppConfig", today: date, *, dry_run: bool) -> int:
    """Re-send the email for a run that completed but failed to notify."""
    logger.info("Retrying email delivery for %s", today.strftime("%B %Y"))

    history = _load_run_history(config.log_path)
    this_month = today.strftime("%Y-%m")
    prior_record = next(
        (
            r for r in reversed(history)
            if r.get("run_date", "").startswith(this_month)
            and r.get("overall_status") == "completed_email_failed"
        ),
        None,
    )

    if prior_record is None:
        logger.warning("No completed_email_failed record found; running fresh")
        return 1

    # Reconstruct results from stored record
    from src.models import PaymentStatus, PropertyResult, TransactionRecord

    results = []
    for pr in prior_record.get("property_results", []):
        mt = pr.get("matched_transaction")
        matched = None
        if mt:
            matched = TransactionRecord(
                date=date.fromisoformat(mt["date"]),
                description=mt.get("description", ""),
                amount=mt.get("amount", 0.0),
                account=mt.get("account", ""),
                category=mt.get("category", ""),
            )
        try:
            status = PaymentStatus(pr["status"])
        except (KeyError, ValueError):
            status = PaymentStatus.MISSING
        results.append(
            PropertyResult(
                property_name=pr.get("property_name", "Unknown"),
                status=status,
                matched_transaction=matched,
                notes=pr.get("notes", ""),
                step_resolved_by=pr.get("step_resolved_by"),
            )
        )

    from src.notifier import send_notification

    email_sent = send_notification(results, config, run_date=today, dry_run=dry_run)

    if email_sent:
        # Upgrade the record to 'completed'
        for r in reversed(history):
            if (
                r.get("run_date", "").startswith(this_month)
                and r.get("overall_status") == "completed_email_failed"
            ):
                r["overall_status"] = "completed"
                r["email_retry_sent"] = datetime.now().isoformat()
                break
        _write_history(config.log_path, history, dry_run=dry_run)
        logger.info("Email retry succeeded; record updated to 'completed'")
        return 0
    else:
        logger.error("Email retry also failed")
        return 1


# ---------------------------------------------------------------------------
# Idempotency and run history
# ---------------------------------------------------------------------------


def _check_already_run(log_path: Path, today: date) -> tuple[bool, str | None]:
    """Check whether a successful run already exists for the current month.

    Returns:
        (already_run, status_of_prior_run)
        already_run is True if this month is complete or had an email failure.
    """
    history = _load_run_history(log_path)
    this_month = today.strftime("%Y-%m")

    for record in reversed(history):
        run_date_str = record.get("run_date", "")
        if not run_date_str.startswith(this_month):
            continue
        status = record.get("overall_status", "")
        if status in ("completed", "completed_email_failed"):
            logger.debug(
                "Found prior run this month: run_date=%s status=%s",
                run_date_str, status,
            )
            return True, status

    return False, None


def _load_run_history(log_path: Path) -> list[dict]:
    """Load run_history.json, returning [] if missing or malformed."""
    if not log_path.exists():
        logger.debug("run_history.json not found at %s — treating as empty", log_path)
        return []

    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            logger.warning(
                "run_history.json is not a JSON array — treating as empty. "
                "File: %s", log_path
            )
            return []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Could not read run_history.json (%s) — treating as empty. File: %s",
            exc, log_path,
        )
        return []


def _write_run_record(
    log_path: Path,
    *,
    run_date: date,
    results: list,
    overall_status: str,
    errors: list[str],
    email_sent: bool,
    dry_run: bool,
) -> None:
    """Append a run record to run_history.json."""
    from src.models import PaymentStatus

    if dry_run:
        logger.info("Dry run: skipping run_history.json write")
        return

    record = {
        "run_date": datetime.now().isoformat(),
        "check_month": run_date.strftime("%Y-%m"),
        "overall_status": overall_status,
        "email_sent": email_sent,
        "errors": errors,
        "property_results": [
            {
                "property_name": r.property_name,
                "status": r.status.value,
                "step_resolved_by": r.step_resolved_by,
                "notes": r.notes,
                "matched_transaction": (
                    {
                        "date": r.matched_transaction["date"].isoformat(),
                        "description": r.matched_transaction["description"],
                        "amount": r.matched_transaction["amount"],
                        "account": r.matched_transaction["account"],
                        "category": r.matched_transaction["category"],
                    }
                    if r.matched_transaction
                    else None
                ),
            }
            for r in results
        ],
    }

    history = _load_run_history(log_path)
    history.append(record)
    _write_history(log_path, history, dry_run=False)
    logger.info("Run record written to %s (status: %s)", log_path, overall_status)


def _write_history(log_path: Path, history: list[dict], *, dry_run: bool) -> None:
    """Write the full history list to disk."""
    if dry_run:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(history, indent=2, default=str), encoding="utf-8"
    )


def _load_transactions_file(path: str) -> list:
    """Load a JSON transactions fixture file for --no-scrape testing."""
    from src.models import TransactionRecord

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    txns = []
    for row in raw:
        row["date"] = date.fromisoformat(row["date"])
        txns.append(TransactionRecord(**row))  # type: ignore[typeddict-item]
    return txns


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log_summary(results: list) -> None:
    """Log a concise per-property summary after the run."""
    logger.info("--- Payment check summary ---")
    for r in results:
        step = f"(step {r.step_resolved_by})" if r.step_resolved_by else "(unresolved)"
        txn_info = ""
        if r.matched_transaction:
            t = r.matched_transaction
            txn_info = f"  ${t['amount']:.2f} on {t['date']}"
        logger.info(
            "  %-20s  %-24s %s%s",
            r.property_name,
            r.status.value,
            step,
            txn_info,
        )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rent payment checker agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print email to stdout; do not send or write run history",
    )
    parser.add_argument(
        "--no-scrape",
        action="store_true",
        help="Skip Monarch scraping (use --transactions-file or empty list)",
    )
    parser.add_argument(
        "--transactions-file",
        metavar="FILE",
        help="JSON fixture file to use with --no-scrape",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show browser window during scraping",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run even if this month's check is already complete",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    sys.exit(main())
