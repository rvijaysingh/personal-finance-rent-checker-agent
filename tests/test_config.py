"""Unit tests for src/config_loader.py.

All tests use tmp_path to create isolated config files. No real config
files are read and no external services are called.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.config_loader import AppConfig, ConfigError, load_config, _validate_property
from src.models import PropertyConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_env(tmp_path: Path, **overrides) -> Path:
    """Write a valid .env.json to tmp_path and return its path."""
    data = {
        "gmail_sender": "sender@example.com",
        "gmail_password": "test-app-password",
        "gmail_recipient": "recipient@example.com",
        "monarch_browser_profile_path": "C:\\playwright-profile",
        "ollama_endpoint": "http://localhost:11434",
        "ollama_model": "qwen3:8b",
        **overrides,
    }
    p = tmp_path / ".env.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _make_agent_config(tmp_path: Path, **overrides) -> Path:
    """Write a valid agent_config.json to tmp_path and return its path."""
    data = {
        "scraper_headless": True,
        "properties": [
            {
                "name": "Links Lane",
                "merchant_name": "Alice Smith",
                "expected_rent": 1500.00,
                "due_day": 1,
                "grace_period_days": 5,
                "category_label": "Rental Income (Links Lane)",
                "account": "Chase Checking ••1230",
            }
        ],
        **overrides,
    }
    p = tmp_path / "agent_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _make_prompts(tmp_path: Path) -> Path:
    """Create a prompts/ directory with the required .md files."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "rent_match.md").write_text("Template: {{property_name}}", encoding="utf-8")
    (prompts_dir / "payment_summary.md").write_text("Summary: {{results_json}}", encoding="utf-8")
    return prompts_dir


@pytest.fixture()
def config_env(tmp_path, monkeypatch):
    """Set up a complete valid config environment and patch REPO_ROOT."""
    env_path = _make_env(tmp_path)
    agent_path = _make_agent_config(tmp_path)
    prompts_dir = _make_prompts(tmp_path)

    # Point config_loader at tmp_path as the repo root
    monkeypatch.setenv("ENV_CONFIG_PATH", str(env_path))
    monkeypatch.setattr("src.config_loader.REPO_ROOT", tmp_path)

    # agent_config.json must be at <REPO_ROOT>/config/agent_config.json
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    real_agent = config_dir / "agent_config.json"
    real_agent.write_text(agent_path.read_text(), encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_config_valid_returns_appconfig(config_env):
    """Happy path: all valid config sources produce a fully populated AppConfig."""
    cfg = load_config()

    assert isinstance(cfg, AppConfig)
    assert cfg.gmail_sender == "sender@example.com"
    assert cfg.gmail_recipient == "recipient@example.com"
    assert cfg.ollama_endpoint == "http://localhost:11434"
    assert cfg.ollama_model == "qwen3:8b"
    assert cfg.browser_profile_path == Path("C:\\playwright-profile")
    assert len(cfg.properties) == 1
    assert cfg.properties[0].name == "Links Lane"
    assert cfg.properties[0].expected_rent == 1500.0
    assert cfg.headless is True
    assert "rent_match" in cfg.prompts


def test_load_config_env_path_override_works(tmp_path, monkeypatch):
    """ENV_CONFIG_PATH overrides the default .env.json path."""
    custom_env = tmp_path / "custom_env.json"
    custom_env.write_text(
        json.dumps({
            "gmail_sender": "custom@example.com",
            "gmail_password": "pw",
            "gmail_recipient": "recv@example.com",
            "monarch_browser_profile_path": "C:\\profile",
            "ollama_endpoint": "http://localhost:11434",
            "ollama_model": "qwen3:8b",
        }),
        encoding="utf-8",
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    agent_data = {
        "scraper_headless": True,
        "properties": [{
            "name": "A", "merchant_name": "T", "expected_rent": 100.0,
            "due_day": 1, "grace_period_days": 3,
            "category_label": "Cat A", "account": "Chase",
        }],
    }
    (config_dir / "agent_config.json").write_text(json.dumps(agent_data))
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "rent_match.md").write_text("x")
    (prompts_dir / "payment_summary.md").write_text("x")

    monkeypatch.setenv("ENV_CONFIG_PATH", str(custom_env))
    monkeypatch.setattr("src.config_loader.REPO_ROOT", tmp_path)

    cfg = load_config()
    assert cfg.gmail_sender == "custom@example.com"


# ---------------------------------------------------------------------------
# Missing / invalid env fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_field", [
    "gmail_sender",
    "gmail_password",
    "gmail_recipient",
    "monarch_browser_profile_path",
    "ollama_endpoint",
    "ollama_model",
])
def test_load_config_missing_env_field_raises(config_env, monkeypatch, missing_field):
    """Each required .env.json field raises ConfigError when missing."""
    env_path = Path(os.environ["ENV_CONFIG_PATH"])
    data = json.loads(env_path.read_text())
    del data[missing_field]
    env_path.write_text(json.dumps(data))

    with pytest.raises(ConfigError, match=missing_field):
        load_config()


def test_load_config_env_file_missing_raises(tmp_path, monkeypatch):
    """A non-existent .env.json path raises ConfigError."""
    monkeypatch.setenv("ENV_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
    monkeypatch.setattr("src.config_loader.REPO_ROOT", tmp_path)

    with pytest.raises(ConfigError, match="not found"):
        load_config()


def test_load_config_env_invalid_json_raises(tmp_path, monkeypatch):
    """Malformed .env.json raises ConfigError."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("ENV_CONFIG_PATH", str(bad))
    monkeypatch.setattr("src.config_loader.REPO_ROOT", tmp_path)

    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config()


def test_load_config_invalid_ollama_endpoint_raises(config_env):
    """An ollama_endpoint not starting with 'http' raises ConfigError."""
    env_path = Path(os.environ["ENV_CONFIG_PATH"])
    data = json.loads(env_path.read_text())
    data["ollama_endpoint"] = "ftp://localhost:11434"
    env_path.write_text(json.dumps(data))

    with pytest.raises(ConfigError, match="endpoint"):
        load_config()


# ---------------------------------------------------------------------------
# Missing / invalid agent_config fields
# ---------------------------------------------------------------------------


def test_load_config_missing_agent_config_raises(config_env, monkeypatch):
    """A missing agent_config.json raises ConfigError."""
    (config_env / "config" / "agent_config.json").unlink()

    with pytest.raises(ConfigError, match="agent config not found"):
        load_config()


def test_load_config_missing_properties_raises(config_env):
    """Missing properties key in agent_config raises ConfigError."""
    path = config_env / "config" / "agent_config.json"
    data = json.loads(path.read_text())
    del data["properties"]
    path.write_text(json.dumps(data))

    with pytest.raises(ConfigError, match="properties"):
        load_config()


def test_load_config_empty_properties_raises(config_env):
    """An empty properties list raises ConfigError."""
    path = config_env / "config" / "agent_config.json"
    data = json.loads(path.read_text())
    data["properties"] = []
    path.write_text(json.dumps(data))

    with pytest.raises(ConfigError, match="non-empty list"):
        load_config()


def test_load_config_invalid_scraper_headless_raises(config_env):
    """A non-boolean scraper_headless raises ConfigError."""
    path = config_env / "config" / "agent_config.json"
    data = json.loads(path.read_text())
    data["scraper_headless"] = "yes"
    path.write_text(json.dumps(data))

    with pytest.raises(ConfigError, match="scraper_headless"):
        load_config()


def test_load_config_headless_defaults_to_true(config_env):
    """Omitting scraper_headless defaults to True."""
    path = config_env / "config" / "agent_config.json"
    data = json.loads(path.read_text())
    data.pop("scraper_headless", None)
    path.write_text(json.dumps(data))

    cfg = load_config()
    assert cfg.headless is True


# ---------------------------------------------------------------------------
# Missing prompt files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_prompt", ["rent_match"])
def test_load_config_missing_prompt_raises(config_env, missing_prompt):
    """A missing required prompt file raises ConfigError."""
    (config_env / "prompts" / f"{missing_prompt}.md").unlink()

    with pytest.raises(ConfigError, match=missing_prompt):
        load_config()


def test_load_config_missing_prompts_dir_raises(config_env, monkeypatch):
    """A missing prompts/ directory raises ConfigError."""
    import shutil
    shutil.rmtree(config_env / "prompts")

    with pytest.raises(ConfigError, match="prompts directory"):
        load_config()


# ---------------------------------------------------------------------------
# Property validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_due_day", [0, 29, -1, "first"])
def test_validate_property_invalid_due_day_raises(bad_due_day):
    """due_day outside 1–28 raises ConfigError."""
    data = {
        "name": "Test",
        "merchant_name": "T",
        "expected_rent": 1000.0,
        "due_day": bad_due_day,
        "grace_period_days": 5,
        "category_label": "Cat",
        "account": "Acc",
    }
    with pytest.raises(ConfigError, match="due_day"):
        _validate_property(data, 0)


@pytest.mark.parametrize("bad_rent", [0, -100, "free"])
def test_validate_property_invalid_expected_rent_raises(bad_rent):
    """expected_rent <= 0 or non-numeric raises ConfigError."""
    data = {
        "name": "Test",
        "merchant_name": "T",
        "expected_rent": bad_rent,
        "due_day": 1,
        "grace_period_days": 5,
        "category_label": "Cat",
        "account": "Acc",
    }
    with pytest.raises(ConfigError, match="expected_rent"):
        _validate_property(data, 0)


@pytest.mark.parametrize("bad_grace", [-1, "five", None])
def test_validate_property_invalid_grace_period_raises(bad_grace):
    """grace_period_days < 0 or non-integer raises ConfigError."""
    data = {
        "name": "Test",
        "merchant_name": "T",
        "expected_rent": 1000.0,
        "due_day": 1,
        "grace_period_days": bad_grace,
        "category_label": "Cat",
        "account": "Acc",
    }
    with pytest.raises(ConfigError, match="grace_period_days"):
        _validate_property(data, 0)


def test_validate_property_valid_returns_propertyconfig():
    """A valid property dict returns a PropertyConfig."""
    data = {
        "name": "Links Lane",
        "merchant_name": "Alice",
        "expected_rent": 1500.0,
        "due_day": 1,
        "grace_period_days": 5,
        "category_label": "Rental Income (Links Lane)",
        "account": "Chase Checking ••1230",
    }
    result = _validate_property(data, 0)
    assert isinstance(result, PropertyConfig)
    assert result.name == "Links Lane"
    assert result.expected_rent == 1500.0
    assert result.due_day == 1
