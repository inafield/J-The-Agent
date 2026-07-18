"""Typed application configuration with YAML and environment overrides."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

APP_NAME = "j-the-agent"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / APP_NAME / "config.yaml"
ENV_PREFIX = "J_AGENT_"


class ConfigurationError(ValueError):
    """Raised when configuration cannot be loaded or validated."""


class LLMProvider(StrEnum):
    OLLAMA = "ollama"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"
    GROQ = "groq"


class AccessMode(StrEnum):
    FULL = "full"
    SELECTED = "selected"
    CURRENT_DIRECTORY = "current_directory"


class SafetyProfile(StrEnum):
    RECOMMENDED = "recommended"
    ADVANCED = "advanced"
    CURRENT_DIRECTORY = "current_directory"
    DEFAULTS_PLUS_CUSTOM = "defaults_plus_custom"
    CUSTOM_ONLY = "custom_only"


PROVIDER_DEFAULTS: dict[LLMProvider, dict[str, str]] = {
    LLMProvider.OLLAMA: {"model": "qwen3:8b", "base_url": "http://localhost:11434"},
    LLMProvider.OPENAI: {"model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"},
    LLMProvider.OPENROUTER: {
        "model": "openai/gpt-4o-mini",
        "base_url": "https://openrouter.ai/api/v1",
    },
    LLMProvider.ANTHROPIC: {
        "model": "claude-3-5-haiku-latest",
        "base_url": "https://api.anthropic.com/v1",
    },
    LLMProvider.GROQ: {
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
    },
}


class LLMSettings(BaseModel):
    """Settings shared by every LLM backend."""

    model_config = ConfigDict(extra="forbid")

    provider: LLMProvider = LLMProvider.OLLAMA
    model: str | None = None
    api_key: SecretStr | None = None
    base_url: str | None = None
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, gt=0)
    request_timeout: float = Field(default=120.0, gt=0)

    @field_validator("model")
    @classmethod
    def non_empty_model(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("model cannot be empty")
        return value

    @model_validator(mode="after")
    def custom_url_is_ollama_only(self) -> LLMSettings:
        if self.provider is not LLMProvider.OLLAMA:
            self.base_url = None
        return self

    @property
    def resolved_model(self) -> str:
        return self.model or PROVIDER_DEFAULTS[self.provider]["model"]

    @property
    def resolved_base_url(self) -> str:
        return (self.base_url or PROVIDER_DEFAULTS[self.provider]["base_url"]).rstrip("/")

    @property
    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key.get_secret_value()
        env_names = {
            LLMProvider.OPENAI: ("OPENAI_API_KEY",),
            LLMProvider.OPENROUTER: ("OPENROUTER_API_KEY",),
            LLMProvider.ANTHROPIC: ("ANTHROPIC_API_KEY",),
            LLMProvider.GROQ: ("GROQ_API_KEY",),
            LLMProvider.OLLAMA: (),
        }
        return next(
            (os.environ[name] for name in env_names[self.provider] if os.getenv(name)), None
        )


class SafetySettings(BaseModel):
    """Persistent permission choices consumed by ``SafetyGuard``."""

    model_config = ConfigDict(extra="forbid")

    access_mode: AccessMode = AccessMode.CURRENT_DIRECTORY
    profile: SafetyProfile = SafetyProfile.CURRENT_DIRECTORY
    working_directory: Path = Field(default_factory=Path.cwd)
    allowed_paths: list[Path] = Field(default_factory=list)
    read_only_paths: list[Path] = Field(default_factory=lambda: [Path("/etc")])
    forbidden_patterns: list[str] = Field(default_factory=list)
    forbidden_paths: list[Path] = Field(
        default_factory=lambda: [
            Path("/root"),
            Path("/proc"),
            Path("/sys"),
            Path("/dev"),
            Path("/boot"),
        ]
    )
    confirm_dangerous_commands: bool = True

    @field_validator("allowed_paths", "read_only_paths", "forbidden_paths")
    @classmethod
    def normalize_paths(cls, paths: list[Path]) -> list[Path]:
        return [path.expanduser().absolute() for path in paths]

    @field_validator("working_directory")
    @classmethod
    def normalize_working_directory(cls, path: Path) -> Path:
        return path.expanduser().absolute()


class UISettings(BaseModel):
    """Presentation preferences shared by interactive modes."""

    model_config = ConfigDict(extra="forbid")

    show_reasoning: bool = True
    stream_responses: bool = True


class AgentSettings(BaseModel):
    """Runtime budgets and context controls for bounded agents."""

    model_config = ConfigDict(extra="forbid")

    max_iterations: int = Field(default=6, ge=1, le=20)
    max_tool_calls: int = Field(default=12, ge=1, le=50)
    history_max_messages: int = Field(default=30, ge=4, le=200)
    dynamic_tools: bool = True


class AppConfig(BaseModel):
    """Root configuration object, independent from any particular mode.

    Mode-specific sections (for example ``companion:``) may appear in the same
    YAML file; they are ignored here and owned by the corresponding mode package.
    """

    model_config = ConfigDict(extra="ignore")

    mode: str = "quick"
    llm: LLMSettings = Field(default_factory=LLMSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    ui: UISettings = Field(default_factory=UISettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    plugins_dir: Path = Field(
        default_factory=lambda: Path.home() / ".config" / APP_NAME / "plugins"
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot read configuration from {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Configuration root in {path} must be a mapping")
    return raw


def _environment_overrides() -> dict[str, Any]:
    """Convert ``J_AGENT_LLM__MODEL=x`` variables into nested mappings."""

    result: dict[str, Any] = {}
    ignored = {
        "J_AGENT_BIN_DIR",
        "J_AGENT_CONFIG",
        "J_AGENT_HOME",
        "J_AGENT_REPO_BRANCH",
        "J_AGENT_REPO_URL",
        "J_AGENT_STATE_DIR",
    }
    for name, raw_value in os.environ.items():
        if not name.startswith(ENV_PREFIX) or name in ignored:
            continue
        keys = name.removeprefix(ENV_PREFIX).lower().split("__")
        target = result
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        try:
            target[keys[-1]] = yaml.safe_load(raw_value)
        except yaml.YAMLError:
            target[keys[-1]] = raw_value
    return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load YAML config and apply environment values."""

    env_path = os.getenv("J_AGENT_CONFIG")
    # An environment path selects where first-run setup will save its config;
    # unlike an explicit function argument, it is not evidence that the file
    # must already exist.
    explicit_path = path is not None
    config_path = Path(path or env_path or DEFAULT_CONFIG_PATH).expanduser()
    values: dict[str, Any] = {}
    if config_path.exists():
        values = _read_yaml(config_path)
    elif explicit_path:
        raise ConfigurationError(f"Configuration file does not exist: {config_path}")

    try:
        return AppConfig.model_validate(_deep_merge(values, _environment_overrides()))
    except ValueError as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc


def save_config(config: AppConfig, path: str | Path = DEFAULT_CONFIG_PATH) -> Path:
    """Save shared settings without wiping mode-specific YAML sections."""

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    preserved: dict[str, Any] = {}
    if destination.exists():
        existing = _read_yaml(destination)
        known = set(AppConfig.model_fields)
        preserved = {key: value for key, value in existing.items() if key not in known}
    data = config.model_dump(mode="json")
    if config.llm.api_key:
        data["llm"]["api_key"] = config.llm.api_key.get_secret_value()
    data = {**preserved, **data}
    destination.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    destination.chmod(0o600)
    return destination
