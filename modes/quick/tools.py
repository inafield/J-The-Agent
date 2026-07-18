"""Filesystem, shell, and server-repair tools available to Quick only."""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.safety import SafetyError
from core.tools import Tool, ToolContext, ToolRegistry, ToolResult, load_plugins
from core.utils import human_size, truncate_output


def _schema(**properties: Any) -> dict[str, Any]:
    required = [name for name, spec in properties.items() if spec.pop("_required", False)]
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _error(code: str, output: str, suggestion: str | None = None) -> ToolResult:
    return ToolResult(output=output, ok=False, code=code, suggestion=suggestion)


def _read_file(context: ToolContext, args: dict[str, Any]) -> str:
    path = context.safety.check_read(args["path"])
    if not path.is_file():
        return _error("not_file", f"File not found or not a regular file: {path}")
    max_bytes = min(max(int(args.get("max_bytes", 100_000)), 1), 500_000)
    return truncate_output(path.read_text(encoding="utf-8", errors="replace")[:max_bytes])


def _list_directory(context: ToolContext, args: dict[str, Any]) -> str:
    path = context.safety.check_read(args.get("path", context.config.safety.working_directory))
    if not path.is_dir():
        return _error("not_directory", f"Directory not found: {path}")
    entries: list[str] = []
    for entry in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))[:200]:
        try:
            safe_entry = context.safety.check_read(entry)
        except SafetyError:
            continue
        suffix = "/" if safe_entry.is_dir() else f" ({human_size(safe_entry.stat().st_size)})"
        entries.append(f"{'[dir]' if safe_entry.is_dir() else '[file]'} {entry.name}{suffix}")
    return truncate_output("\n".join(entries) or "(empty directory)")


def _grep_search(context: ToolContext, args: dict[str, Any]) -> str:
    root = context.safety.check_read(args.get("path", context.config.safety.working_directory))
    regex = re.compile(args["pattern"])
    max_results = min(max(int(args.get("max_results", 50)), 1), 200)
    files = [root] if root.is_file() else root.rglob("*")
    matches: list[str] = []
    skipped = 0
    for file in files:
        if len(matches) >= max_results:
            break
        try:
            file = context.safety.check_read(file)
        except SafetyError:
            skipped += 1
            continue
        if not file.is_file() or file.stat().st_size > 2_000_000:
            continue
        try:
            lines = file.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if regex.search(line):
                matches.append(f"{file}:{number}: {line.strip()}")
                if len(matches) >= max_results:
                    break
    note = f"\n[{skipped} inaccessible paths skipped]" if skipped else ""
    return truncate_output(("\n".join(matches) or "No matches found.") + note)


def _write_file(context: ToolContext, args: dict[str, Any]) -> str:
    raw_path = Path(args["path"]).expanduser()
    path = (
        context.safety.check_write(raw_path)
        if raw_path.exists()
        else context.safety.check_create(raw_path)
    )
    content = str(args.get("content", ""))
    append = bool(args.get("append", False))
    action = "append to" if append else "overwrite"
    if not context.confirm(f"Allow J to {action} {path} ({len(content)} characters)?"):
        return _error("user_denied", "Cancelled by user.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as handle:
        handle.write(content)
    return f"Wrote {len(content)} characters to {path}"


def _patch_file(context: ToolContext, args: dict[str, Any]) -> str:
    path = context.safety.check_write(args["path"])
    old = str(args["old_text"])
    new = str(args["new_text"])
    content = path.read_text(encoding="utf-8")
    count = content.count(old)
    if count != 1:
        return _error(
            "ambiguous_patch",
            f"Patch refused: old_text must occur exactly once (found {count}).",
            "Read the file again and provide a unique exact old_text block.",
        )
    if not context.confirm(f"Allow J to modify {path}?"):
        return _error("user_denied", "Cancelled by user.")
    path.write_text(content.replace(old, new, 1), encoding="utf-8")
    return f"Patched {path}"


def _run_command(context: ToolContext, args: dict[str, Any]) -> str:
    command = str(args["command"]).strip()
    workdir = context.safety.check_read(
        args.get("workdir", context.config.safety.working_directory)
    )
    if not workdir.is_dir():
        return _error("missing_workdir", f"Working directory does not exist: {workdir}")
    verdict = context.safety.inspect_command(command, workdir)
    if verdict.blocked:
        return _error(
            "safety_block",
            f"Blocked by SafetyGuard: {'; '.join(verdict.reasons)}",
            "Use a permitted path or a safer typed tool.",
        )
    if verdict.needs_confirmation and not context.confirm(
        f"Allow J to run this potentially dangerous command?\n$ {command}\n"
        f"Reasons: {'; '.join(verdict.reasons)}"
    ):
        return _error("user_denied", "Cancelled by user.")
    timeout = min(max(int(args.get("timeout", 60)), 1), 300)
    try:
        result = subprocess.run(  # noqa: S602 - reviewed by SafetyGuard immediately above.
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _error(
            "timeout",
            f"Command timed out after {timeout} seconds.",
            "Retry with a larger timeout or a narrower command.",
        )
    return truncate_output(
        f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _get_system_info(context: ToolContext, args: dict[str, Any]) -> str:
    uname = platform.uname()
    return "\n".join(
        (
            f"system: {uname.system} {uname.release}",
            f"machine: {uname.machine}",
            f"hostname: {uname.node}",
            f"python: {platform.python_version()}",
            f"cpu_count: {os.cpu_count()}",
            f"working_directory: {context.config.safety.working_directory}",
        )
    )


def _current_time(context: ToolContext, args: dict[str, Any]) -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _run_fixed(
    context: ToolContext,
    argv: list[str],
    *,
    mutate: bool = False,
    timeout: int = 30,
) -> str:
    workdir = context.safety.check_read(context.config.safety.working_directory)
    verdict = context.safety.inspect_command(shlex.join(argv), workdir)
    if verdict.blocked:
        return _error("safety_block", f"Blocked by SafetyGuard: {'; '.join(verdict.reasons)}")
    if mutate and not context.confirm(f"Allow J to run: {shlex.join(argv)}?"):
        return _error("user_denied", "Cancelled by user.")
    try:
        result = subprocess.run(
            argv,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return _error(
            "command_missing",
            f"Command is unavailable: {argv[0]}",
            "Install the command or use another diagnostic tool.",
        )
    except subprocess.TimeoutExpired:
        return _error("timeout", f"Command timed out after {timeout} seconds.")
    return truncate_output(
        f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _unit(args: dict[str, Any]) -> str:
    unit = str(args["unit"])
    if not re.fullmatch(r"[A-Za-z0-9@_.-]+", unit):
        raise ValueError("Invalid service unit name")
    return unit


def _service_status(context: ToolContext, args: dict[str, Any]) -> str:
    return _run_fixed(context, ["systemctl", "status", _unit(args), "--no-pager"])


def _restart_service(context: ToolContext, args: dict[str, Any]) -> str:
    return _run_fixed(context, ["systemctl", "restart", _unit(args)], mutate=True, timeout=60)


def _failed_services(context: ToolContext, args: dict[str, Any]) -> str:
    return _run_fixed(context, ["systemctl", "--failed", "--no-pager"])


def _journal_tail(context: ToolContext, args: dict[str, Any]) -> str:
    lines = min(max(int(args.get("lines", 100)), 1), 500)
    return _run_fixed(
        context,
        ["journalctl", "-u", _unit(args), "-n", str(lines), "--no-pager"],
    )


def _disk_usage(context: ToolContext, args: dict[str, Any]) -> str:
    path = context.safety.check_read(args.get("path", context.config.safety.working_directory))
    usage = shutil.disk_usage(path)
    return (
        f"path: {path}\ntotal: {human_size(usage.total)}\n"
        f"used: {human_size(usage.used)}\nfree: {human_size(usage.free)}"
    )


def _listening_ports(context: ToolContext, args: dict[str, Any]) -> str:
    return _run_fixed(context, ["ss", "-tulnp"])


def register_quick_tools(registry: ToolRegistry) -> None:
    """Register tools unique to Quick mode."""

    tools = [
        Tool(
            "read_file",
            "Read a permitted text file.",
            _schema(
                path={"type": "string", "_required": True},
                max_bytes={"type": "integer"},
            ),
            _read_file,
        ),
        Tool(
            "list_directory",
            "List a permitted directory.",
            _schema(
                path={"type": "string"},
            ),
            _list_directory,
        ),
        Tool(
            "grep_search",
            "Regex-search permitted text files.",
            _schema(
                pattern={"type": "string", "_required": True},
                path={"type": "string"},
                max_results={"type": "integer"},
            ),
            _grep_search,
        ),
        Tool(
            "write_file",
            "Create, overwrite, or append a file after confirmation.",
            _schema(
                path={"type": "string", "_required": True},
                content={"type": "string", "_required": True},
                append={"type": "boolean"},
            ),
            _write_file,
        ),
        Tool(
            "patch_file",
            "Replace one exact text block in a file after confirmation.",
            _schema(
                path={"type": "string", "_required": True},
                old_text={"type": "string", "_required": True},
                new_text={"type": "string", "_required": True},
            ),
            _patch_file,
        ),
        Tool(
            "run_command",
            "Run a shell command after SafetyGuard review.",
            _schema(
                command={"type": "string", "_required": True},
                workdir={"type": "string"},
                timeout={"type": "integer"},
            ),
            _run_command,
        ),
        Tool(
            "get_system_info", "Return host and runtime information.", _schema(), _get_system_info
        ),
        Tool("current_time", "Return local date and time.", _schema(), _current_time),
        Tool(
            "service_status",
            "Show a systemd service status.",
            _schema(
                unit={"type": "string", "_required": True},
            ),
            _service_status,
        ),
        Tool(
            "restart_service",
            "Restart a systemd service after confirmation.",
            _schema(
                unit={"type": "string", "_required": True},
            ),
            _restart_service,
        ),
        Tool("failed_services", "List failed systemd services.", _schema(), _failed_services),
        Tool(
            "journal_tail",
            "Show recent logs for a systemd unit.",
            _schema(
                unit={"type": "string", "_required": True},
                lines={"type": "integer"},
            ),
            _journal_tail,
        ),
        Tool(
            "disk_usage",
            "Show disk usage for a permitted path.",
            _schema(
                path={"type": "string"},
            ),
            _disk_usage,
        ),
        Tool("listening_ports", "List listening TCP/UDP ports.", _schema(), _listening_ports),
    ]
    for tool in tools:
        registry.register(tool)


def build_registry(config: AppConfig) -> ToolRegistry:
    """Assemble the full tool set for Quick mode: base + quick + plugins."""

    registry = ToolRegistry()
    register_quick_tools(registry)
    load_plugins(registry, config.plugins_dir)
    return registry
