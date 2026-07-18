"""Resolve which product mode the installed ``ja`` / ``agent`` should run.

An official install (``scripts/install.sh``) ships ``core`` plus exactly one of
``modes/quick`` or ``modes/companion``. They must not import each other.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from core.config import DEFAULT_CONFIG_PATH, load_config

_INSTALL_HOME = Path(os.getenv("J_AGENT_HOME", Path.home() / ".local/share/j-the-agent"))
_MANIFEST_PATH = _INSTALL_HOME / "install-manifest.json"
SUPPORTED_MODES = ("quick", "companion")


def mode_is_installed(mode: str) -> bool:
    """True when the mode package is importable in this environment."""

    if mode not in SUPPORTED_MODES:
        return False
    return importlib.util.find_spec(f"modes.{mode}") is not None


def available_modes() -> list[str]:
    """Modes present in the current installation (usually exactly one)."""

    return [mode for mode in SUPPORTED_MODES if mode_is_installed(mode)]


def resolve_mode() -> str:
    """Pick the active mode.

    Priority: ``J_AGENT_MODE`` → install manifest → config.yaml → sole installed
    mode. Preferences that point at a *missing* mode are ignored so a Companion-
    only install never falls through to Quick.
    """

    installed = available_modes()
    if not installed:
        raise SystemExit(
            "No product mode is installed (neither modes.quick nor modes.companion). "
            "Re-run scripts/install.sh and choose Quick or Companion."
        )

    preferred = _preferred_mode()
    if preferred in installed:
        return preferred

    if len(installed) == 1:
        return installed[0]

    # Dev checkouts may have both modes; require an explicit choice.
    raise SystemExit(
        "Both Quick and Companion are present, but no mode was selected. "
        "Set J_AGENT_MODE=quick|companion, or use scripts/install.sh "
        "(official install ships only one mode)."
    )


def _preferred_mode() -> str | None:
    env_mode = (os.getenv("J_AGENT_MODE") or "").strip().lower()
    if env_mode in SUPPORTED_MODES:
        return env_mode

    for candidate in (_MANIFEST_PATH, Path(sys_prefix_manifest())):
        mode = _mode_from_manifest(candidate)
        if mode:
            return mode

    config_path = Path(os.getenv("J_AGENT_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()
    if config_path.exists():
        try:
            mode = load_config(config_path).mode.strip().lower()
        except Exception:  # noqa: BLE001 - fall back if config is partial
            mode = ""
        if mode in SUPPORTED_MODES:
            return mode
    return None


def sys_prefix_manifest() -> Path:
    import sys

    return Path(sys.prefix).parent / "install-manifest.json"


def _mode_from_manifest(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    mode = str(data.get("mode", "")).strip().lower()
    return mode if mode in SUPPORTED_MODES else None
