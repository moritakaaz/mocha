"""Configuration loading: environment variables plus ``config.yaml``.

Manual channel mappings in ``config.yaml`` always win over same-name
auto-discovery of a Discord text channel in the target guild.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("bridge.config")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ChannelRule:
    """One manual mapping between a Slack channel and a Discord channel."""

    slack: str            # Slack channel name (no #) or channel ID
    discord: str          # Discord channel name (no #) or channel ID
    webhook: str | None = None  # Optional per-channel Discord webhook URL


@dataclass
class Config:
    slack_bot_token: str = ""
    slack_app_token: str = ""
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    auto_create_channels: bool = False
    auto_join_public_channels: bool = True
    max_file_mb: float = 25.0
    log_level: str = "INFO"
    name_format: str = "display"  # display | real | user
    impersonate: bool = False     # auto-manage per-channel webhooks
    db_path: str = "data/bridge.db"
    config_path: str = "config.yaml"
    rules: list[ChannelRule] = field(default_factory=list)

    # --- mapping helpers -------------------------------------------
    def _norm(self, ref: str) -> str:
        return ref.lstrip("#").strip()

    def rule_for_slack(self, slack_ref: str) -> ChannelRule | None:
        """Return the manual rule for a Slack channel name or ID."""
        want = self._norm(slack_ref).lower()
        for rule in self.rules:
            if rule.slack.lstrip("#").strip().lower() == want:
                return rule
        return None

    def webhook_for(self, slack_ref: str) -> str | None:
        """Return the webhook URL configured for a Slack channel, if any."""
        rule = self.rule_for_slack(slack_ref)
        return rule.webhook if rule else None

    def discord_ref_for(self, slack_ref: str) -> str | None:
        """Return the configured Discord channel ref for a Slack channel."""
        rule = self.rule_for_slack(slack_ref)
        return rule.discord if rule else self._norm(slack_ref)

    @staticmethod
    def looks_like_id(ref: str) -> bool:
        """Discord channel IDs are all digits."""
        return ref.isdigit()


def load_env_file(path: str) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (no override)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def load_config(env: dict[str, str] | None = None,
                config_path: str | None = None) -> Config:
    """Build a Config from the environment and optionally config.yaml."""
    env = env if env is not None else os.environ
    config_path = config_path or env.get("BRIDGE_CONFIG_PATH", "config.yaml")

    cfg = Config(
        slack_bot_token=env.get("SLACK_BOT_TOKEN", ""),
        slack_app_token=env.get("SLACK_APP_TOKEN", ""),
        discord_bot_token=env.get("DISCORD_BOT_TOKEN", ""),
        discord_guild_id=env.get("DISCORD_GUILD_ID", ""),
        auto_create_channels=_as_bool(env.get("AUTO_CREATE_CHANNELS")),
        auto_join_public_channels=_as_bool(
            env.get("AUTO_JOIN_PUBLIC_CHANNELS"), default=True),
        max_file_mb=float(env.get("DISCORD_MAX_FILE_MB", "25") or "25"),
        log_level=(env.get("LOG_LEVEL", "INFO") or "INFO").upper(),
        db_path=env.get("BRIDGE_DB_PATH", "data/bridge.db"),
        config_path=config_path,
    )

    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        fmt = str(data.get("name_format", "display")).lower()
        cfg.name_format = fmt if fmt in ("display", "real", "user") \
            else "display"
        if "impersonate" in data:
            cfg.impersonate = _as_bool(str(data.get("impersonate")))
        for entry in data.get("channels", []) or []:
            cfg.rules.append(ChannelRule(
                slack=str(entry.get("slack", "")),
                discord=str(entry.get("discord", "")),
                webhook=entry.get("webhook") or None,
            ))
        log.info("Loaded %d channel rule(s) from %s", len(cfg.rules),
                 config_path)
    else:
        log.info("No config file at %s; using auto-discovery only",
                 config_path)
    return cfg
