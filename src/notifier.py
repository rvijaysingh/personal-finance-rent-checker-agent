"""Email notification module.

Generates the payment status email body (via Ollama LLM or Python fallback)
and sends it via Gmail SMTP.

Key behaviours:
  - Always sends an email, regardless of LLM availability.
  - If Ollama is unavailable, falls back to a plain-text Python template.
  - On SMTP failure, raises so the orchestrator can log and handle it.
  - Email subject reflects overall status at a glance.

Run standalone to preview the email without sending:
    python -m src.notifier --dry-run
"""

from __future__ import annotations

import json
import logging
import smtplib
import urllib.error
import urllib.request
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config_loader import AppConfig

from src.models import PaymentStatus, PropertyResult

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# Statuses that require operator attention (payment missing or uncertain).
# PAID_LATE is intentionally excluded — payment was received, just late.
ATTENTION_STATUSES = {
    PaymentStatus.WRONG_AMOUNT,
    PaymentStatus.POSSIBLE_MATCH,
    PaymentStatus.LLM_SUGGESTED,
    PaymentStatus.MISSING,
    PaymentStatus.LLM_SKIPPED_MISSING,
}

# Statuses indicating payment was received but late.
LATE_STATUSES = {PaymentStatus.PAID_LATE}

STATUS_LABELS = {
    PaymentStatus.PAID_ON_TIME:        "Paid on time",
    PaymentStatus.PAID_LATE:           "PAID LATE",
    PaymentStatus.WRONG_AMOUNT:        "WRONG AMOUNT",
    PaymentStatus.POSSIBLE_MATCH:      "POSSIBLE MATCH — needs review",
    PaymentStatus.LLM_SUGGESTED:       "LLM-SUGGESTED — needs review",
    PaymentStatus.MISSING:             "MISSING — no payment found",
    PaymentStatus.LLM_SKIPPED_MISSING: "MISSING (LLM check skipped)",
}

# Statuses where no payment was found at all.
_TRULY_MISSING = {PaymentStatus.MISSING, PaymentStatus.LLM_SKIPPED_MISSING}

# Inline CSS for per-property status highlighting in HTML email.
_YELLOW_STYLE = "background-color: #FFEB3B; padding: 2px 6px;"
_RED_STYLE = "background-color: #EF5350; color: white; padding: 2px 6px;"


def _compute_summary_line(results: list[PropertyResult]) -> str:
    """Return a factual one-sentence opening for the email.

    Logic (mirrors the subject-line tiers):
      - Any truly-missing payments → "[N] of [T] received. [M] not yet received."
      - Late but none missing      → "[N] of [T] received on time. [M] received late."
      - All on time                → "All [T] rent payments received on time."
    """
    total = len(results)
    missing_count = sum(1 for r in results if r.status in _TRULY_MISSING)
    late_count = sum(1 for r in results if r.status in LATE_STATUSES)

    if missing_count > 0:
        received = total - missing_count
        return (
            f"{received} of {total} payments received. "
            f"{missing_count} not yet received."
        )
    if late_count > 0:
        on_time = total - late_count
        return (
            f"{on_time} of {total} payments received on time. "
            f"{late_count} received late."
        )
    return f"All {total} rent payments received on time."


def _highlight(text: str, style: str) -> str:
    """Wrap text in a <span> with the given inline CSS style."""
    return f'<span style="{style}">{text}</span>'


def send_notification(
    results: list[PropertyResult],
    config: "AppConfig",
    run_date: date,
    *,
    dry_run: bool = False,
    error_message: str | None = None,
) -> bool:
    """Generate and send the payment status email.

    Args:
        results: Matching results for all properties.
        config: Validated application configuration.
        run_date: Date the check was performed.
        dry_run: If True, print the email to stdout instead of sending.
        error_message: If provided, include an error notice in the email.

    Returns:
        True if email was sent (or dry-run), False if SMTP failed.

    Raises:
        Never — SMTP failure is caught, logged, and returned as False so
        the orchestrator can decide how to handle it.
    """
    subject = _build_subject(
        results, run_date, config.email_subject_prefix,
        error=error_message is not None,
    )
    body, used_llm = _generate_body(results, config, run_date, error_message)

    if dry_run:
        print("=" * 60)
        print(f"DRY RUN — email would be sent to: {config.gmail_recipient}")
        print(f"Subject: {subject}")
        print("=" * 60)
        print(body)
        print("=" * 60)
        logger.info("Dry run: email preview printed, not sent")
        return True

    logger.info(
        "Sending email to %s (subject: %r, llm_body=%s)",
        config.gmail_recipient, subject, used_llm,
    )

    try:
        _send_smtp(
            sender=config.gmail_sender,
            password=config.gmail_password,
            recipient=config.gmail_recipient,
            subject=subject,
            body=body,
        )
        logger.info("Email sent successfully")
        return True
    except Exception as exc:
        logger.error(
            "SMTP delivery failed to %s: %s",
            config.gmail_recipient, exc,
            exc_info=True,
        )
        return False


def send_error_notification(
    config: "AppConfig",
    error_message: str,
    run_date: date,
    *,
    dry_run: bool = False,
) -> bool:
    """Send a minimal error notification when the pipeline itself fails.

    This is a best-effort call — if it also fails, the error is logged
    but not re-raised.

    Args:
        config: Validated application configuration.
        error_message: Description of the failure.
        run_date: Date of the failed run.
        dry_run: If True, print instead of send.

    Returns:
        True if the notification was sent/printed, False otherwise.
    """
    subject = f"{config.email_subject_prefix} {run_date} - PIPELINE ERROR"
    body = (
        f"<html><body>"
        f"<p>The rent payment checker failed on {run_date}.</p>"
        f"<pre>{error_message}</pre>"
        f"<p>No payment results are available. The run was not logged as complete.</p>"
        f"<p>Please investigate and re-run manually.</p>"
        f"</body></html>"
    )

    if dry_run:
        print(f"DRY RUN error email:\nSubject: {subject}\n{body}")
        return True

    try:
        _send_smtp(
            sender=config.gmail_sender,
            password=config.gmail_password,
            recipient=config.gmail_recipient,
            subject=subject,
            body=body,
        )
        logger.info("Error notification sent")
        return True
    except Exception as exc:
        logger.error("Could not send error notification: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Email construction
# ---------------------------------------------------------------------------


def _build_subject(
    results: list[PropertyResult],
    run_date: date,
    prefix: str,
    *,
    error: bool = False,
) -> str:
    """Build the email subject line.

    Three-tier logic:
      1. Any property missing or uncertain → ACTION NEEDED
      2. No missing, but at least one late payment → LATE PAYMENT
      3. All on time → All Received
    """
    if error:
        return f"{prefix} {run_date} - PIPELINE ERROR"

    if any(r.status in ATTENTION_STATUSES for r in results):
        suffix = "ACTION NEEDED"
    elif any(r.status in LATE_STATUSES for r in results):
        suffix = "LATE PAYMENT"
    else:
        suffix = "All Received"
    return f"{prefix} {run_date} - {suffix}"


def _generate_body(
    results: list[PropertyResult],
    config: "AppConfig",
    run_date: date,
    error_message: str | None,
) -> tuple[str, bool]:
    """Generate the email body.

    Tries Ollama first; falls back to Python template on any failure.

    Returns:
        (body_text, used_llm) — used_llm is True if Ollama generated the body.
    """
    prompt_template = config.prompts.get("payment_summary", "")
    summary_line = _compute_summary_line(results)

    results_data = [
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
    ]

    prompt = (
        prompt_template
        .replace("{{check_date}}", str(run_date))
        .replace("{{summary_line}}", summary_line)
        .replace("{{results_json}}", json.dumps(results_data, indent=2))
    )

    logger.debug("Payment summary prompt:\n%s", prompt)

    # Attempt LLM generation
    try:
        llm_body = _call_ollama_for_summary(config.ollama_endpoint, config.ollama_model, prompt)
        if not llm_body.strip():
            raise ValueError("Ollama returned an empty response")

        # Validate: the LLM must include the pre-computed summary line verbatim.
        # If it doesn't, the body may contradict the facts — discard it.
        if summary_line not in llm_body:
            logger.warning(
                "LLM email body does not contain required summary line %r — "
                "possible factual contradiction. Discarding LLM output. "
                "Raw LLM body (first 300 chars): %r",
                summary_line, llm_body[:300],
            )
            raise ValueError("LLM body missing required summary line")

        logger.info("Email body generated by LLM")
        if error_message:
            llm_body = (
                f"<p><em>NOTE: The run completed but an error occurred:</em><br>"
                f"<pre>{error_message}</pre></p>\n{llm_body}"
            )
        body = f"<html><body>\n{llm_body}\n</body></html>"
        return body, True
    except Exception as exc:
        logger.warning("LLM email generation failed (%s); using fallback template", exc)

    # Python fallback
    body = _fallback_body(results, run_date, llm_unavailable=True, error_message=error_message)
    return body, False


def _call_ollama_for_summary(endpoint: str, model: str, prompt: str) -> str:
    """Call Ollama for email body generation."""
    url = f"{endpoint.rstrip('/')}/api/generate"
    payload = json.dumps(
        {"model": model, "prompt": prompt, "stream": False}
    ).encode("utf-8")

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("response", "")


def _fallback_body(
    results: list[PropertyResult],
    run_date: date,
    *,
    llm_unavailable: bool,
    error_message: str | None,
) -> str:
    """Produce an HTML email body when LLM is unavailable."""
    parts: list[str] = ["<html><body>"]

    if llm_unavailable:
        parts.append(
            "<p><em>NOTE: LLM review and email generation were unavailable — "
            "showing raw results only.</em></p>"
        )

    if error_message:
        parts.append(
            f"<p><em>NOTE: An error occurred during this run:</em><br>"
            f"<pre>{error_message}</pre></p>"
        )

    # Opening summary line — same factual text the LLM is instructed to use.
    summary_line = _compute_summary_line(results)
    needs_emphasis = any(
        r.status in ATTENTION_STATUSES or r.status in LATE_STATUSES for r in results
    )
    if needs_emphasis:
        parts.append(f"<p><strong>{summary_line}</strong></p>")
    else:
        parts.append(f"<p>{summary_line}</p>")

    parts.append(f"<p>Rent check for: {run_date.strftime('%B %Y')}</p>")
    parts.append("<hr>")

    # Per-property bullet list with inline CSS highlighting.
    parts.append("<ul>")
    for r in results:
        label = STATUS_LABELS.get(r.status, r.status.value)

        if r.status in LATE_STATUSES:
            name_html = _highlight(f"<strong>{r.property_name}</strong>", _YELLOW_STYLE)
            label_html = _highlight(label, _YELLOW_STYLE)
        elif r.status in _TRULY_MISSING:
            name_html = _highlight(f"<strong>{r.property_name}</strong>", _RED_STYLE)
            label_html = _highlight(label, _RED_STYLE)
        else:
            name_html = f"<strong>{r.property_name}</strong>"
            label_html = label

        item: list[str] = [f"{name_html}: {label_html}"]

        if r.matched_transaction:
            t = r.matched_transaction
            item.append(
                f"Transaction: {t['description']} &nbsp;|&nbsp; "
                f"${t['amount']:.2f} &nbsp;|&nbsp; {t['date']} "
                f"&nbsp;|&nbsp; {t['account']}"
            )
            if t["category"]:
                item.append(f"Category: {t['category']}")

        if r.notes:
            item.append(f"Notes: {r.notes}")

        item.append(f"Resolved by: Step {r.step_resolved_by or '—'}")
        parts.append("<li>" + "<br>".join(item) + "</li>")

    parts.append("</ul>")
    parts.append("<hr>")
    parts.append("<p><small>Generated by rent-payment-checker-agent.</small></p>")
    parts.append("</body></html>")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# SMTP delivery
# ---------------------------------------------------------------------------


def _send_smtp(
    sender: str,
    password: str,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    """Send a plain-text email via Gmail SMTP with STARTTLS.

    Raises:
        smtplib.SMTPException: On any SMTP-level failure.
        OSError: On network-level failure.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(body, "html", "utf-8"))

    logger.debug(
        "Connecting to %s:%d as %s", SMTP_HOST, SMTP_PORT, sender
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())

    logger.debug("SMTP delivery complete")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse
    import sys

    from src.config_loader import load_config, ConfigError
    from src.models import PaymentStatus, PropertyResult, TransactionRecord

    parser = argparse.ArgumentParser(description="Preview or send the notification email")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print the email instead of sending (default: true for standalone)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Sample results for preview
    today = date.today()
    sample_results = [
        PropertyResult(
            property_name="Links Lane",
            status=PaymentStatus.PAID_ON_TIME,
            matched_transaction=TransactionRecord(
                date=today,
                description="Zelle from John Smith",
                amount=1500.00,
                account="Chase Checking ••1230",
                category="Rental Income (Links Lane)",
            ),
            notes=f"Received {today} — on time (deadline {today}).",
            step_resolved_by=1,
        ),
        PropertyResult(
            property_name="Calmar",
            status=PaymentStatus.MISSING,
            matched_transaction=None,
            notes="No match found after all three steps.",
            step_resolved_by=None,
        ),
        PropertyResult(
            property_name="505",
            status=PaymentStatus.POSSIBLE_MATCH,
            matched_transaction=TransactionRecord(
                date=today,
                description="Online Transfer Credit",
                amount=2200.00,
                account="Chase Checking ••1230",
                category="Transfer",
            ),
            notes="Amount matches but category is wrong. MANUAL REVIEW RECOMMENDED.",
            step_resolved_by=2,
        ),
    ]

    sent = send_notification(
        sample_results, cfg, run_date=today, dry_run=args.dry_run
    )
    if not sent:
        print("Failed to send/preview notification.", file=sys.stderr)
        sys.exit(1)
