"""Shared CLI helpers for every *installed* mode.

Not part of ``core`` (core stays mode-agnostic). Quick and Companion both may
use this module, but they never import each other.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from core.audit import APP_STATE_DIR
from core.config import (
    DEFAULT_CONFIG_PATH,
    PROVIDER_DEFAULTS,
    AppConfig,
    LLMProvider,
    LLMSettings,
    load_config,
    save_config,
)
from core.utils import arrow_select_style, get_console

VERSION = "0.4.0"
BACK = "__back__"
CONFIG_PATH = Path(os.getenv("J_AGENT_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()
INSTALL_HOME = Path(os.getenv("J_AGENT_HOME", Path.home() / ".local/share/j-the-agent"))
MANIFEST_PATH = INSTALL_HOME / "install-manifest.json"

_AT_PATH = re.compile(r"(?:^|\s)@(?:\"([^\"]+)\"|'([^']+)'|(\S+))")
console: Console = get_console()


def select(
    prompt: str,
    choices: list[Any],
    *,
    default: Any = None,
    back: bool = True,
) -> Any:
    """Arrow menu styled like the installer. ``default`` is ignored (no pre-selection)."""

    import questionary

    del default
    items = list(choices)
    if back:
        items.append(questionary.Choice("← Back", value=BACK))
    return questionary.select(
        prompt,
        choices=items,
        style=arrow_select_style(),
        instruction="(↑/↓, Enter)",
    ).ask()


def model_wizard(initial: LLMSettings | None = None) -> LLMSettings | None:
    """Provider → free-form model name → API key (stored in config)."""

    import questionary

    current = initial or LLMSettings()
    provider = current.provider
    model = current.resolved_model
    api_key = current.resolved_api_key
    base_url = current.resolved_base_url if provider is LLMProvider.OLLAMA else None
    step = 0

    while True:
        if step == 0:
            answer = select(
                "LLM provider:",
                [questionary.Choice(item.value, value=item) for item in LLMProvider],
            )
            if answer in (None, BACK):
                return None
            provider = answer
            model = PROVIDER_DEFAULTS[provider]["model"]
            base_url = (
                PROVIDER_DEFAULTS[provider]["base_url"] if provider is LLMProvider.OLLAMA else None
            )
            step = 1
            continue

        if step == 1:
            hint = PROVIDER_DEFAULTS[provider]["model"]
            entered = questionary.text(
                f"Model name (e.g. {hint}; type 'back' to return):",
                default=model or hint,
            ).ask()
            if entered is None or entered.strip().lower() == "back":
                step = 0
                continue
            if not entered.strip():
                console.print("[yellow]Please enter a model name.[/yellow]")
                continue
            model = entered.strip()
            step = 2
            continue

        if provider is LLMProvider.OLLAMA:
            entered = questionary.text(
                "Ollama URL (Enter for default; type 'back' to return):",
                default=base_url or PROVIDER_DEFAULTS[provider]["base_url"],
            ).ask()
            if entered is None or entered.strip().lower() == "back":
                step = 1
                continue
            base_url = entered.strip() or PROVIDER_DEFAULTS[provider]["base_url"]
            api_key = None
        else:
            entered = questionary.password(
                f"API key for {provider.value} (stored in config; type 'back' to return):"
            ).ask()
            if entered is None or entered.strip().lower() == "back":
                step = 1
                continue
            api_key = entered.strip() or None
            if not api_key:
                console.print(
                    "[yellow]API key is empty — set it later with ja switch model.[/yellow]"
                )
            base_url = None

        return LLMSettings(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=current.temperature,
            max_tokens=current.max_tokens,
            request_timeout=current.request_timeout,
        )


def choose_model(config: AppConfig) -> bool:
    llm = model_wizard(config.llm)
    if llm is None:
        return False
    config.llm = llm
    save_config(config, CONFIG_PATH)
    console.print(f"[green]Model switched to {llm.provider.value}:{llm.resolved_model}[/green]")
    return True


def choose_directory(config: AppConfig) -> bool:
    import questionary

    candidates = [config.safety.working_directory, *config.safety.allowed_paths]
    unique = list(dict.fromkeys(path.expanduser() for path in candidates))
    answer = select(
        "Working directory:",
        [
            *[questionary.Choice(str(path), value=str(path)) for path in unique],
            questionary.Choice("Custom directory…", value="__custom__"),
        ],
    )
    if answer in (None, BACK):
        return False
    if answer == "__custom__":
        entered = questionary.path(
            "Directory (type 'back' to return):",
            default=str(config.safety.working_directory),
            only_directories=True,
        ).ask()
        if entered is None or entered.strip().lower() == "back":
            return False
        path = Path(entered).expanduser().resolve()
    else:
        path = Path(answer).expanduser().resolve()
    if config.safety.access_mode.value == "selected" and path not in config.safety.allowed_paths:
        config.safety.allowed_paths.append(path)
    if config.safety.access_mode.value == "current_directory":
        config.safety.allowed_paths = [path]
    config.safety.working_directory = path
    save_config(config, CONFIG_PATH)
    console.print(f"[green]Working directory switched to {path}[/green]")
    return True


def extract_attachments(query: str) -> tuple[str, list[Path]]:
    paths = [
        Path(next(group for group in match.groups() if group is not None)).expanduser()
        for match in _AT_PATH.finditer(query)
    ]
    cleaned = _AT_PATH.sub(" ", query)
    return " ".join(cleaned.split()), paths


def render_answer(result: Any) -> None:
    if not result.streamed:
        console.print(Panel(result.answer or "(no answer)", title="J", border_style="green"))
    console.print(
        f"[dim]{result.iterations} step(s) · {result.total_tokens} tokens "
        f"({result.prompt_tokens} in / {result.completion_tokens} out)[/dim]\n"
    )


def manifest_path() -> Path:
    runtime_manifest = Path(sys.prefix).parent / "install-manifest.json"
    for candidate in (MANIFEST_PATH, runtime_manifest):
        if candidate.exists():
            return candidate
    return MANIFEST_PATH


def run_uninstall(*, purge: bool = False, yes: bool = False) -> None:
    """Remove installer-owned files; optionally purge config/state."""

    path = manifest_path()
    if not path.exists():
        console.print("[yellow]No install manifest found; nothing was removed.[/yellow]")
        raise typer.Exit(code=1)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    install_dir = Path(manifest["install_dir"]).expanduser()
    links = [Path(item).expanduser() for item in manifest.get("symlinks", [])]
    config_path = Path(manifest.get("config_path", CONFIG_PATH)).expanduser()
    state_dir = Path(manifest.get("state_dir", APP_STATE_DIR)).expanduser()
    console.print("[bold]J will remove:[/bold]")
    for link in links:
        console.print(f"  {link}")
    console.print(f"  {install_dir}")
    if purge:
        console.print(f"  {config_path.parent}")
        console.print(f"  {state_dir} (interaction history)")
    if not yes:
        import questionary

        if not questionary.confirm("Confirm complete uninstall?", default=False).ask():
            return
    for link in links:
        if link.is_symlink() or link.is_file():
            link.unlink(missing_ok=True)
    legacy_ka = Path(manifest.get("bin_dir", Path.home() / ".local/bin")) / "ka"
    if legacy_ka.is_symlink():
        target = legacy_ka.resolve(strict=False)
        if target.is_relative_to(install_dir):
            legacy_ka.unlink(missing_ok=True)
    if purge and config_path.parent.exists():
        shutil.rmtree(config_path.parent)
    if purge and state_dir.exists():
        shutil.rmtree(state_dir)
    path.unlink(missing_ok=True)
    if install_dir.exists():
        shutil.rmtree(install_dir, ignore_errors=True)
    if purge:
        console.print("[green]J the Agent and its owned data were removed.[/green]")
    else:
        console.print(
            "[green]J the Agent was removed; configuration and user data were kept.[/green]"
        )


def load_or_setup_shared(run_setup) -> AppConfig:
    """Helper kept for Quick; Companion uses its own tuple-returning loader."""

    if CONFIG_PATH.exists():
        return load_config()
    console.print("[yellow]No configuration found; starting first-run setup.[/yellow]")
    return run_setup(CONFIG_PATH)
