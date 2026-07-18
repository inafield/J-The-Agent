"""Human-readable, user-owned interaction history.

The log is deliberately write-only from the agent's perspective: it is never
fed back into prompts or used as memory. Sensitive-looking values are redacted
before they reach disk.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

APP_STATE_DIR = Path(
    os.getenv(
        "J_AGENT_STATE_DIR",
        Path.home() / ".local" / "state" / "j-the-agent",
    )
).expanduser()
DEFAULT_HISTORY_PATH = APP_STATE_DIR / "history.log"

_SECRET_KEYS = re.compile(
    r"(api[_-]?key|authorization|password|passwd|secret|token|credential)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_KEY_VALUE = re.compile(r"(?i)\b(api[_-]?key|password|secret|token)\s*[:=]\s*([^\s,;]+)")


def redact(value: Any) -> Any:
    """Recursively redact common secret shapes while preserving readability."""

    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = _BEARER.sub(r"\1[REDACTED]", value)
        return _KEY_VALUE.sub(r"\1=[REDACTED]", text)
    return value


class InteractionLogger:
    """Append formatted interaction events to a private local file."""

    def __init__(self, path: str | Path = DEFAULT_HISTORY_PATH) -> None:
        self.path = Path(path).expanduser()
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)

    def session_start(self, *, provider: str, model: str, mode: str = "quick") -> None:
        self._write(
            "SESSION START",
            {
                "mode": mode,
                "provider": provider,
                "model": model,
                "pid": os.getpid(),
            },
            separator="=",
        )

    def session_end(self, reason: str = "completed") -> None:
        self._write("SESSION END", {"reason": reason}, separator="=")

    def event(self, title: str, data: Any = None) -> None:
        self._write(title, data)

    def llm_request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> None:
        self._write("LLM REQUEST", {"messages": messages, "tools": tools})

    def llm_response(self, response: dict[str, Any]) -> None:
        self._write("LLM RESPONSE", response)

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self._write(f"TOOL CALL · {name}", arguments)

    def tool_result(self, name: str, result: Any) -> None:
        self._write(f"TOOL RESULT · {name}", result)

    def approval(self, prompt: str, decision: str) -> None:
        self._write("USER APPROVAL", {"prompt": prompt, "decision": decision})

    def _write(self, title: str, data: Any, *, separator: str = "-") -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        line = separator * 78
        body = self._render(redact(data))
        block = f"\n{line}\n[{timestamp}] {title}\n{line}\n"
        if body:
            block += f"{body}\n"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(block)

    @staticmethod
    def _render(data: Any) -> str:
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
