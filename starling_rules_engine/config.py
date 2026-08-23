"""Loads and validates config.yaml + secrets from the environment.

Secrets (the Starling personal access token, and optionally an alert
webhook URL) are read from environment variables - never from the YAML
config file - so config.yaml never leaks anything if you copy/paste or
back it up somewhere. Load them from a local .env file (not committed)
via python-dotenv when running by hand or from cron; see .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .safety import SafetyLimits


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    employer_names: list[str]
    credit_card_payee_name: str
    credit_card_payment_reference: Optional[str]
    account_uid: Optional[str]
    currency: str
    safety: SafetyLimits
    dry_run: bool
    sandbox: bool
    poll_lookback_minutes: int
    state_path: Path
    starling_token: str
    alert_webhook_url: Optional[str]
    signing_key_uid: Optional[str]
    signing_private_key_path: Optional[Path]
    reconciliation_match_window_days: int


def load_config(config_path: Path) -> Config:
    if not config_path.exists():
        raise ConfigError(
            f"config file not found: {config_path} (copy config.example.yaml to config.yaml first)"
        )
    with open(config_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    def require(key: str):
        if key not in raw:
            raise ConfigError(f"config.yaml missing required key: {key}")
        return raw[key]

    token = os.environ.get("STARLING_PERSONAL_ACCESS_TOKEN")
    if not token:
        raise ConfigError("STARLING_PERSONAL_ACCESS_TOKEN environment variable is not set (see .env.example)")

    employer_names = require("employer_names")
    if not isinstance(employer_names, list) or not employer_names:
        raise ConfigError("employer_names must be a non-empty list")

    safety_raw = require("safety")
    try:
        safety = SafetyLimits(
            max_transfer_minor_units=int(safety_raw["max_transfer_minor_units"]),
            max_daily_total_minor_units=int(safety_raw["max_daily_total_minor_units"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid safety config: {exc}") from exc

    reconciliation_raw = raw.get("reconciliation") or {}
    try:
        reconciliation_match_window_days = int(reconciliation_raw.get("match_window_days", 14))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid reconciliation.match_window_days: {exc}") from exc
    if reconciliation_match_window_days < 0:
        raise ConfigError("reconciliation.match_window_days must be >= 0")

    return Config(
        employer_names=employer_names,
        credit_card_payee_name=require("credit_card_payee_name"),
        credit_card_payment_reference=raw.get("credit_card_payment_reference") or None,
        account_uid=raw.get("account_uid"),
        currency=raw.get("currency", "GBP"),
        safety=safety,
        dry_run=bool(raw.get("dry_run", True)),
        sandbox=bool(raw.get("sandbox", False)),
        poll_lookback_minutes=int(raw.get("poll_lookback_minutes", 1440)),
        state_path=Path(raw.get("state_path", "state.json")),
        starling_token=token,
        alert_webhook_url=os.environ.get("STARLING_RULES_ALERT_WEBHOOK_URL") or None,
        signing_key_uid=raw.get("signing_key_uid") or None,
        signing_private_key_path=(
            Path(raw["signing_private_key_path"]).expanduser() if raw.get("signing_private_key_path") else None
        ),
        reconciliation_match_window_days=reconciliation_match_window_days,
    )
