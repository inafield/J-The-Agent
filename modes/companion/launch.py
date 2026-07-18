"""Open a terminal / notify the user when Companion needs attention."""

from __future__ import annotations

import json
import platform
import shlex
import shutil
import subprocess
from pathlib import Path


def resolve_ja_command() -> str:
    """Prefer the installed ``ja`` on PATH, else the current interpreter entry."""

    found = shutil.which("ja")
    if found:
        return found
    import sys

    return str(Path(sys.executable).with_name("ja"))


def open_terminal_with_command(command: str) -> bool:
    """Open a new terminal window running ``command``. Returns True on success."""

    system = platform.system()
    if system == "Darwin":
        script = f'tell application "Terminal" to do script {json.dumps(command)}'
        completed = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0

    shell_command = f"{command}; exec bash"
    for binary, args in (
        ("gnome-terminal", ["--", "bash", "-lc", shell_command]),
        ("x-terminal-emulator", ["-e", "bash", "-lc", shell_command]),
        ("konsole", ["-e", "bash", "-lc", shell_command]),
        ("xfce4-terminal", ["--command", shlex.join(["bash", "-lc", shell_command])]),
    ):
        path = shutil.which(binary)
        if not path:
            continue
        completed = subprocess.run([path, *args], capture_output=True, text=True, check=False)
        if completed.returncode == 0:
            return True
    return False


def notify(title: str, message: str) -> bool:
    """Best-effort desktop notification."""

    system = platform.system()
    if system == "Darwin":
        script = (
            f"display notification {json.dumps(message)} "
            f"with title {json.dumps(title)}"
        )
        completed = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0
    if shutil.which("notify-send"):
        completed = subprocess.run(
            ["notify-send", title, message],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0
    return False


def activate_for_reminder(reminder_id: str, text: str) -> dict[str, bool]:
    """Notify and try to open Companion in a new terminal for this reminder."""

    ja = resolve_ja_command()
    command = shlex.join([ja, "deliver-reminder", reminder_id, "--interactive"])
    notified = notify("J Companion", text[:180])
    opened = open_terminal_with_command(command)
    return {"notified": notified, "opened_terminal": opened}
