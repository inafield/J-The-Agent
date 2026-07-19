"""Companion-only filesystem safety setup (no Quick directory profiles)."""

from __future__ import annotations

from pathlib import Path

from core.config import AccessMode, SafetyProfile, SafetySettings
from core.safety import RECOMMENDED_FORBIDDEN_PATTERNS
from modes.common_cli import BACK, console, select

# System / OS-critical paths blocked by default on both Linux and macOS.
COMPANION_SYSTEM_FORBIDDEN_PATHS: tuple[str, ...] = (
    # Linux
    "/root",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/lost+found",
    "/run",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/passwd",
    "/var/lib/docker",
    # macOS
    "/System",
    "/private/var/db",
    "/Library/Keychains",
    "/usr/standalone",
)

COMPANION_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    *RECOMMENDED_FORBIDDEN_PATTERNS,
    "/Library/Keychains/**",
    "/private/etc/sudoers*",
)


def default_companion_safety(
    *,
    working_directory: Path | None = None,
    extra_forbidden: list[Path] | None = None,
    extra_patterns: list[str] | None = None,
) -> SafetySettings:
    """Full home/desktop access with OS system paths blocked."""

    paths = [Path(item) for item in COMPANION_SYSTEM_FORBIDDEN_PATHS]
    if extra_forbidden:
        paths.extend(extra_forbidden)
    patterns = list(COMPANION_FORBIDDEN_PATTERNS)
    if extra_patterns:
        patterns.extend(extra_patterns)
    # Deduplicate while preserving order
    seen_paths: set[str] = set()
    unique_paths: list[Path] = []
    for path in paths:
        key = str(path.expanduser().absolute())
        if key in seen_paths:
            continue
        seen_paths.add(key)
        unique_paths.append(path.expanduser().absolute())
    seen_patterns: set[str] = set()
    unique_patterns: list[str] = []
    for pattern in patterns:
        if pattern in seen_patterns:
            continue
        seen_patterns.add(pattern)
        unique_patterns.append(pattern)
    return SafetySettings(
        profile=SafetyProfile.COMPANION,
        access_mode=AccessMode.FULL,
        working_directory=(working_directory or Path.cwd()).expanduser().resolve(),
        allowed_paths=[],
        read_only_paths=[],
        forbidden_paths=unique_paths,
        forbidden_patterns=unique_patterns,
        confirm_dangerous_commands=True,
    )


def run_companion_permission_wizard(
    initial: SafetySettings | None = None,
    *,
    allow_back: bool = False,
) -> SafetySettings | None:
    """Explain system blocks, optionally collect extra forbidden paths."""

    import questionary

    console.print(
        "\n[bold cyan]Companion filesystem safety[/bold cyan]\n"
        "System folders and sensitive OS paths are [bold]forbidden by default[/bold] "
        "on Linux and macOS (e.g. /System, /proc, /dev, /boot, /root, keychains, "
        "shadow/sudoers).\n"
        "You can block more paths now, or later with:\n"
        "  [green]ja deny /path/to/file-or-folder[/green]\n"
    )

    add_more = select(
        "Add extra forbidden paths now?",
        [
            questionary.Choice("No — keep system defaults only", value=False),
            questionary.Choice("Yes — add paths one by one", value=True),
        ],
        back=allow_back,
    )
    if add_more in (None, BACK):
        return None if allow_back else _raise_keyboard_interrupt()

    extras: list[Path] = []
    patterns: list[str] = []
    # Keep any user-added paths from a previous Companion config (not system defaults).
    if initial is not None and initial.profile is SafetyProfile.COMPANION:
        system = {str(Path(p).expanduser().absolute()) for p in COMPANION_SYSTEM_FORBIDDEN_PATHS}
        system_patterns = set(COMPANION_FORBIDDEN_PATTERNS)
        for path in initial.forbidden_paths:
            key = str(path.expanduser().absolute())
            if key not in system:
                extras.append(path)
        for pattern in initial.forbidden_patterns:
            if pattern not in system_patterns:
                patterns.append(pattern)

    if add_more:
        collected = _collect_extra_forbidden()
        if collected is None:
            return None if allow_back else _raise_keyboard_interrupt()
        extras, patterns = collected

    return default_companion_safety(
        working_directory=Path.cwd(),
        extra_forbidden=extras,
        extra_patterns=patterns,
    )


def _collect_extra_forbidden() -> tuple[list[Path], list[str]] | None:
    """One path per prompt until the user types ``back``."""

    import questionary

    paths: list[Path] = []
    patterns: list[str] = []
    console.print(
        "[dim]Enter one path or glob per line. Type [bold]back[/bold] when finished.[/dim]"
    )
    while True:
        if paths or patterns:
            console.print("[dim]Extra forbidden paths so far:[/dim]")
            for item in [*(str(p) for p in paths), *patterns]:
                console.print(f"  [red]×[/red] {item}")
        value = questionary.text(
            "Forbidden path (or 'back' to finish):",
        ).ask()
        if value is None:
            return None
        text = value.strip()
        if not text or text.lower() == "back":
            return paths, patterns
        if any(char in text for char in "*?["):
            if text not in patterns:
                patterns.append(text)
        else:
            path = Path(text).expanduser()
            if path not in paths:
                paths.append(path)


def deny_path(config_safety: SafetySettings, raw_path: str) -> SafetySettings:
    """Add a file/directory (or glob) to the Companion forbid list."""

    text = raw_path.strip()
    if not text:
        raise ValueError("Path cannot be empty.")
    if any(char in text for char in "*?["):
        patterns = list(config_safety.forbidden_patterns)
        if text not in patterns:
            patterns.append(text)
        return config_safety.model_copy(update={"forbidden_patterns": patterns})
    path = Path(text).expanduser().absolute()
    paths = list(config_safety.forbidden_paths)
    if path not in paths:
        paths.append(path)
    return config_safety.model_copy(update={"forbidden_paths": paths})


def _raise_keyboard_interrupt() -> None:
    raise KeyboardInterrupt
