"""Central permission checks for all agent tools.

Every tool that touches the filesystem or runs a shell command must go through a
:class:`SafetyGuard`. The guard is intentionally mode-independent: Quick uses it
today, and Companion/Manager will reuse it unchanged.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from core.config import AccessMode, SafetyProfile, SafetySettings

CRITICAL_FORBIDDEN_PATHS: tuple[str, ...] = (
    "/root",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/lost+found",
    "/run",
)

RECOMMENDED_FORBIDDEN_PATHS: tuple[str, ...] = (
    *CRITICAL_FORBIDDEN_PATHS,
    "/etc/shadow",
    "/etc/passwd",
    "/etc/sudoers",
    "/var/cache",
    "/var/backups",
)

RECOMMENDED_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "/home/*/.ssh",
    "/home/*/.gnupg",
    "/home/*/.password-store",
    "/Users/*/.ssh",
    "/Users/*/.gnupg",
    "/Users/*/.password-store",
    "/root/**",
    "~/.ssh",
    "~/.gnupg",
    "~/.password-store",
    "~/.config",
    "~/.cache",
    "~/.local/share",
)

SAFE_READONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "awk",
        "cat",
        "cut",
        "df",
        "du",
        "echo",
        "find",
        "grep",
        "head",
        "hostname",
        "id",
        "journalctl",
        "ls",
        "lsof",
        "netstat",
        "pgrep",
        "ps",
        "printf",
        "pwd",
        "sed",
        "ss",
        "stat",
        "tail",
        "uname",
        "uptime",
        "wc",
        "which",
        "whoami",
    }
)

# Commands that are refused outright — no confirmation can enable them.
_CRITICAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf][a-z]*\s+/\s*($|\s)"), "recursive delete of /"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bmkfs\.[a-z0-9]+\b|\bmkfs\b"), "filesystem format"),
    (re.compile(r"\bdd\b.*\bof=/dev/(sd|nvme|mmcblk|disk)"), "raw write to a disk device"),
    (re.compile(r">\s*/dev/(sd|nvme|mmcblk|disk)"), "redirect to a disk device"),
    (re.compile(r"\b(chmod|chown)\b.*\s-R\b.*\s/\s*($|[;&|])"), "recursive root mutation"),
)

# Commands allowed only after an explicit confirmation.
_DANGEROUS_COMMANDS: dict[str, str] = {
    "rm": "deletes files",
    "rmdir": "removes directories",
    "mv": "moves or overwrites files",
    "cp": "copies or overwrites files",
    "touch": "creates or changes files",
    "mkdir": "creates directories",
    "tee": "writes files",
    "truncate": "changes file contents",
    "install": "copies files and changes permissions",
    "dd": "writes raw data",
    "chmod": "changes permissions",
    "chown": "changes ownership",
    "kill": "terminates processes",
    "pkill": "terminates processes",
    "killall": "terminates processes",
    "shutdown": "powers off the machine",
    "reboot": "restarts the machine",
    "systemctl": "controls system services",
    "service": "controls system services",
    "apt": "installs or removes packages",
    "apt-get": "installs or removes packages",
    "yum": "installs or removes packages",
    "dnf": "installs or removes packages",
    "pacman": "installs or removes packages",
    "pip": "installs or removes packages",
    "npm": "installs or removes packages",
    "curl": "makes network requests",
    "wget": "downloads from the network",
    "git": "may modify the repository",
    "docker": "controls containers",
    "sudo": "runs a command as another user",
}


class SafetyError(PermissionError):
    """Raised when a requested action violates the configured policy."""


@dataclass
class CommandVerdict:
    """Result of inspecting a shell command before running it."""

    command: str
    blocked: bool = False
    needs_confirmation: bool = False
    reasons: list[str] = field(default_factory=list)


class SafetyGuard:
    """Validate filesystem paths and shell commands against the policy."""

    def __init__(self, settings: SafetySettings) -> None:
        self.settings = settings
        self._forbidden = [self._normalize(path) for path in settings.forbidden_paths]
        self._forbidden_patterns = [
            self._normalize_pattern(pattern) for pattern in settings.forbidden_patterns
        ]
        self._read_only = [self._normalize(path) for path in settings.read_only_paths]

    @staticmethod
    def _normalize(path: str | Path) -> Path:
        # strict=False resolves existing symlinks and canonicalizes a future
        # target through its existing parent. This closes allowed-dir symlink
        # escapes without requiring the destination to exist.
        return Path(path).expanduser().resolve(strict=False)

    @staticmethod
    def _is_within(path: Path, base: Path) -> bool:
        return path == base or path.is_relative_to(base)

    @staticmethod
    def _normalize_pattern(pattern: str) -> str:
        expanded = str(Path(pattern).expanduser()) if pattern.startswith("~") else pattern
        return expanded.rstrip("/")

    def allowed_roots(self) -> list[Path]:
        """Directories the agent may read, based on the active access mode."""

        mode = self.settings.access_mode
        if mode is AccessMode.FULL:
            return [Path("/")]
        if mode is AccessMode.CURRENT_DIRECTORY:
            return [self._normalize(self.settings.working_directory)]
        roots = [self._normalize(p) for p in self.settings.allowed_paths]
        roots.extend(self._read_only)
        return roots or [Path.cwd()]

    def is_forbidden(self, path: Path) -> bool:
        normalized = self._normalize(path)
        if any(self._is_within(normalized, base) for base in self._forbidden):
            return True
        candidates = [str(normalized), *(str(parent) for parent in normalized.parents)]
        return any(
            fnmatch(candidate, pattern.removesuffix("/**")) or fnmatch(candidate, pattern)
            for candidate in candidates
            for pattern in self._forbidden_patterns
        )

    def is_read_only(self, path: Path) -> bool:
        return any(self._is_within(path, base) for base in self._read_only)

    def _within_allowed(self, path: Path) -> bool:
        return any(self._is_within(path, root) for root in self.allowed_roots())

    def check_read(self, path: str | Path) -> Path:
        """Return a resolved path if reading it is permitted, else raise."""

        resolved = self._normalize(path)
        if self.is_forbidden(resolved):
            raise SafetyError(f"Access to '{resolved}' is forbidden by policy.")
        if not self._within_allowed(resolved):
            raise SafetyError(
                f"'{resolved}' is outside the allowed directories. "
                f"Run 'agent switch directory' or 'agent permissions' to adjust access."
            )
        return resolved

    def check_write(self, path: str | Path) -> Path:
        """Return a resolved path if writing to it is permitted, else raise."""

        resolved = self.check_read(path)
        if self.is_read_only(resolved):
            raise SafetyError(f"'{resolved}' is read-only by policy.")
        return resolved

    def check_create(self, path: str | Path) -> Path:
        """Check a new path and its resolved parent before it is created."""

        candidate = self._normalize(path)
        self.check_write(candidate.parent)
        if self.is_forbidden(candidate):
            raise SafetyError(f"Creation in '{candidate}' is forbidden by policy.")
        return candidate

    def inspect_command(
        self,
        command: str,
        working_directory: str | Path | None = None,
    ) -> CommandVerdict:
        """Classify a shell command as safe, dangerous, or blocked."""

        verdict = CommandVerdict(command=command.strip())
        if not verdict.command:
            verdict.blocked = True
            verdict.reasons.append("empty command")
            return verdict

        for pattern, reason in _CRITICAL_PATTERNS:
            if pattern.search(command):
                verdict.blocked = True
                verdict.reasons.append(f"critical: {reason}")
                return verdict

        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            verdict.blocked = True
            verdict.reasons.append(f"cannot safely parse command: {exc}")
            return verdict

        base_commands = self._base_commands(command)
        writes_paths = bool(
            set(base_commands)
            & {"rm", "rmdir", "mv", "cp", "touch", "mkdir", "tee", "truncate", "install"}
        )
        write_next = False
        for token in tokens:
            if token in {">", ">>", "1>", "1>>", "2>", "2>>"}:
                write_next = True
                continue
            path_text = self._path_from_token(token)
            if path_text is None:
                continue
            if self._path_pattern_hits_forbidden(path_text, working_directory):
                verdict.blocked = True
                verdict.reasons.append(f"command references forbidden path: {path_text}")
                return verdict
            if not any(char in path_text for char in "*?["):
                command_path = Path(path_text).expanduser()
                if not command_path.is_absolute():
                    command_path = (
                        Path(working_directory or self.settings.working_directory) / command_path
                    )
                try:
                    is_write = writes_paths or write_next or token.startswith(">")
                    if is_write:
                        if command_path.exists():
                            self.check_write(command_path)
                        else:
                            self.check_create(command_path)
                    else:
                        self.check_read(
                            command_path if command_path.exists() else command_path.parent
                        )
                except SafetyError as exc:
                    verdict.blocked = True
                    verdict.reasons.append(str(exc))
                    return verdict
                finally:
                    write_next = False

        all_readonly = bool(base_commands) and all(
            name in SAFE_READONLY_COMMANDS
            or (
                name == "systemctl"
                and re.search(r"\bsystemctl\s+(status|show|is-active)\b", command)
            )
            or (name == "git" and re.search(r"\bgit\s+(status|diff|log|show|branch)\b", command))
            for name in base_commands
        )
        unsafe_readonly_syntax = bool(
            re.search(
                r"\$\(|`|"
                r"\bfind\b.*\s(-delete|-exec|-execdir|-ok)\b|"
                r"\bsed\b\s+[^;&|]*-[a-zA-Z]*i|"
                r"\bawk\b.*\bsystem\s*\(|"
                r"\bjournalctl\b.*(--vacuum|--rotate)",
                command,
            )
        )
        all_readonly = all_readonly and not unsafe_readonly_syntax
        for name in base_commands:
            note = _DANGEROUS_COMMANDS.get(name)
            if note and not all_readonly:
                verdict.needs_confirmation = True
                verdict.reasons.append(f"{name}: {note}")

        if re.search(r"(^|[^<])>{1,2}|<", command):
            verdict.needs_confirmation = True
            verdict.reasons.append("shell redirection may read or modify files")
        if not all_readonly:
            verdict.needs_confirmation = True
            verdict.reasons.append("command is not on the safe read-only allowlist")

        return verdict

    @staticmethod
    def _path_from_token(token: str) -> str | None:
        value = token
        if token.startswith("-") and "=" in token:
            value = token.split("=", 1)[1]
        value = value.lstrip("<>")
        if value.startswith(("/", "./", "../", "~")):
            return value
        return None

    def _path_pattern_hits_forbidden(
        self,
        value: str,
        working_directory: str | Path | None = None,
    ) -> bool:
        expanded = str(Path(value).expanduser())
        if any(char in expanded for char in "*?["):
            base_dir = Path(working_directory or self.settings.working_directory)
            absolute = expanded if expanded.startswith("/") else str(base_dir / expanded)
            if any(fnmatch(str(base), absolute) for base in self._forbidden):
                return True
            command_prefix = re.split(r"[*?\[]", absolute, maxsplit=1)[0].rstrip("/")
            for pattern in self._forbidden_patterns:
                forbidden_prefix = re.split(r"[*?\[]", pattern, maxsplit=1)[0].rstrip("/")
                if command_prefix.startswith(forbidden_prefix) or forbidden_prefix.startswith(
                    command_prefix
                ):
                    return True
            return False
        path = Path(expanded)
        if not path.is_absolute():
            path = Path(working_directory or self.settings.working_directory) / path
        return self.is_forbidden(self._normalize(path))

    @staticmethod
    def _base_commands(command: str) -> list[str]:
        """Extract program names, following pipes and ``&&``/``;`` separators."""

        try:
            tokens = shlex.split(command)
        except ValueError:
            return []
        names: list[str] = []
        expect_command = True
        for token in tokens:
            if token in {"|", "||", "&&", ";", "&"}:
                expect_command = True
                continue
            if expect_command:
                names.append(Path(token).name)
                expect_command = False
        return names


# --------------------------------------------------------------------------- #
# Interactive first-run permission wizard
# --------------------------------------------------------------------------- #


def run_permission_wizard(
    initial: SafetySettings | None = None,
    *,
    allow_back: bool = False,
) -> SafetySettings | None:
    """Interactively build :class:`SafetySettings` on first setup.

    Uses arrow-key selection via ``questionary``. Kept out of module import time
    so ``core`` stays lightweight and unit-testable.
    """

    import questionary
    from rich.console import Console
    from rich.table import Table

    current = initial or SafetySettings()
    console = Console()
    console.print(
        "\n[bold cyan]J Security Profile[/bold cyan]\n"
        "Forbidden paths always take priority over allowed paths.\n"
    )
    profile_table = Table(show_header=True, header_style="bold cyan", box=None)
    profile_table.add_column("Profile", style="bold")
    profile_table.add_column("Access")
    profile_table.add_row(
        "Recommended",
        "Current directory, /var/log, /var/www, /opt; /etc and /tmp read-only. "
        "Private keys and critical system data are blocked.",
    )
    profile_table.add_row(
        "Advanced",
        "Almost the whole server. Sensitive personal data and critical system "
        "directories remain blocked; /etc is read-only.",
    )
    profile_table.add_row(
        "Current directory",
        "Only the directory from which J was started.",
    )
    profile_table.add_row(
        "Essentials + custom",
        "Critical system paths plus each path you add are forbidden; everything else is allowed.",
    )
    profile_table.add_row(
        "Custom only",
        "Only paths you enter are blocked. This can expose credentials and system data.",
    )
    console.print(profile_table)
    back = "__back__"
    choices = [
        questionary.Choice(
            "Recommended — server work without private data or critical system access",
            value=SafetyProfile.RECOMMENDED,
        ),
        questionary.Choice(
            "Advanced — almost the whole server, excluding sensitive and critical paths",
            value=SafetyProfile.ADVANCED,
        ),
        questionary.Choice(
            "Current directory only — safest for a single project",
            value=SafetyProfile.CURRENT_DIRECTORY,
        ),
        questionary.Choice(
            "System essentials + my forbidden paths — everything else is allowed",
            value=SafetyProfile.DEFAULTS_PLUS_CUSTOM,
        ),
        questionary.Choice(
            "Only my forbidden paths — dangerous; use only if you understand the risks",
            value=SafetyProfile.CUSTOM_ONLY,
        ),
    ]
    if allow_back:
        choices.append(questionary.Choice("← Back", value=back))
    profile = questionary.select(
        "Choose a security profile (↑/↓, Enter):",
        choices=choices,
        default=current.profile,
    ).ask()
    if profile in (None, back):
        return None if allow_back else _raise_keyboard_interrupt()

    working_directory = Path.cwd().resolve()
    common_patterns = list(RECOMMENDED_FORBIDDEN_PATTERNS)
    common_paths = [Path(item) for item in RECOMMENDED_FORBIDDEN_PATHS]

    if profile is SafetyProfile.RECOMMENDED:
        return SafetySettings(
            profile=profile,
            access_mode=AccessMode.SELECTED,
            working_directory=working_directory,
            allowed_paths=[
                working_directory,
                Path("/etc"),
                Path("/var/log"),
                Path("/var/www"),
                Path("/opt"),
                Path("/tmp"),
            ],
            read_only_paths=[Path("/etc"), Path("/tmp")],
            forbidden_paths=common_paths,
            forbidden_patterns=common_patterns,
        )
    if profile is SafetyProfile.ADVANCED:
        return SafetySettings(
            profile=profile,
            access_mode=AccessMode.FULL,
            working_directory=working_directory,
            allowed_paths=[],
            read_only_paths=[Path("/etc")],
            forbidden_paths=common_paths,
            forbidden_patterns=common_patterns,
        )
    if profile is SafetyProfile.CURRENT_DIRECTORY:
        return SafetySettings(
            profile=profile,
            access_mode=AccessMode.CURRENT_DIRECTORY,
            working_directory=working_directory,
            allowed_paths=[working_directory],
            read_only_paths=[],
            forbidden_paths=[Path(item) for item in CRITICAL_FORBIDDEN_PATHS],
            forbidden_patterns=[],
        )

    custom = _collect_forbidden_paths(console)
    if custom is None:
        return run_permission_wizard(initial, allow_back=allow_back)
    custom_paths, custom_patterns = custom
    if profile is SafetyProfile.CUSTOM_ONLY:
        warning = questionary.select(
            "This profile can expose credentials and system data. Continue?",
            choices=[
                questionary.Choice("No — return to profiles", value=False),
                questionary.Choice("Yes, I understand the risk", value=True),
            ],
            default=False,
        ).ask()
        if not warning:
            return run_permission_wizard(initial, allow_back=allow_back)
        base_paths: list[Path] = []
    else:
        base_paths = [Path(item) for item in CRITICAL_FORBIDDEN_PATHS]
    return SafetySettings(
        profile=profile,
        access_mode=AccessMode.FULL,
        working_directory=working_directory,
        allowed_paths=[],
        read_only_paths=[Path("/etc")],
        forbidden_paths=[*base_paths, *custom_paths],
        forbidden_patterns=custom_patterns,
    )


def _collect_forbidden_paths(console) -> tuple[list[Path], list[str]] | None:
    """Collect one path at a time until Done/Back is selected."""

    import questionary

    paths: list[Path] = []
    patterns: list[str] = []
    while True:
        rendered = [*(str(path) for path in paths), *patterns]
        if rendered:
            console.print("[dim]Current forbidden paths:[/dim]")
            for item in rendered:
                console.print(f"  [red]×[/red] {item}")
        action = questionary.select(
            "Forbidden paths:",
            choices=[
                questionary.Choice("Add a path or glob pattern", value="add"),
                questionary.Choice("Done", value="done"),
                questionary.Choice("← Back", value="back"),
            ],
            default="add" if not rendered else "done",
        ).ask()
        if action in (None, "back"):
            return None
        if action == "done":
            return paths, patterns
        value = questionary.text("Path or pattern (examples: /srv/private, /home/*/.ssh):").ask()
        if not value:
            continue
        value = value.strip()
        if any(char in value for char in "*?["):
            patterns.append(value)
        else:
            paths.append(Path(value).expanduser())


def _raise_keyboard_interrupt() -> None:
    raise KeyboardInterrupt
