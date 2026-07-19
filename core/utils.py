"""Small shared helpers with no mode-specific dependencies."""

from __future__ import annotations

from functools import lru_cache

from rich.console import Console


@lru_cache(maxsize=1)
def get_console() -> Console:
    """Return a shared Rich console so output stays consistent everywhere."""

    return Console()


def truncate_output(text: str, max_chars: int = 4000, max_lines: int = 120) -> str:
    """Shorten long tool output to keep prompts (and token usage) small.

    The middle is dropped rather than the tail so both the start and the end of
    a command's output survive, which is what usually matters for diagnosis.
    """

    lines = text.splitlines()
    if len(lines) > max_lines:
        head = lines[: max_lines // 2]
        tail = lines[-max_lines // 2 :]
        omitted = len(lines) - len(head) - len(tail)
        lines = [*head, f"... [{omitted} lines omitted] ...", *tail]
        text = "\n".join(lines)

    if len(text) > max_chars:
        keep = max_chars // 2
        text = f"{text[:keep]}\n... [truncated] ...\n{text[-keep:]}"
    return text


def human_size(num_bytes: float) -> str:
    """Format a byte count using binary units."""

    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def confirm(prompt: str, *, default: bool = False) -> bool:
    """Ask the user a yes/no question via the shared console."""

    from rich.prompt import Confirm

    return Confirm.ask(prompt, default=default, console=get_console())


@lru_cache(maxsize=1)
def arrow_select_style():
    """Questionary style matching ``install.sh`` (green active row, no reverse video)."""

    from questionary import Style

    return Style(
        [
            ("qmark", "fg:cyan bold"),
            ("question", "bold"),
            ("answer", "fg:cyan"),
            ("pointer", "fg:green bold"),
            ("highlighted", "fg:green bold"),
            ("selected", "fg:green"),
            ("separator", "fg:ansigray"),
            ("instruction", "fg:ansigray"),
            ("text", ""),
            ("disabled", "fg:ansigray italic"),
        ]
    )