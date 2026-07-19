"""Companion tool handlers: memory, progressive guides, and desktop helpers."""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.safety import SafetyError
from core.tools import Tool, ToolContext, ToolRegistry, ToolResult, load_plugins
from core.utils import human_size, truncate_output
from modes.companion.memory_store import MEMORY_FILES, MemoryStore
from modes.companion.reminders import ReminderStore, ScheduleError
from modes.companion.settings import CompanionSettings
from modes.companion.tool_guides import ToolGuideCatalog
from modes.companion.web import fetch_url, search_web

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_GUIDES_DIR = PACKAGE_DIR / "tools"
DEFAULT_TEMPLATES_DIR = PACKAGE_DIR / "memory"

CORE_TOOL_NAMES = {
    "load_tool_guide",
    "memory_read",
    "memory_write",
    "memory_append",
    "memory_list",
}

# Progressive unlock: schemas live in the registry; guides are tips only.
CATEGORY_TOOLS: dict[str, set[str]] = {
    "memory": {
        "memory_read",
        "memory_write",
        "memory_append",
        "memory_list",
    },
    "files": {
        "read_file",
        "list_directory",
        "grep_search",
        "write_file",
        "patch_file",
        "create_directory",
        "copy_path",
        "move_path",
        "trash_path",
        "path_info",
        "find_files",
    },
    "system": {
        "run_command",
        "get_system_info",
        "disk_usage",
        "list_processes",
        "current_time",
    },
    "web": {"web_search", "fetch_url", "open_url"},
    "apps": {"open_app", "open_path", "list_apps"},
    "code": {
        "read_file",
        "grep_search",
        "run_command",
        "patch_file",
        "create_directory",
        "find_files",
        "path_info",
        "git_status",
        "git_diff",
        "git_log",
    },
    "reminders": {
        "reminder_add",
        "reminder_list",
        "reminder_done",
        "reminder_cancel",
    },
    "media": {
        "clipboard_read",
        "clipboard_write",
        "screenshot",
        "list_media",
    },
}


def category_tool_names(category: str) -> set[str]:
    key = category.strip().lower().removesuffix(".md")
    if key not in CATEGORY_TOOLS:
        available = ", ".join(sorted(CATEGORY_TOOLS))
        raise FileNotFoundError(f"Unknown tool category '{category}'. Available: {available}")
    return set(CATEGORY_TOOLS[key])


def all_category_tool_names() -> set[str]:
    names: set[str] = set()
    for group in CATEGORY_TOOLS.values():
        names |= group
    return names


def _schema(**properties: Any) -> dict[str, Any]:
    required = [name for name, spec in properties.items() if spec.pop("_required", False)]
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _error(code: str, output: str, suggestion: str | None = None) -> ToolResult:
    return ToolResult(output=output, ok=False, code=code, suggestion=suggestion)


def build_registry(
    config: AppConfig,
    *,
    memory: MemoryStore,
    guides: ToolGuideCatalog,
    companion: CompanionSettings | None = None,
    reminders: ReminderStore | None = None,
) -> ToolRegistry:
    """Register Companion tools and load user plugins."""

    companion = companion or CompanionSettings()
    reminders = reminders or ReminderStore()
    registry = ToolRegistry()

    def load_tool_guide(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        category = str(args.get("category", "")).strip()
        if not category:
            return _error(
                "missing_category",
                f"Choose a category: {', '.join(sorted(CATEGORY_TOOLS))}",
            )
        try:
            tools = sorted(category_tool_names(category))
            guide = guides.get(category)
        except FileNotFoundError as exc:
            return _error("unknown_category", str(exc))
        tips = guide.read().strip()
        output = f"{tips}\n\nUnlocked tools (use native function calling):\n" + "\n".join(
            f"- `{name}`" for name in tools
        )
        return ToolResult(
            output=output,
            data={"category": guide.name, "tools": tools},
        )

    def memory_read(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        name = str(args.get("name", "memory"))
        try:
            text = memory.read(name)
        except ValueError as exc:
            return _error("invalid_memory", str(exc))
        return ToolResult(text or "(empty)", data={"name": name})

    def memory_write(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        name = str(args["name"])
        content = str(args.get("content", ""))
        try:
            path = memory.write(name, content)
        except ValueError as exc:
            return _error("invalid_memory", str(exc))
        return ToolResult(f"Wrote {path}", data={"path": str(path)})

    def memory_append(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        name = str(args.get("name", "memory"))
        content = str(args.get("content", "")).strip()
        if not content:
            return _error("empty", "Nothing to append.")
        try:
            path = memory.append(name, content)
        except ValueError as exc:
            return _error("invalid_memory", str(exc))
        return ToolResult(f"Appended to {path}", data={"path": str(path)})

    def memory_list(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        rows = []
        for name in MEMORY_FILES:
            path = memory.root / name
            if path.exists():
                rows.append(f"{name}: {human_size(path.stat().st_size)} · {path}")
            else:
                rows.append(f"{name}: missing")
        return ToolResult("\n".join(rows), data={"root": str(memory.root)})

    registry.register(
        Tool(
            "load_tool_guide",
            "Load a Companion tool category guide (files, web, reminders, ...).",
            _schema(
                category={
                    "type": "string",
                    "description": "Category name from tools/index.md",
                    "_required": True,
                }
            ),
            load_tool_guide,
        )
    )
    registry.register(
        Tool(
            "memory_read",
            "Read user.md, soul.md, or memory.md.",
            _schema(
                name={
                    "type": "string",
                    "enum": ["user", "soul", "memory"],
                    "_required": True,
                }
            ),
            memory_read,
        )
    )
    registry.register(
        Tool(
            "memory_write",
            "Replace an entire memory file. Prefer memory_append for new facts.",
            _schema(
                name={
                    "type": "string",
                    "enum": ["user", "soul", "memory"],
                    "_required": True,
                },
                content={"type": "string", "_required": True},
            ),
            memory_write,
        )
    )
    registry.register(
        Tool(
            "memory_append",
            "Append a short durable note to a memory file (default: memory).",
            _schema(
                content={"type": "string", "_required": True},
                name={
                    "type": "string",
                    "enum": ["user", "soul", "memory"],
                    "default": "memory",
                },
            ),
            memory_append,
        )
    )
    registry.register(
        Tool(
            "memory_list",
            "List Companion memory files and sizes.",
            _schema(),
            memory_list,
        )
    )

    # Category tools unlock after load_tool_guide; all are registered here.
    _register_file_tools(registry)
    _register_system_tools(registry)
    _register_web_tools(registry, companion)
    _register_app_tools(registry)
    _register_reminder_tools(registry, reminders)
    _register_media_tools(registry)

    load_plugins(registry, config.plugins_dir)
    return registry


def _register_file_tools(registry: ToolRegistry) -> None:
    def read_file(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = context.safety.check_read(args["path"])
        if not path.is_file():
            return _error("not_file", f"Not a file: {path}")
        max_bytes = min(max(int(args.get("max_bytes", 100_000)), 1), 500_000)
        return ToolResult(
            truncate_output(path.read_text(encoding="utf-8", errors="replace")[:max_bytes])
        )

    def list_directory(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = context.safety.check_read(
            args.get("path", context.config.safety.working_directory)
        )
        if not path.is_dir():
            return _error("not_directory", f"Not a directory: {path}")
        entries: list[str] = []
        for entry in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))[
            :200
        ]:
            try:
                safe = context.safety.check_read(entry)
            except SafetyError:
                continue
            suffix = "/" if safe.is_dir() else f" ({human_size(safe.stat().st_size)})"
            entries.append(f"{'[dir]' if safe.is_dir() else '[file]'} {entry.name}{suffix}")
        return ToolResult(truncate_output("\n".join(entries) or "(empty)"))

    def grep_search(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        root = context.safety.check_read(
            args.get("path", context.config.safety.working_directory)
        )
        regex = re.compile(args["pattern"])
        max_results = min(max(int(args.get("max_results", 50)), 1), 200)
        files = [root] if root.is_file() else root.rglob("*")
        matches: list[str] = []
        for file in files:
            if len(matches) >= max_results:
                break
            try:
                file = context.safety.check_read(file)
            except SafetyError:
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
        return ToolResult(truncate_output("\n".join(matches) or "No matches."))

    def write_file(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        raw_path = Path(args["path"]).expanduser()
        path = (
            context.safety.check_write(raw_path)
            if raw_path.exists()
            else context.safety.check_create(raw_path)
        )
        content = str(args.get("content", ""))
        append = bool(args.get("append", False))
        action = "Append to" if append else "Write"
        if not context.confirm(f"{action} file {path}?"):
            return _error("cancelled", "User declined write_file.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w", encoding="utf-8") as handle:
            handle.write(content)
        return ToolResult(f"Wrote {len(content)} chars to {path}")

    def patch_file(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = context.safety.check_write(args["path"])
        old = str(args["old"])
        new = str(args["new"])
        text = path.read_text(encoding="utf-8")
        if old not in text:
            return _error("not_found", "old text not found in file")
        if text.count(old) > 1 and not args.get("replace_all"):
            return _error("ambiguous", "old text matches multiple times; set replace_all=true")
        if not context.confirm(f"Patch file {path}?"):
            return _error("cancelled", "User declined patch_file.")
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        return ToolResult(f"Patched {path}")

    def _resolve_destination(context: ToolContext, raw: str | Path) -> Path:
        path = Path(raw).expanduser()
        if path.exists():
            return context.safety.check_write(path)
        return context.safety.check_create(path)

    def create_directory(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        raw = Path(args["path"]).expanduser()
        parents = bool(args.get("parents", True))
        if raw.exists():
            path = context.safety.check_write(raw)
            if path.is_dir():
                return ToolResult(f"Directory already exists: {path}")
            return _error("not_directory", f"Path exists and is not a directory: {path}")
        path = context.safety.check_create(raw)
        if not context.confirm(f"Create directory {path}?"):
            return _error("cancelled", "User declined create_directory.")
        path.mkdir(parents=parents, exist_ok=True)
        return ToolResult(f"Created directory {path}")

    def copy_path(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        source = context.safety.check_read(args["source"])
        destination = _resolve_destination(context, args["destination"])
        if not source.exists():
            return _error("missing", f"Source not found: {source}")
        if not context.confirm(f"Copy {source} → {destination}?"):
            return _error("cancelled", "User declined copy_path.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=bool(args.get("overwrite", False)))
        else:
            if destination.exists() and destination.is_dir():
                destination = context.safety.check_write(destination / source.name)
            shutil.copy2(source, destination)
        return ToolResult(f"Copied to {destination}")

    def move_path(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        source = context.safety.check_write(args["source"])
        destination = _resolve_destination(context, args["destination"])
        if not source.exists():
            return _error("missing", f"Source not found: {source}")
        if not context.confirm(f"Move {source} → {destination}?"):
            return _error("cancelled", "User declined move_path.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return ToolResult(f"Moved to {destination}")

    def trash_path(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = context.safety.check_write(args["path"])
        if not path.exists():
            return _error("missing", f"Path not found: {path}")
        if not context.confirm(f"Trash {path}?"):
            return _error("cancelled", "User declined trash_path.")
        # Best-effort trash; fall back to unlink.
        if platform.system() == "Darwin":
            script = f'tell application "Finder" to delete POSIX file "{path}"'
            completed = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                return ToolResult(f"Moved to Trash: {path}")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return ToolResult(f"Deleted {path}")

    def path_info(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        from datetime import datetime

        path = context.safety.check_read(args["path"])
        if not path.exists():
            return _error("missing", f"Path not found: {path}")
        stat = path.stat()
        kind = "directory" if path.is_dir() else "symlink" if path.is_symlink() else "file"
        mtime = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
        return ToolResult(
            "\n".join(
                (
                    f"path: {path}",
                    f"type: {kind}",
                    f"size: {human_size(stat.st_size)}",
                    f"modified: {mtime}",
                    f"mode: {oct(stat.st_mode)[-3:]}",
                )
            )
        )

    def find_files(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        root = context.safety.check_read(
            args.get("path", context.config.safety.working_directory)
        )
        pattern = str(args.get("pattern", "*")).strip() or "*"
        max_results = min(max(int(args.get("max_results", 100)), 1), 500)
        if not root.is_dir():
            return _error("not_directory", f"Not a directory: {root}")
        hits: list[str] = []
        for match in sorted(root.rglob(pattern)):
            if len(hits) >= max_results:
                break
            try:
                safe = context.safety.check_read(match)
            except SafetyError:
                continue
            suffix = "/" if safe.is_dir() else ""
            hits.append(f"{safe}{suffix}")
        return ToolResult(truncate_output("\n".join(hits) or "No matches."))

    for name, description, parameters, handler in (
        (
            "read_file",
            "Read a local text file.",
            _schema(
                path={"type": "string", "_required": True},
                max_bytes={"type": "integer", "default": 100000},
            ),
            read_file,
        ),
        (
            "list_directory",
            "List a directory.",
            _schema(path={"type": "string"}),
            list_directory,
        ),
        (
            "grep_search",
            "Regex search under a path.",
            _schema(
                pattern={"type": "string", "_required": True},
                path={"type": "string"},
                max_results={"type": "integer", "default": 50},
            ),
            grep_search,
        ),
        (
            "write_file",
            "Write or append a text file (confirmation required).",
            _schema(
                path={"type": "string", "_required": True},
                content={"type": "string", "_required": True},
                append={"type": "boolean", "default": False},
            ),
            write_file,
        ),
        (
            "patch_file",
            "Search/replace inside a file (confirmation required).",
            _schema(
                path={"type": "string", "_required": True},
                old={"type": "string", "_required": True},
                new={"type": "string", "_required": True},
                replace_all={"type": "boolean", "default": False},
            ),
            patch_file,
        ),
        (
            "create_directory",
            "Create a directory and parents (confirmation required).",
            _schema(
                path={"type": "string", "_required": True},
                parents={"type": "boolean", "default": True},
            ),
            create_directory,
        ),
        (
            "copy_path",
            "Copy a file or directory (confirmation required).",
            _schema(
                source={"type": "string", "_required": True},
                destination={"type": "string", "_required": True},
                overwrite={"type": "boolean", "default": False},
            ),
            copy_path,
        ),
        (
            "move_path",
            "Move or rename a path (confirmation required).",
            _schema(
                source={"type": "string", "_required": True},
                destination={"type": "string", "_required": True},
            ),
            move_path,
        ),
        (
            "trash_path",
            "Trash or delete a path (confirmation required).",
            _schema(path={"type": "string", "_required": True}),
            trash_path,
        ),
        (
            "path_info",
            "Show type, size, and modification time for a path.",
            _schema(path={"type": "string", "_required": True}),
            path_info,
        ),
        (
            "find_files",
            "Find files/directories by glob pattern under a path.",
            _schema(
                pattern={"type": "string", "_required": True},
                path={"type": "string"},
                max_results={"type": "integer", "default": 100},
            ),
            find_files,
        ),
    ):
        registry.register(Tool(name, description, parameters, handler))


def _register_system_tools(registry: ToolRegistry) -> None:
    def run_command(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        import shlex

        command = str(args["command"]).strip()
        cwd = context.safety.check_read(
            args.get("cwd", context.config.safety.working_directory)
        )
        if not cwd.is_dir():
            return _error("missing_workdir", f"Working directory does not exist: {cwd}")
        use_shell = bool(args.get("shell", False))
        verdict = context.safety.inspect_command(command, cwd)
        if verdict.blocked:
            return _error(
                "blocked",
                f"Blocked by SafetyGuard: {'; '.join(verdict.reasons)}",
            )
        if use_shell and not context.confirm(
            "Run with shell=True (pipes/redirects)? Prefer argv form when possible.\n"
            f"$ {command}"
        ):
            return _error("cancelled", "User declined shell mode.")
        if verdict.needs_confirmation and not context.confirm(
            f"Run potentially dangerous command?\n$ {command}\n"
            f"Reasons: {'; '.join(verdict.reasons)}"
        ):
            return _error("cancelled", "User declined run_command.")
        timeout = min(max(float(args.get("timeout", 60)), 1), 300)
        try:
            if use_shell:
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            else:
                argv = shlex.split(command)
                if not argv:
                    return _error("empty", "Empty command.")
                completed = subprocess.run(
                    argv,
                    shell=False,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
        except ValueError as exc:
            return _error(
                "bad_command",
                f"Cannot parse command as argv: {exc}. "
                "Set shell=true only if you need pipes/redirects.",
            )
        output = (completed.stdout or "") + (completed.stderr or "")
        return ToolResult(
            truncate_output(output or f"(exit {completed.returncode})"),
            ok=completed.returncode == 0,
            code="ok" if completed.returncode == 0 else "exit_nonzero",
            data={"returncode": completed.returncode, "shell": use_shell},
        )

    def get_system_info(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        info = {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "cwd": str(context.config.safety.working_directory),
        }
        return ToolResult(json.dumps(info, ensure_ascii=False, indent=2), data=info)

    def disk_usage(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = context.safety.check_read(
            args.get("path", context.config.safety.working_directory)
        )
        usage = shutil.disk_usage(path)
        text = (
            f"path={path}\n"
            f"total={human_size(usage.total)} free={human_size(usage.free)} "
            f"used={human_size(usage.used)}"
        )
        return ToolResult(text)

    def list_processes(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        completed = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            check=False,
        )
        lines = completed.stdout.splitlines()
        return ToolResult(truncate_output("\n".join(lines[:40]), 6_000, 40))

    def current_time(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        now = datetime.now().astimezone()
        return ToolResult(now.isoformat(timespec="seconds"))

    def git_status(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        root = context.safety.check_read(
            args.get("path", context.config.safety.working_directory)
        )
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "-sb"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return _error("git_error", completed.stderr.strip() or "git status failed")
        return ToolResult(truncate_output(completed.stdout or "(clean)"))

    def git_diff(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        root = context.safety.check_read(
            args.get("path", context.config.safety.working_directory)
        )
        cmd = ["git", "-C", str(root), "diff"]
        if args.get("staged"):
            cmd.append("--staged")
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            return _error("git_error", completed.stderr.strip() or "git diff failed")
        return ToolResult(truncate_output(completed.stdout or "(no diff)", 8_000, 200))

    def git_log(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        root = context.safety.check_read(
            args.get("path", context.config.safety.working_directory)
        )
        limit = min(max(int(args.get("limit", 20)), 1), 100)
        completed = subprocess.run(
            ["git", "-C", str(root), "log", f"-{limit}", "--oneline", "--decorate"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return _error("git_error", completed.stderr.strip() or "git log failed")
        return ToolResult(truncate_output(completed.stdout or "(no commits)"))

    for name, description, parameters, handler in (
        (
            "run_command",
            "Run a command as argv (default). Set shell=true only for pipes/redirects.",
            _schema(
                command={"type": "string", "_required": True},
                cwd={"type": "string"},
                timeout={"type": "number", "default": 60},
                shell={
                    "type": "boolean",
                    "default": False,
                    "description": "Use /bin/sh. Requires confirmation.",
                },
            ),
            run_command,
        ),
        ("get_system_info", "Summarize local system information.", _schema(), get_system_info),
        (
            "disk_usage",
            "Show disk usage for a path.",
            _schema(path={"type": "string"}),
            disk_usage,
        ),
        ("list_processes", "List top processes (best-effort).", _schema(), list_processes),
        ("current_time", "Current local datetime.", _schema(), current_time),
        (
            "git_status",
            "Short git status for a repository.",
            _schema(path={"type": "string"}),
            git_status,
        ),
        (
            "git_diff",
            "Bounded git diff.",
            _schema(
                path={"type": "string"},
                staged={"type": "boolean", "default": False},
            ),
            git_diff,
        ),
        (
            "git_log",
            "Recent git commits (oneline).",
            _schema(
                path={"type": "string"},
                limit={"type": "integer", "default": 20},
            ),
            git_log,
        ),
    ):
        registry.register(Tool(name, description, parameters, handler))


def _register_web_tools(registry: ToolRegistry, companion: CompanionSettings) -> None:
    def web_search(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        payload = search_web(
            companion,
            str(args["query"]),
            max_results=int(args.get("max_results", 5)),
        )
        if not payload.get("ok"):
            return _error("web_search_failed", str(payload.get("error") or "search failed"))
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2), data=payload)

    def fetch_url_tool(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        from modes.companion.url_safety import check_fetch_url

        url = str(args["url"]).strip()
        check = check_fetch_url(url, allowed_local_ports=companion.allowed_local_ports)
        if not check.ok:
            message = check.error or "URL blocked"
            if check.suggestion:
                message = f"{message} {check.suggestion}"
            return _error("fetch_blocked", message)
        if not context.confirm(f"Allow J to fetch this URL?\n{url}"):
            return _error("denied", "User denied fetching that URL.")
        payload = fetch_url(url, allowed_local_ports=companion.allowed_local_ports)
        if not payload.get("ok"):
            return _error("fetch_failed", str(payload.get("error") or "fetch failed"))
        return ToolResult(truncate_output(payload.get("text") or ""), data=payload)

    def open_url(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        from modes.companion.url_safety import check_fetch_url

        url = str(args["url"]).strip()
        check = check_fetch_url(url, allowed_local_ports=companion.allowed_local_ports)
        if not check.ok:
            message = check.error or "URL blocked"
            if check.suggestion:
                message = f"{message} {check.suggestion}"
            return _error("bad_url", message)
        if not context.confirm(f"Allow J to open this URL in the browser?\n{url}"):
            return _error("denied", "User denied opening that URL.")
        opened = webbrowser.open(url)
        return ToolResult(f"Opened {url}" if opened else f"Tried to open {url}")

    registry.register(
        Tool(
            "web_search",
            "Search the web for current information.",
            _schema(
                query={"type": "string", "_required": True},
                max_results={"type": "integer", "default": 5},
            ),
            web_search,
        )
    )
    registry.register(
        Tool(
            "fetch_url",
            "Fetch a URL and return readable text.",
            _schema(url={"type": "string", "_required": True}),
            fetch_url_tool,
        )
    )
    registry.register(
        Tool(
            "open_url",
            "Open a URL in the default browser.",
            _schema(url={"type": "string", "_required": True}),
            open_url,
        )
    )


def _register_app_tools(registry: ToolRegistry) -> None:
    def open_app(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        name = str(args["name"]).strip()
        system = platform.system()
        if system == "Darwin":
            completed = subprocess.run(
                ["open", "-a", name], capture_output=True, text=True, check=False
            )
        else:
            # Prefer desktop launchers only — never exec an arbitrary PATH binary.
            completed = subprocess.run(
                ["gtk-launch", name], capture_output=True, text=True, check=False
            )
            if completed.returncode != 0:
                completed = subprocess.run(
                    ["xdg-open", name], capture_output=True, text=True, check=False
                )
        if completed.returncode != 0:
            return _error("open_app_failed", completed.stderr.strip() or f"Could not open {name}")
        return ToolResult(f"Opened app {name}")

    def open_path(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        path = context.safety.check_read(args["path"])
        system = platform.system()
        cmd = ["open", str(path)] if system == "Darwin" else ["xdg-open", str(path)]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            return _error("open_path_failed", completed.stderr.strip() or f"Could not open {path}")
        return ToolResult(f"Opened {path}")

    def list_apps(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        system = platform.system()
        names: list[str] = []
        if system == "Darwin":
            root = Path("/Applications")
            if root.is_dir():
                names = sorted(path.stem for path in root.glob("*.app"))[:80]
        else:
            share = Path("/usr/share/applications")
            if share.is_dir():
                names = sorted(path.stem for path in share.glob("*.desktop"))[:80]
        return ToolResult("\n".join(names) or "(no apps discovered)")

    registry.register(
        Tool(
            "open_app",
            "Open a desktop application by name.",
            _schema(name={"type": "string", "_required": True}),
            open_app,
        )
    )
    registry.register(
        Tool(
            "open_path",
            "Open or reveal a local path with the default handler.",
            _schema(path={"type": "string", "_required": True}),
            open_path,
        )
    )
    registry.register(
        Tool("list_apps", "List available desktop apps (best-effort).", _schema(), list_apps)
    )


def _register_reminder_tools(registry: ToolRegistry, reminders: ReminderStore) -> None:
    def reminder_add(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        try:
            item = reminders.add(str(args["text"]), str(args["when"]))
        except ValueError as exc:
            return _error("bad_when", str(exc))
        except ScheduleError as exc:
            return ToolResult(
                f"Reminder {exc.reminder_id or '?'} was saved, but the OS schedule "
                f"failed: {exc}. It will only fire when overdue is checked manually "
                f"(ja check-reminders / ja reminders).",
                data={"id": exc.reminder_id, "schedule_ok": False, "error": str(exc)},
            )
        return ToolResult(
            f"Reminder {item.id} at {item.due_at}: {item.text}",
            data={
                "id": item.id,
                "due_at": item.due_at,
                "recurring": item.recurring,
                "schedule_ok": bool(item.schedule_ref),
            },
        )

    def reminder_list(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        items = reminders.list(include_done=bool(args.get("include_done")))
        if not items:
            return ToolResult("(no reminders)")
        lines = [
            f"{item.id} [{item.status}] {item.due_at} · {item.text}"
            + (f" ({item.recurring})" if item.recurring else "")
            for item in items
        ]
        return ToolResult("\n".join(lines))

    def reminder_done(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        item = reminders.mark_done(str(args["id"]))
        if item is None:
            return _error("not_found", f"Reminder {args['id']} not found")
        return ToolResult(f"Done: {item.id} · {item.text}")

    def reminder_cancel(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        item = reminders.cancel(str(args["id"]))
        if item is None:
            return _error("not_found", f"Reminder {args['id']} not found")
        return ToolResult(f"Cancelled: {item.id} · {item.text}")

    registry.register(
        Tool(
            "reminder_add",
            "Create a reminder. when=: 'in 2 hours' / 'через 2 часа', "
            "'at 07:00' / 'в 07:00', 'every day at 07:00' / 'каждый день в 07:00', ISO.",
            _schema(
                text={"type": "string", "_required": True},
                when={"type": "string", "_required": True},
            ),
            reminder_add,
        )
    )
    registry.register(
        Tool(
            "reminder_list",
            "List reminders.",
            _schema(include_done={"type": "boolean", "default": False}),
            reminder_list,
        )
    )
    registry.register(
        Tool(
            "reminder_done",
            "Mark a reminder done.",
            _schema(id={"type": "string", "_required": True}),
            reminder_done,
        )
    )
    registry.register(
        Tool(
            "reminder_cancel",
            "Cancel a reminder.",
            _schema(id={"type": "string", "_required": True}),
            reminder_cancel,
        )
    )


def _register_media_tools(registry: ToolRegistry) -> None:
    def clipboard_read(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        if platform.system() == "Darwin":
            cmd = ["pbpaste"]
        elif shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard", "-o"]
        elif shutil.which("wl-paste"):
            cmd = ["wl-paste"]
        else:
            return _error("unsupported", "No clipboard tool found (pbpaste/xclip/wl-paste).")
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return ToolResult(truncate_output(completed.stdout or "(empty clipboard)"))

    def clipboard_write(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        text = str(args["text"])
        if platform.system() == "Darwin":
            cmd = ["pbcopy"]
        elif shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard"]
        elif shutil.which("wl-copy"):
            cmd = ["wl-copy"]
        else:
            return _error("unsupported", "No clipboard tool found (pbcopy/xclip/wl-copy).")
        completed = subprocess.run(cmd, input=text, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            return _error("clipboard_failed", completed.stderr.strip() or "clipboard write failed")
        return ToolResult(f"Copied {len(text)} characters to clipboard")

    def screenshot(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        destination = Path(
            args.get("path")
            or (context.config.safety.working_directory / f"screenshot-{_stamp()}.png")
        ).expanduser()
        destination = (
            context.safety.check_write(destination)
            if destination.exists()
            else context.safety.check_create(destination)
        )
        if not context.confirm(f"Capture screenshot to {destination}?"):
            return _error("cancelled", "User declined screenshot.")
        if platform.system() == "Darwin":
            completed = subprocess.run(
                ["screencapture", "-x", str(destination)],
                capture_output=True,
                text=True,
                check=False,
            )
        elif shutil.which("gnome-screenshot"):
            completed = subprocess.run(
                ["gnome-screenshot", "-f", str(destination)],
                capture_output=True,
                text=True,
                check=False,
            )
        elif shutil.which("import"):
            completed = subprocess.run(
                ["import", "-window", "root", str(destination)],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            return _error("unsupported", "No screenshot tool found.")
        if completed.returncode != 0:
            return _error("screenshot_failed", completed.stderr.strip() or "screenshot failed")
        return ToolResult(f"Saved screenshot to {destination}")

    def list_media(context: ToolContext, args: dict[str, Any]) -> ToolResult:
        root = context.safety.check_read(
            args.get("path", context.config.safety.working_directory)
        )
        if not root.is_dir():
            return _error("not_directory", f"Not a directory: {root}")
        patterns = ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.mp3", "*.mp4", "*.mov")
        files: list[Path] = []
        for pattern in patterns:
            files.extend(root.glob(pattern))
        files = sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)[:40]
        lines = [f"{path.name} ({human_size(path.stat().st_size)})" for path in files]
        return ToolResult("\n".join(lines) or "(no media files)")

    registry.register(
        Tool("clipboard_read", "Read text from the clipboard.", _schema(), clipboard_read)
    )
    registry.register(
        Tool(
            "clipboard_write",
            "Write text to the clipboard.",
            _schema(text={"type": "string", "_required": True}),
            clipboard_write,
        )
    )
    registry.register(
        Tool(
            "screenshot",
            "Capture a screenshot to a file.",
            _schema(path={"type": "string"}),
            screenshot,
        )
    )
    registry.register(
        Tool(
            "list_media",
            "List recent media files under a folder.",
            _schema(path={"type": "string"}),
            list_media,
        )
    )


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
