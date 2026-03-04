"""Run coordinator tests — R-01 through R-04.

Tests the idempotency check and run history helpers in orchestrator.py.
All tests use tmp_path for isolated file I/O. No real network calls.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.orchestrator import _check_already_run, _load_run_history, _write_run_record
from tests.conftest import FIXTURE_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copy_fixture(fixture_name: str, dest: Path) -> None:
    dest.write_text((FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"), encoding="utf-8")


def _write_history(path: Path, records: list[dict]) -> None:
    path.write_text(json.dumps(records), encoding="utf-8")


# ---------------------------------------------------------------------------
# R-01: Already ran successfully this month → skip
# ---------------------------------------------------------------------------


def test_r01_already_completed_this_month_returns_true(tmp_path):
    """R-01: run_history.json has 'completed' for current month → _check_already_run returns (True, 'completed')."""
    log_path = tmp_path / "run_history.json"
    _copy_fixture("run_history_success.json", log_path)

    already_run, status = _check_already_run(log_path, date(2026, 3, 15))

    assert already_run is True
    assert status == "completed"


def test_r01_completed_email_failed_detected_as_already_run(tmp_path):
    """R-01: 'completed_email_failed' status → _check_already_run returns (True, 'completed_email_failed')."""
    log_path = tmp_path / "run_history.json"
    records = [
        {
            "run_date": "2026-03-01T10:00:00",
            "check_month": "2026-03",
            "overall_status": "completed_email_failed",
            "email_sent": False,
            "errors": [],
            "property_results": [],
        }
    ]
    _write_history(log_path, records)

    already_run, status = _check_already_run(log_path, date(2026, 3, 15))

    assert already_run is True
    assert status == "completed_email_failed"


def test_r01_previous_month_completed_does_not_block_current_month(tmp_path):
    """R-01: Previous month 'completed' does not trigger skip for a different month."""
    log_path = tmp_path / "run_history.json"
    records = [
        {
            "run_date": "2026-02-01T10:00:00",
            "check_month": "2026-02",
            "overall_status": "completed",
            "email_sent": True,
            "errors": [],
            "property_results": [],
        }
    ]
    _write_history(log_path, records)

    already_run, status = _check_already_run(log_path, date(2026, 3, 1))

    assert already_run is False
    assert status is None


# ---------------------------------------------------------------------------
# R-02: Previous run had errors → allow re-run
# ---------------------------------------------------------------------------


def test_r02_error_status_does_not_block_rerun(tmp_path):
    """R-02: 'error' status for this month → _check_already_run returns (False, None)."""
    log_path = tmp_path / "run_history.json"
    _copy_fixture("run_history_errors.json", log_path)

    already_run, status = _check_already_run(log_path, date(2026, 3, 15))

    assert already_run is False
    assert status is None


def test_r02_action_needed_status_does_not_block_rerun(tmp_path):
    """R-02: 'action_needed' is not a terminal status → allows re-run."""
    log_path = tmp_path / "run_history.json"
    records = [
        {
            "run_date": "2026-03-01T10:00:00",
            "check_month": "2026-03",
            "overall_status": "action_needed",
            "email_sent": True,
            "errors": [],
            "property_results": [],
        }
    ]
    _write_history(log_path, records)

    already_run, status = _check_already_run(log_path, date(2026, 3, 15))

    # 'action_needed' is treated like 'completed' in the idempotency check
    # (both indicate the check ran successfully). Verify the behaviour.
    # NOTE: current orchestrator code checks for "completed" and
    # "completed_email_failed" only. "action_needed" does not trigger skip.
    assert already_run is False


# ---------------------------------------------------------------------------
# R-03: No run_history file → treat as empty, proceed
# ---------------------------------------------------------------------------


def test_r03_missing_file_returns_empty_list(tmp_path):
    """R-03: Non-existent run_history.json → _load_run_history returns []."""
    log_path = tmp_path / "nonexistent_run_history.json"
    assert not log_path.exists()

    result = _load_run_history(log_path)

    assert result == []


def test_r03_missing_file_check_already_run_returns_false(tmp_path):
    """R-03: Missing file → _check_already_run returns (False, None); run proceeds."""
    log_path = tmp_path / "nonexistent.json"

    already_run, status = _check_already_run(log_path, date(2026, 3, 1))

    assert already_run is False
    assert status is None


# ---------------------------------------------------------------------------
# R-04: Corrupted run_history.json → treat as no history
# ---------------------------------------------------------------------------


def test_r04_corrupted_json_load_returns_empty_list(tmp_path):
    """R-04: Corrupt JSON in run_history.json → _load_run_history returns [] with warning."""
    log_path = tmp_path / "run_history.json"
    _copy_fixture("run_history_corrupt.json", log_path)

    result = _load_run_history(log_path)

    assert result == []


def test_r04_non_array_json_returns_empty_list(tmp_path):
    """R-04: run_history.json contains a JSON object (not array) → [] returned."""
    log_path = tmp_path / "run_history.json"
    log_path.write_text('{"not": "an array"}', encoding="utf-8")

    result = _load_run_history(log_path)

    assert result == []


def test_r04_corrupted_file_check_already_run_proceeds(tmp_path):
    """R-04: Corrupt file → _check_already_run returns (False, None); check not skipped."""
    log_path = tmp_path / "run_history.json"
    _copy_fixture("run_history_corrupt.json", log_path)

    already_run, status = _check_already_run(log_path, date(2026, 3, 1))

    assert already_run is False
    assert status is None


# ---------------------------------------------------------------------------
# Write run record tests (auxiliary)
# ---------------------------------------------------------------------------


def test_write_run_record_creates_file_and_appends(tmp_path):
    """Writing a run record creates the file if it does not exist."""
    log_path = tmp_path / "logs" / "run_history.json"

    _write_run_record(
        log_path,
        run_date=date(2026, 3, 1),
        results=[],
        overall_status="completed",
        errors=[],
        email_sent=True,
        dry_run=False,
    )

    assert log_path.exists()
    data = json.loads(log_path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["overall_status"] == "completed"
    assert data[0]["check_month"] == "2026-03"


def test_write_run_record_dry_run_does_not_write(tmp_path):
    """dry_run=True prevents any file write."""
    log_path = tmp_path / "run_history.json"

    _write_run_record(
        log_path,
        run_date=date(2026, 3, 1),
        results=[],
        overall_status="completed",
        errors=[],
        email_sent=True,
        dry_run=True,
    )

    assert not log_path.exists()
