"""Mode-independent tool contracts, registry, and plugin autoloading.

A *tool* is a named, JSON-schema-described function the LLM can call. Tools are
grouped in a :class:`ToolRegistry`. Every filesystem/command tool receives a
:class:`ToolContext` carrying the :class:`~core.safety.SafetyGuard`, so security
checks are consistently available. Concrete tools belong to individual modes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.audit import InteractionLogger
from core.config import AppConfig
from core.llm import ToolSpec
from core.safety import SafetyError, SafetyGuard
from core.utils import get_console

ToolHandler = Callable[["ToolContext", dict[str, Any]], str]


@dataclass
class ToolContext:
    """Everything a tool needs, injected at execution time."""

    config: AppConfig
    safety: SafetyGuard
    confirm: Callable[[str], bool]
    logger: InteractionLogger | None = None


@dataclass
class Tool:
    """A callable exposed to the LLM."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


@dataclass
class ToolResult:
    """Outcome of a tool execution, ready to feed back into the model."""

    output: str
    ok: bool = True
    code: str = "ok"
    suggestion: str | None = None
    data: dict[str, Any] | None = None

    def as_observation(self) -> str:
        """Return a structured payload that is easy for an LLM to recover from."""

        return json.dumps(
            {
                "ok": self.ok,
                "code": self.code,
                "output": self.output,
                "suggestion": self.suggestion,
                "data": self.data,
            },
            ensure_ascii=False,
            default=str,
        )


class ToolRegistry:
    """A simple name-to-tool map with execution and OpenAI-style specs."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, override: bool = False) -> None:
        if tool.name in self._tools and not override:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Decorator form for registering a handler."""

        def decorator(handler: ToolHandler) -> ToolHandler:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    parameters=parameters or {"type": "object", "properties": {}},
                    handler=handler,
                )
            )
            return handler

        return decorator

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, names: set[str] | None = None) -> list[ToolSpec]:
        return [tool.spec() for name, tool in self._tools.items() if names is None or name in names]

    def execute(self, name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                f"Unknown tool '{name}'. Available: {', '.join(self.names())}",
                ok=False,
                code="unknown_tool",
                suggestion="Choose one of the tools listed in the output.",
            )
        try:
            result = tool.handler(context, arguments)
            return result if isinstance(result, ToolResult) else ToolResult(result)
        except SafetyError as exc:
            return ToolResult(
                f"Blocked by safety policy: {exc}",
                ok=False,
                code="safety_block",
                suggestion="Use an allowed path or ask the user to change permissions.",
            )
        except Exception as exc:  # noqa: BLE001 - surface any tool error back to the model.
            return ToolResult(
                f"Error while running '{name}': {exc}",
                ok=False,
                code="tool_error",
                suggestion="Inspect the error and try a different tool or corrected arguments.",
            )


# --------------------------------------------------------------------------- #
# Plugin autoloading
# --------------------------------------------------------------------------- #


def load_plugins(registry: ToolRegistry, plugins_dir: str | Path) -> list[str]:
    """Import every ``*.py`` in ``plugins_dir`` and call its ``register(registry)``.

    Returns the names of successfully loaded plugin modules. Files starting with
    ``_`` are ignored, so ``example.py`` acts as a template.
    """

    directory = Path(plugins_dir).expanduser()
    if not directory.is_dir():
        return []

    loaded: list[str] = []
    for file in sorted(directory.glob("*.py")):
        if file.name.startswith("_"):
            continue
        module_name = f"j_agent_plugin_{file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if callable(register):
                register(registry)
                loaded.append(file.stem)
        except Exception as exc:  # noqa: BLE001 - a bad plugin must not crash the agent.
            get_console().print(f"[yellow]Skipped plugin {file.name}: {exc}[/yellow]")
    return loaded
