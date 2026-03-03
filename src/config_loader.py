"""Configuration loader for the rent payment checker agent.

Loads and validates all three config sources at startup:
  1. .env.json  — secrets (Gmail credentials)
  2. config/agent_config.json — business rules and property definitions
  3. prompts/*.md — LLM prompt templates

All validation happens here. Every other module receives a fully-validated
AppConfig and can trust that required fields are present and type-correct.

Run standalone to verify config on a new machine:
    python -m src.config_loader
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from src.models import PropertyConfig

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent


class ConfigError(Exception):
    """Raised when configuration is missing, invalid, or cannot be loaded."""


@dataclass
class AppConfig:
    """Complete, validated application configuration.

    Produced by load_config() and passed to all other modules.
    """

    # Secrets — from .env.json
    gmail_sender: str
    gmail_password: str
    gmail_recipient: str

    # Matching rules — from agent_config.json
    deposit_account: str
    amount_tolerance_percent: float
    properties: list[PropertyConfig]

    # LLM — from agent_config.json
    ollama_endpoint: str
    ollama_model: str

    # Scraper — from agent_config.json
    browser_profile_path: Path
    headless: bool

    # Derived paths (always repo-relative, not in any config file)
    log_path: Path
    prompts_dir: Path

    # Loaded prompt templates — stem → raw content
    prompts: dict[str, str]


def load_config() -> AppConfig:
    """Load and validate all configuration sources.

    Returns:
        Fully populated AppConfig.

    Raises:
        ConfigError: If any required field is missing, invalid, or a
            required file cannot be found.
    """
    logger.info("Loading configuration")
    env_data = _load_env_json()
    agent_data = _load_agent_config()
    prompts = _load_prompts()
    config = _build_and_validate(env_data, agent_data, prompts)
    logger.info(
        "Configuration loaded: %d properties, deposit_account=%r",
        len(config.properties),
        config.deposit_account,
    )
    return config


# ---------------------------------------------------------------------------
# Private loaders
# ---------------------------------------------------------------------------


def _load_env_json() -> dict:
    """Load secrets from .env.json, respecting ENV_CONFIG_PATH override."""
    env_path_str = os.environ.get("ENV_CONFIG_PATH")
    if env_path_str:
        env_path = Path(env_path_str)
        logger.debug("ENV_CONFIG_PATH set; loading from %s", env_path)
    else:
        env_path = REPO_ROOT / "config" / ".env.json"
        logger.debug("Loading env config from default path: %s", env_path)

    if not env_path.exists():
        raise ConfigError(
            f"missing or invalid field: env config not found at {env_path}. "
            "Copy config/.env.json.example to that path and fill in values."
        )

    try:
        data = json.loads(env_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"missing or invalid field: .env.json is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError("missing or invalid field: .env.json must be a JSON object")

    logger.debug("Loaded env config from %s", env_path)
    return data


def _load_agent_config() -> dict:
    """Load business rules from config/agent_config.json."""
    config_path = REPO_ROOT / "config" / "agent_config.json"
    logger.debug("Loading agent config from %s", config_path)

    if not config_path.exists():
        raise ConfigError(
            f"missing or invalid field: agent config not found at {config_path}. "
            "Copy config/agent_config.json.example and fill in values."
        )

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"missing or invalid field: agent_config.json is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError("missing or invalid field: agent_config.json must be a JSON object")

    logger.debug("Loaded agent config from %s", config_path)
    return data


def _load_prompts() -> dict[str, str]:
    """Load all *.md files from the prompts/ directory."""
    prompts_dir = REPO_ROOT / "prompts"

    if not prompts_dir.exists():
        raise ConfigError(
            f"missing or invalid field: prompts directory not found at {prompts_dir}"
        )

    prompts: dict[str, str] = {}
    for md_file in sorted(prompts_dir.glob("*.md")):
        prompts[md_file.stem] = md_file.read_text(encoding="utf-8")
        logger.debug("Loaded prompt template: %s", md_file.name)

    for required in ("rent_match", "payment_summary"):
        if required not in prompts:
            raise ConfigError(
                f"missing or invalid field: required prompt '{required}.md' "
                f"not found in {prompts_dir}"
            )

    logger.debug("Loaded %d prompt templates: %s", len(prompts), sorted(prompts))
    return prompts


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _req(data: dict, key: str, label: str) -> object:
    """Return data[key] or raise ConfigError naming the field."""
    if key not in data:
        raise ConfigError(f"missing or invalid field: {label}.{key}")
    if data[key] is None:
        raise ConfigError(f"missing or invalid field: {label}.{key} must not be null")
    return data[key]


def _req_str(data: dict, key: str, label: str) -> str:
    """Return a required non-empty string field."""
    value = _req(data, key, label)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"missing or invalid field: {label}.{key} must be a non-empty string"
        )
    return value


def _req_dict(data: dict, key: str, label: str) -> dict:
    """Return a required dict field."""
    value = _req(data, key, label)
    if not isinstance(value, dict):
        raise ConfigError(f"missing or invalid field: {label}.{key} must be an object")
    return value


# ---------------------------------------------------------------------------
# Config assembly
# ---------------------------------------------------------------------------


def _build_and_validate(
    env: dict,
    agent: dict,
    prompts: dict[str, str],
) -> AppConfig:
    """Assemble and validate AppConfig from raw dicts."""
    # Secrets
    gmail_sender = _req_str(env, "gmail_sender", ".env.json")
    gmail_password = _req_str(env, "gmail_password", ".env.json")
    gmail_recipient = _req_str(env, "gmail_recipient", ".env.json")

    # Matching section
    matching = _req_dict(agent, "matching", "agent_config")
    deposit_account = _req_str(matching, "deposit_account", "agent_config.matching")
    raw_tol = _req(matching, "amount_tolerance_percent", "agent_config.matching")
    try:
        amount_tolerance_percent = float(raw_tol)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigError(
            "missing or invalid field: agent_config.matching.amount_tolerance_percent "
            "must be a number"
        )
    if not (0 < amount_tolerance_percent <= 100):
        raise ConfigError(
            f"missing or invalid field: agent_config.matching.amount_tolerance_percent "
            f"must be > 0 and <= 100, got {amount_tolerance_percent}"
        )

    # Properties
    raw_props = _req(agent, "properties", "agent_config")
    if not isinstance(raw_props, list) or len(raw_props) == 0:
        raise ConfigError(
            "missing or invalid field: agent_config.properties must be a non-empty list"
        )
    properties = [_validate_property(p, i) for i, p in enumerate(raw_props)]

    # Ollama
    ollama = _req_dict(agent, "ollama", "agent_config")
    ollama_endpoint = _req_str(ollama, "endpoint", "agent_config.ollama")
    if not ollama_endpoint.startswith("http"):
        raise ConfigError(
            f"missing or invalid field: agent_config.ollama.endpoint must start "
            f"with 'http', got: {ollama_endpoint!r}"
        )
    ollama_model = _req_str(ollama, "model", "agent_config.ollama")

    # Scraper
    scraper = _req_dict(agent, "scraper", "agent_config")
    browser_profile_str = _req_str(
        scraper, "browser_profile_path", "agent_config.scraper"
    )
    headless = scraper.get("headless", True)
    if not isinstance(headless, bool):
        raise ConfigError(
            "missing or invalid field: agent_config.scraper.headless must be true or false"
        )

    return AppConfig(
        gmail_sender=gmail_sender,
        gmail_password=gmail_password,
        gmail_recipient=gmail_recipient,
        deposit_account=deposit_account,
        amount_tolerance_percent=amount_tolerance_percent,
        properties=properties,
        ollama_endpoint=ollama_endpoint,
        ollama_model=ollama_model,
        browser_profile_path=Path(browser_profile_str),
        headless=headless,
        log_path=REPO_ROOT / "logs" / "run_history.json",
        prompts_dir=REPO_ROOT / "prompts",
        prompts=prompts,
    )


def _validate_property(data: object, index: int) -> PropertyConfig:
    """Validate a single property entry and return a PropertyConfig."""
    label = f"agent_config.properties[{index}]"
    if not isinstance(data, dict):
        raise ConfigError(f"missing or invalid field: {label} must be an object")

    name = _req_str(data, "name", label)
    tenant_name = _req_str(data, "tenant_name", label)
    category_label = _req_str(data, "category_label", label)
    account = _req_str(data, "account", label)

    raw_rent = _req(data, "expected_rent", label)
    try:
        expected_rent = float(raw_rent)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConfigError(
            f"missing or invalid field: {label}.expected_rent must be a number"
        )
    if expected_rent <= 0:
        raise ConfigError(
            f"missing or invalid field: {label}.expected_rent must be > 0, "
            f"got {expected_rent}"
        )

    raw_due_day = _req(data, "due_day", label)
    if not isinstance(raw_due_day, int) or not (1 <= raw_due_day <= 28):
        raise ConfigError(
            f"missing or invalid field: {label}.due_day must be an integer 1–28, "
            f"got {raw_due_day!r}"
        )

    raw_grace = _req(data, "grace_period_days", label)
    if not isinstance(raw_grace, int) or raw_grace < 0:
        raise ConfigError(
            f"missing or invalid field: {label}.grace_period_days must be an "
            f"integer >= 0, got {raw_grace!r}"
        )

    return PropertyConfig(
        name=name,
        tenant_name=tenant_name,
        expected_rent=expected_rent,
        due_day=raw_due_day,
        grace_period_days=raw_grace,
        category_label=category_label,
        account=account,
    )


# ---------------------------------------------------------------------------
# Standalone verification
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Config loaded successfully.")
    print(f"  Gmail sender:     {cfg.gmail_sender}")
    print(f"  Gmail password:   {'***' if cfg.gmail_password else '(empty)'}")
    print(f"  Gmail recipient:  {cfg.gmail_recipient}")
    print(f"  Deposit account:  {cfg.deposit_account}")
    print(f"  Tolerance:        {cfg.amount_tolerance_percent}%")
    print(f"  Ollama:           {cfg.ollama_endpoint} / {cfg.ollama_model}")
    print(f"  Browser profile:  {cfg.browser_profile_path}")
    print(f"  Headless:         {cfg.headless}")
    print(f"  Log path:         {cfg.log_path}")
    print(f"  Properties ({len(cfg.properties)}):")
    for prop in cfg.properties:
        print(
            f"    [{prop.name}] tenant={prop.tenant_name!r}  "
            f"rent=${prop.expected_rent:.2f}  due=day {prop.due_day}  "
            f"grace={prop.grace_period_days}d  "
            f"category={prop.category_label!r}"
        )
    print(f"  Prompts: {sorted(cfg.prompts)}")
