"""Companion-only settings, stored under the ``companion:`` key in config.yaml."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from core.config import APP_NAME, DEFAULT_CONFIG_PATH, ConfigurationError


class WebSearchProvider(StrEnum):
    NONE = "none"
    DUCKDUCKGO = "duckduckgo"
    BRAVE = "brave"
    SEARXNG = "searxng"
    TAVILY = "tavily"


# Labels for arrow-key setup menus: (provider, needs_api_key, needs_base_url)
WEB_SEARCH_OPTIONS: list[tuple[WebSearchProvider, str]] = [
    (WebSearchProvider.DUCKDUCKGO, "DuckDuckGo (no API key; free, less reliable)"),
    (WebSearchProvider.BRAVE, "Brave Search (API key required; recommended)"),
    (WebSearchProvider.SEARXNG, "SearXNG (instance URL required)"),
    (WebSearchProvider.TAVILY, "Tavily (API key required; recommended)"),
    (WebSearchProvider.NONE, "Disabled (no web search)"),
]


def _default_memory_dir() -> Path:
    state = os.getenv("J_AGENT_STATE_DIR")
    root = Path(state).expanduser() if state else Path.home() / ".local/state" / APP_NAME
    return (root / "companion" / "memory").absolute()


class CompanionSettings(BaseModel):
    """Personal Companion mode: memory location, profile, and optional web search."""

    model_config = ConfigDict(extra="forbid")

    memory_dir: Path = Field(default_factory=_default_memory_dir)
    web_provider: WebSearchProvider = WebSearchProvider.DUCKDUCKGO
    web_api_key: SecretStr | None = None
    web_base_url: str | None = None
    # Loopback ports explicitly allowed for fetch_url (e.g. localhost:3000).
    allowed_local_ports: list[int] = Field(default_factory=list)

    # Introduction / profile (also mirrored into user.md and soul.md)
    hello_completed: bool = False
    user_name: str | None = None
    agent_name: str = "J"
    language: str = "en"  # en | ru — default reply language
    address_as: str | None = None
    timezone_city: str = "UTC"
    timezone: str = "UTC"
    work_context: str | None = None
    preferences: str | None = None
    avoid: str | None = None

    @field_validator("memory_dir")
    @classmethod
    def normalize_memory_dir(cls, path: Path) -> Path:
        return path.expanduser().absolute()

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        key = (value or "en").strip().lower()
        return key if key in {"en", "ru"} else "en"

    @field_validator("allowed_local_ports")
    @classmethod
    def normalize_allowed_ports(cls, ports: list[int]) -> list[int]:
        from modes.companion.url_safety import normalize_allowed_ports

        return normalize_allowed_ports(ports)

    @property
    def reminders_path(self) -> Path:
        return self.memory_dir.parent / "reminders.json"


def _config_path(path: str | Path | None = None) -> Path:
    return Path(path or os.getenv("J_AGENT_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()


def load_companion_settings(path: str | Path | None = None) -> CompanionSettings:
    """Load the ``companion`` section from the shared YAML config file."""

    config_path = _config_path(path)
    if not config_path.exists():
        return CompanionSettings()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot read configuration from {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Configuration root in {config_path} must be a mapping")
    section = raw.get("companion") or {}
    if not isinstance(section, dict):
        raise ConfigurationError("Configuration key 'companion' must be a mapping")
    try:
        return CompanionSettings.model_validate(section)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid companion configuration: {exc}") from exc


def save_companion_settings(
    settings: CompanionSettings,
    path: str | Path | None = None,
) -> Path:
    """Update only the ``companion`` section, leaving the rest of the YAML intact."""

    destination = _config_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing: dict[str, Any] = {}
    if destination.exists():
        loaded = yaml.safe_load(destination.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigurationError(f"Configuration root in {destination} must be a mapping")
        existing = loaded
    data = settings.model_dump(mode="json")
    if settings.web_api_key:
        data["web_api_key"] = settings.web_api_key.get_secret_value()
    existing["companion"] = data
    if "mode" not in existing:
        existing["mode"] = "companion"
    destination.write_text(
        yaml.safe_dump(existing, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    destination.chmod(0o600)
    return destination
