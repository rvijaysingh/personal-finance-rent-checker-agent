"""Configuration loader for the rent payment checker agent.

Loads and validates all three config sources at startup:
  1. .env.json  — machine-local file containing secrets, machine-specific
                  paths, and Ollama settings (shared across projects on
                  the same machine)
  2. config/agent_config.json — project-specific business rules:
                  property definitions and scraper_headless flag
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

    # Machine-specific paths — from .env.json
    browser_profile_path: Path

    # LLM settings — from .env.json
    ollama_endpoint: str
    ollama_model: str

    # Property definitions — from agent_config.json
    properties: list[PropertyConfig]

    # Scraper behaviour — from agent_config.json
    headless: bool
    early_payment_days: int   # how many days before the 1st to look back

    # Email settings — from agent_config.json
    email_subject_prefix: str

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
        "Configuration loaded: %d properties, ollama=%s",
        len(config.properties),
        config.ollama_endpoint,
    )
    return config


# ---------------------------------------------------------------------------
# Private loaders
# ---------------------------------------------------------------------------


def _load_env_json() -> dict:
    """Load the machine-local .env.json, respecting ENV_CONFIG_PATH override."""
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
    """Load project-specific business rules from config/agent_config.json."""
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


# ---------------------------------------------------------------------------
# Config assembly
# ---------------------------------------------------------------------------


def _build_and_validate(
    env: dict,
    agent: dict,
    prompts: dict[str, str],
) -> AppConfig:
    """Assemble and validate AppConfig from raw dicts."""
    # Gmail secrets — from .env.json
    gmail_sender = _req_str(env, "gmail_sender", ".env.json")
    gmail_password = _req_str(env, "gmail_password", ".env.json")
    gmail_recipient = _req_str(env, "gmail_recipient", ".env.json")

    # Machine-specific path — from .env.json
    browser_profile_str = _req_str(env, "monarch_browser_profile_path", ".env.json")

    # Ollama settings — from .env.json
    ollama_endpoint = _req_str(env, "ollama_endpoint", ".env.json")
    if not ollama_endpoint.startswith("http"):
        raise ConfigError(
            f"missing or invalid field: .env.json.ollama_endpoint must start "
            f"with 'http', got: {ollama_endpoint!r}"
        )
    ollama_model = _req_str(env, "ollama_model", ".env.json")

    # Property definitions — from agent_config.json
    raw_props = _req(agent, "properties", "agent_config")
    if not isinstance(raw_props, list) or len(raw_props) == 0:
        raise ConfigError(
            "missing or invalid field: agent_config.properties must be a non-empty list"
        )
    properties = [_validate_property(p, i) for i, p in enumerate(raw_props)]

    # Scraper behaviour — from agent_config.json (optional, defaults to True)
    headless = agent.get("scraper_headless", True)
    if not isinstance(headless, bool):
        raise ConfigError(
            "missing or invalid field: agent_config.scraper_headless must be "
            "true or false"
        )

    # Early payment lookback — from agent_config.json (optional, defaults to 3)
    early_payment_days = agent.get("early_payment_days", 3)
    if not isinstance(early_payment_days, int) or early_payment_days < 0:
        raise ConfigError(
            "missing or invalid field: agent_config.early_payment_days must be "
            f"an integer >= 0, got {early_payment_days!r}"
        )

    # Email subject prefix — from agent_config.json (optional)
    email_subject_prefix = agent.get("email_subject_prefix", "[Agent - Rent Check]")
    if not isinstance(email_subject_prefix, str) or not email_subject_prefix.strip():
        raise ConfigError(
            "missing or invalid field: agent_config.email_subject_prefix must be "
            f"a non-empty string, got {email_subject_prefix!r}"
        )

    return AppConfig(
        gmail_sender=gmail_sender,
        gmail_password=gmail_password,
        gmail_recipient=gmail_recipient,
        browser_profile_path=Path(browser_profile_str),
        ollama_endpoint=ollama_endpoint,
        ollama_model=ollama_model,
        properties=properties,
        headless=headless,
        early_payment_days=early_payment_days,
        email_subject_prefix=email_subject_prefix,
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
    merchant_name = _req_str(data, "merchant_name", label)
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
        merchant_name=merchant_name,
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
    print(f"  Browser profile:  {cfg.browser_profile_path}")
    print(f"  Ollama:           {cfg.ollama_endpoint} / {cfg.ollama_model}")
    print(f"  Headless:         {cfg.headless}")
    print(f"  Early pay days:   {cfg.early_payment_days}")
    print(f"  Email prefix:     {cfg.email_subject_prefix}")
    print(f"  Log path:         {cfg.log_path}")
    print(f"  Properties ({len(cfg.properties)}):")
    for prop in cfg.properties:
        print(
            f"    [{prop.name}] merchant={prop.merchant_name!r}  "
            f"rent=${prop.expected_rent:.2f}  due=day {prop.due_day}  "
            f"grace={prop.grace_period_days}d  "
            f"category={prop.category_label!r}"
        )
    print(f"  Prompts: {sorted(cfg.prompts)}")
