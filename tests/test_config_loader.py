"""Config loader tests — C-01 through C-05.

All tests write config files to pytest's tmp_path. No real config files
are read; no external services are called.

These tests complement tests/test_config.py and focus specifically on
the test plan IDs C-01 to C-05.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.config_loader import ConfigError, load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_env(tmp_path: Path, **overrides) -> Path:
    data = {
        "gmail_sender": "sender@example.com",
        "gmail_password": "test-app-password",
        "gmail_recipient": "recv@example.com",
        "monarch_browser_profile_path": "C:\\playwright-profile",
        "ollama_endpoint": "http://localhost:11434",
        "ollama_model": "qwen3:8b",
        **overrides,
    }
    p = tmp_path / ".env.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _write_agent_config(tmp_path: Path, **overrides) -> Path:
    data = {
        "scraper_headless": True,
        "properties": [
            {
                "name": "Links Lane",
                "merchant_name": "JANE DOE",
                "expected_rent": 2950.00,
                "due_day": 1,
                "grace_period_days": 5,
                "category_label": "Rental Income (Links Lane)",
                "account": "Chase Checking \u20221230",
            }
        ],
        **overrides,
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    p = config_dir / "agent_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _write_prompts(tmp_path: Path) -> None:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "rent_match.md").write_text("Template: {{property_name}}", encoding="utf-8")


def _setup_valid_env(tmp_path: Path, monkeypatch) -> Path:
    """Set up a complete valid config environment. Returns tmp_path."""
    env_path = _write_env(tmp_path)
    _write_agent_config(tmp_path)
    _write_prompts(tmp_path)
    monkeypatch.setenv("ENV_CONFIG_PATH", str(env_path))
    monkeypatch.setattr("src.config_loader.REPO_ROOT", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# C-01: Missing .env.json → clear error message
# ---------------------------------------------------------------------------


def test_c01_missing_env_json_raises_with_clear_message(tmp_path, monkeypatch):
    """C-01: .env.json absent → ConfigError names the file and says 'not found'."""
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setenv("ENV_CONFIG_PATH", str(missing))
    monkeypatch.setattr("src.config_loader.REPO_ROOT", tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        load_config()

    msg = str(exc_info.value)
    assert "not found" in msg
    assert "env" in msg.lower() or str(missing) in msg


# ---------------------------------------------------------------------------
# C-02: Missing agent_config.json → clear error message
# ---------------------------------------------------------------------------


def test_c02_missing_agent_config_raises_with_clear_message(tmp_path, monkeypatch):
    """C-02: agent_config.json absent → ConfigError with 'agent config not found'."""
    env_path = _write_env(tmp_path)
    _write_prompts(tmp_path)
    # Deliberately do NOT write agent_config.json
    monkeypatch.setenv("ENV_CONFIG_PATH", str(env_path))
    monkeypatch.setattr("src.config_loader.REPO_ROOT", tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        load_config()

    assert "agent config not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# C-03: Missing required property field (merchant_name) → validation error
# ---------------------------------------------------------------------------


def test_c03_missing_merchant_name_raises_naming_field(tmp_path, monkeypatch):
    """C-03: Property entry missing 'merchant_name' → ConfigError names the field."""
    _setup_valid_env(tmp_path, monkeypatch)

    # Overwrite agent_config without merchant_name
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    bad_config = {
        "scraper_headless": True,
        "properties": [
            {
                "name": "Links Lane",
                # merchant_name deliberately omitted
                "expected_rent": 2950.00,
                "due_day": 1,
                "grace_period_days": 5,
                "category_label": "Rental Income (Links Lane)",
                "account": "Chase Checking \u20221230",
            }
        ],
    }
    (config_dir / "agent_config.json").write_text(json.dumps(bad_config), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_config()

    assert "merchant_name" in str(exc_info.value)


def test_c03_fixture_agent_config_missing_field_has_no_merchant_name():
    """C-03 (fixture validation): agent_config_missing_field.json is missing merchant_name."""
    from tests.conftest import FIXTURE_DIR
    data = json.loads((FIXTURE_DIR / "agent_config_missing_field.json").read_text())
    assert "merchant_name" not in data["properties"][0]


# ---------------------------------------------------------------------------
# C-04: Invalid JSON syntax → clear error, not raw traceback
# ---------------------------------------------------------------------------


def test_c04_invalid_env_json_syntax_raises_config_error_not_json_error(tmp_path, monkeypatch):
    """C-04: .env.json with invalid JSON → ConfigError (not JSONDecodeError) with clear message."""
    bad = tmp_path / "bad_env.json"
    bad.write_text("{not valid json at all", encoding="utf-8")
    monkeypatch.setenv("ENV_CONFIG_PATH", str(bad))
    monkeypatch.setattr("src.config_loader.REPO_ROOT", tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        load_config()

    msg = str(exc_info.value)
    assert "not valid JSON" in msg
    # Must be ConfigError, not bare JSONDecodeError
    assert not isinstance(exc_info.value, json.JSONDecodeError)


def test_c04_invalid_agent_config_syntax_raises_config_error(tmp_path, monkeypatch):
    """C-04: agent_config.json with invalid JSON → ConfigError with 'not valid JSON'."""
    env_path = _write_env(tmp_path)
    _write_prompts(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_config.json").write_text("{bad json", encoding="utf-8")
    monkeypatch.setenv("ENV_CONFIG_PATH", str(env_path))
    monkeypatch.setattr("src.config_loader.REPO_ROOT", tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        load_config()

    assert "not valid JSON" in str(exc_info.value)


# ---------------------------------------------------------------------------
# C-05: Empty properties array → descriptive startup error
# ---------------------------------------------------------------------------


def test_c05_empty_properties_array_raises_with_description(tmp_path, monkeypatch):
    """C-05: properties=[] → ConfigError with 'non-empty list' in message."""
    _setup_valid_env(tmp_path, monkeypatch)

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    empty_props = {"scraper_headless": True, "properties": []}
    (config_dir / "agent_config.json").write_text(json.dumps(empty_props), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_config()

    assert "non-empty list" in str(exc_info.value)
