# Plugins

Drop-in tools that the agent autoloads at startup.

## How it works

1. The agent scans `plugins_dir` from your config (default:
   `~/.config/j-the-agent/plugins`).
2. It imports every `*.py` file whose name does **not** start with `_`.
3. For each module it calls the top-level `register(registry)` function.

## Add your own

Use [`example.py`](./example.py) as a template:

```bash
mkdir -p ~/.config/j-the-agent/plugins
cp plugins/example.py ~/.config/j-the-agent/plugins/
agent info   # your new tool appears in the tool list
```

A minimal plugin:

```python
from core.tools import Tool, ToolContext, ToolRegistry


def _hello(context, args):
    return f"Hello, {args.get('name', 'world')}!"


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="hello",
        description="Say hello.",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}},
        handler=_hello,
    ))
```

Always route file or command access through `context.safety` so your tool
respects the user's permission policy.
