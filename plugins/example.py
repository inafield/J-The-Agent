"""Example plugin — a template for adding your own tools.

How autoloading works
---------------------
On startup the agent scans the plugins directory (``plugins_dir`` in your
config, by default ``~/.config/j-the-agent/plugins``) and imports every ``*.py``
file that does NOT start with an underscore. For each imported module it calls a
top-level ``register(registry)`` function, if present.

To enable this example:

    mkdir -p ~/.config/j-the-agent/plugins
    cp plugins/example.py ~/.config/j-the-agent/plugins/

Then run ``agent info`` — you will see ``reverse_text`` in the tool list.

Every tool receives a ``ToolContext`` (config + SafetyGuard + confirm callback)
and a dict of arguments described by a JSON schema. Use ``context.safety`` for
any filesystem or command access so your tool honours the user's policy.
"""

from __future__ import annotations

from typing import Any

from core.tools import Tool, ToolContext, ToolRegistry


def _reverse_text(context: ToolContext, args: dict[str, Any]) -> str:
    return str(args.get("text", ""))[::-1]


def register(registry: ToolRegistry) -> None:
    """Entry point called by the plugin loader."""

    registry.register(
        Tool(
            name="reverse_text",
            description="Reverse the characters of the given text.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to reverse"},
                },
                "required": ["text"],
            },
            handler=_reverse_text,
        ),
        override=True,
    )
