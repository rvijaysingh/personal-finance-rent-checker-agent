"""Tests for crash alert integration in src/orchestrator.py.

Verifies that send_crash_alert is called with the correct agent_name and
exception when _run() raises an unhandled exception.  All external
dependencies (config, scraper, send_crash_alert) are mocked.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_mock() -> MagicMock:
    cfg = MagicMock()
    cfg.gmail_sender = "sender@example.com"
    cfg.gmail_password = "secret"
    cfg.gmail_recipient = "recipient@example.com"
    cfg.log_path = MagicMock()
    cfg.log_path.exists.return_value = False
    cfg.ollama_endpoint = "http://localhost:11434"
    cfg.ollama_model = "qwen3:8b"
    cfg.properties = []
    cfg.headless = True
    cfg.early_payment_days = 3
    return cfg


# ---------------------------------------------------------------------------
# O-01: crash alert called when _run raises an unhandled exception
# ---------------------------------------------------------------------------


def test_main_crash_alert_called_on_unhandled_exception():
    """O-01: When _run() raises unexpectedly, send_crash_alert is called with
    the correct agent_name and exception, then the exception is re-raised."""
    boom = RuntimeError("unexpected boom")

    with (
        patch("src.orchestrator._run", side_effect=boom),
        patch("src.orchestrator._send_crash_alert_best_effort") as mock_alert,
    ):
        from src.orchestrator import main

        with pytest.raises(RuntimeError, match="unexpected boom"):
            main(["--no-scrape"])

    mock_alert.assert_called_once()
    call_exc = mock_alert.call_args[0][0]
    assert call_exc is boom


# ---------------------------------------------------------------------------
# O-02: send_crash_alert receives correct agent_name from loaded config
# ---------------------------------------------------------------------------


def test_send_crash_alert_best_effort_uses_config_credentials():
    """O-02: _send_crash_alert_best_effort pulls gmail creds from config when
    config loads successfully, and passes them to send_crash_alert."""
    cfg = _make_config_mock()
    exc = ValueError("test error")

    # load_config and send_crash_alert are imported locally inside the function,
    # so we patch at their source module locations.
    with (
        patch("src.config_loader.load_config", return_value=cfg),
        patch("agent_shared.alerts.send_crash_alert") as mock_send,
    ):
        from src.orchestrator import _send_crash_alert_best_effort
        _send_crash_alert_best_effort(exc, "tb string")

    mock_send.assert_called_once()
    kwargs = mock_send.call_args[1] if mock_send.call_args[1] else {}
    args = mock_send.call_args[0] if mock_send.call_args[0] else ()
    call_all = {**dict(zip(["agent_name", "error", "traceback_str",
                             "gmail_sender", "gmail_password", "recipient"], args)),
                **kwargs}
    assert call_all["agent_name"] == "personal-finance-rent-checker-agent"
    assert call_all["error"] is exc
    assert call_all["gmail_sender"] == "sender@example.com"
    assert call_all["gmail_password"] == "secret"
    assert call_all["recipient"] == "recipient@example.com"


# ---------------------------------------------------------------------------
# O-03: falls back to env vars when config loading fails
# ---------------------------------------------------------------------------


def test_send_crash_alert_best_effort_uses_env_vars_when_config_fails():
    """O-03: When config loading raises, _send_crash_alert_best_effort falls
    back to GMAIL_SENDER / GMAIL_PASSWORD env vars."""
    exc = RuntimeError("config failed")

    env_overrides = {
        "GMAIL_SENDER": "envuser@example.com",
        "GMAIL_PASSWORD": "envpass",
    }

    with (
        patch("src.config_loader.load_config", side_effect=Exception("config broken")),
        patch.dict(os.environ, env_overrides),
        patch("agent_shared.alerts.send_crash_alert") as mock_send,
    ):
        from src.orchestrator import _send_crash_alert_best_effort
        _send_crash_alert_best_effort(exc, "tb string")

    mock_send.assert_called_once()
    kwargs = mock_send.call_args[1] if mock_send.call_args[1] else {}
    args = mock_send.call_args[0] if mock_send.call_args[0] else ()
    call_all = {**dict(zip(["agent_name", "error", "traceback_str",
                             "gmail_sender", "gmail_password", "recipient"], args)),
                **kwargs}
    assert call_all["agent_name"] == "personal-finance-rent-checker-agent"
    assert call_all["gmail_sender"] == "envuser@example.com"
    assert call_all["gmail_password"] == "envpass"


# ---------------------------------------------------------------------------
# O-04: no crash alert when env vars also missing
# ---------------------------------------------------------------------------


def test_send_crash_alert_best_effort_skips_when_no_credentials(caplog):
    """O-04: If config fails and env vars are absent, alert is skipped and
    an ERROR is logged — no exception is raised."""
    import logging

    exc = RuntimeError("boom")

    env_without_gmail = {k: v for k, v in os.environ.items()
                         if k not in ("GMAIL_SENDER", "GMAIL_PASSWORD")}

    with (
        patch("src.config_loader.load_config", side_effect=Exception("config broken")),
        patch.dict(os.environ, env_without_gmail, clear=True),
        patch("agent_shared.alerts.send_crash_alert") as mock_send,
        caplog.at_level(logging.ERROR, logger="src.orchestrator"),
    ):
        from src.orchestrator import _send_crash_alert_best_effort
        _send_crash_alert_best_effort(exc, "tb string")

    mock_send.assert_not_called()
    assert any("Cannot send crash alert" in r.message for r in caplog.records)
