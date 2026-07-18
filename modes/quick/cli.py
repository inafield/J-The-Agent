"""Command-line interface for J Quick."""

from __future__ import annotations

import platform
import shutil
import sys
import urllib.error
import urllib.request
from importlib import metadata
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.panel import Panel
from rich.table import Table

from core.audit import DEFAULT_HISTORY_PATH
from core.config import (
    AppConfig,
    LLMProvider,
    LLMSettings,
    load_config,
    save_config,
)
from core.safety import SafetyError, run_permission_wizard
from core.utils import human_size
from modes import common_cli
from modes.common_cli import (
    BACK as _BACK,
)
from modes.common_cli import (
    CONFIG_PATH as _CONFIG_PATH,
)
from modes.common_cli import (
    MANIFEST_PATH as _MANIFEST_PATH,
)
from modes.common_cli import (
    VERSION as _VERSION,
)
from modes.common_cli import (
    choose_directory as _choose_directory,
)
from modes.common_cli import (
    choose_model as _choose_model,
)
from modes.common_cli import (
    console,
    run_uninstall,
)
from modes.common_cli import (
    extract_attachments as _extract_attachments,
)
from modes.common_cli import (
    manifest_path as _common_manifest_path,
)
from modes.common_cli import (
    model_wizard as _common_model_wizard,
)
from modes.common_cli import (
    render_answer as _render_answer,
)
from modes.common_cli import (
    select as _select,
)


def _model_wizard(initial: LLMSettings | None = None) -> LLMSettings | None:
    saved = common_cli.select
    common_cli.select = _select
    try:
        return _common_model_wizard(initial)
    finally:
        common_cli.select = saved


def _manifest_path() -> Path:
    saved = common_cli.MANIFEST_PATH
    common_cli.MANIFEST_PATH = _MANIFEST_PATH
    try:
        return _common_manifest_path()
    finally:
        common_cli.MANIFEST_PATH = saved


app = typer.Typer(no_args_is_help=False, add_completion=False, help="J the Agent — Quick")
switch_app = typer.Typer(help="Switch model or working directory.")
app.add_typer(switch_app, name="switch")


def _run_setup(config_path: Path, initial: AppConfig | None = None) -> AppConfig:
    import questionary

    config = initial or AppConfig(mode="quick")
    config.mode = "quick"
    console.print(
        Panel.fit(
            "[bold cyan]J the Agent — Quick setup[/bold cyan]\n"
            "Configure the model, permissions, and output.",
            border_style="cyan",
        )
    )
    step = 0
    while True:
        if step == 0:
            llm = _model_wizard(config.llm)
            if llm is None:
                raise typer.Abort
            config.llm = llm
            step = 1
            continue
        if step == 1:
            safety = run_permission_wizard(config.safety, allow_back=True)
            if safety is None:
                step = 0
                continue
            config.safety = safety
            step = 2
            continue
        if step == 2:
            reasoning = _select(
                "Show progress, tool calls, and observations?",
                [
                    questionary.Choice("Yes", value=True),
                    questionary.Choice("No — prompt and final answer only", value=False),
                ],
                default=config.ui.show_reasoning,
            )
            if reasoning in (None, _BACK):
                step = 1
                continue
            config.ui.show_reasoning = reasoning
            step = 3
            continue

        confirm = _select(
            "Save this configuration?",
            [
                questionary.Choice("Save and finish", value=True),
                questionary.Choice("Cancel setup", value=False),
            ],
        )
        if confirm in (None, _BACK):
            step = 2
            continue
        if not confirm:
            raise typer.Abort
        saved = save_config(config, config_path)
        console.print(f"[green]Configuration saved:[/green] {saved}")
        return config


def _load_or_setup() -> AppConfig:
    if _CONFIG_PATH.exists():
        return load_config()
    console.print("[yellow]No configuration found; starting first-run setup.[/yellow]")
    return _run_setup(_CONFIG_PATH)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Start interactive J Quick when no subcommand is given."""

    if ctx.invoked_subcommand is None:
        _chat_loop(_load_or_setup())


@app.command()
def setup() -> None:
    """Run the complete setup wizard."""

    initial = load_config() if _CONFIG_PATH.exists() else None
    _run_setup(_CONFIG_PATH, initial)


@app.command()
def permissions() -> None:
    """Change filesystem permissions."""

    config = _load_or_setup()
    safety = run_permission_wizard(config.safety)
    if safety is not None:
        config.safety = safety
        config.mode = "quick"
        save_config(config, _CONFIG_PATH)
        console.print("[green]Permissions updated.[/green]")


@switch_app.command("model")
def switch_model() -> None:
    """Switch LLM provider/model without the full setup."""

    config = _load_or_setup()
    _choose_model(config)


def _switch_directory() -> None:
    config = _load_or_setup()
    _choose_directory(config)


switch_app.command("dir")(_switch_directory)
switch_app.command("directory")(_switch_directory)


@app.command()
def ask(
    question: Annotated[list[str], typer.Argument(help="Question for J (quotes are optional).")],
    files: Annotated[
        Optional[list[Path]],
        typer.Option("--file", "-f", help="Attach a file/directory."),
    ] = None,
    max_iterations: Annotated[
        int,
        typer.Option(min=1, max=12, help="Maximum ReAct steps."),
    ] = 6,
) -> None:
    """Ask one question and exit."""

    from modes.quick.agent import QuickAgent

    config = _load_or_setup()
    prompt = " ".join(question).strip()
    if not prompt:
        console.print("[red]Please provide a question.[/red]")
        raise typer.Exit(code=2)
    if not config.ui.show_reasoning:
        console.print(f"[dim]prompt: {prompt}[/dim]")
    agent = QuickAgent(config, console=console, max_iterations=max_iterations)
    try:
        result = agent.run(
            prompt,
            attachments=list(files or []),
        )
    except SafetyError as exc:
        agent.close("attachment blocked")
        console.print(f"[red]Attachment blocked: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    agent.close("one-shot completed")
    _render_answer(result)


@app.command()
def info() -> None:
    """Show active configuration and Quick tools."""

    from modes.quick.tools import build_registry

    config = _load_or_setup()
    table = Table(title="J Quick", show_header=False, border_style="cyan")
    table.add_row("Provider", config.llm.provider.value)
    table.add_row("Model", config.llm.resolved_model)
    table.add_row("Working directory", str(config.safety.working_directory))
    table.add_row("Access mode", config.safety.access_mode.value)
    table.add_row("Reasoning display", "on" if config.ui.show_reasoning else "off")
    table.add_row("Forbidden", ", ".join(map(str, config.safety.forbidden_paths)))
    table.add_row("Tools", ", ".join(build_registry(config).names()))
    console.print(table)


@app.command()
def doctor() -> None:
    """Diagnose installation, configuration, LLM, plugins, and permissions."""

    from modes.quick.tools import build_registry

    table = Table(title="J Doctor", border_style="cyan")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    def row(name: str, ok: bool, details: str) -> None:
        table.add_row(name, "[green]OK[/green]" if ok else "[yellow]WARN[/yellow]", details)

    manifest = _manifest_path()
    row(
        "Installation",
        manifest.exists(),
        f"manifest: {manifest}" if manifest.exists() else "pip/dev install; no manifest",
    )
    for command in ("ja", "agent"):
        found = shutil.which(command)
        row(f"Command {command}", bool(found), found or "not found in PATH")
    try:
        package_version = metadata.version("j-the-agent")
    except metadata.PackageNotFoundError:
        package_version = "source checkout / legacy package"
    row("Package", package_version == _VERSION, f"runtime {_VERSION}; installed {package_version}")
    row("Python", sys.version_info >= (3, 11), f"{platform.python_version()} · {sys.executable}")
    row("Config", _CONFIG_PATH.exists(), str(_CONFIG_PATH))
    history_details = str(DEFAULT_HISTORY_PATH)
    if DEFAULT_HISTORY_PATH.exists():
        history_details += f" · {human_size(DEFAULT_HISTORY_PATH.stat().st_size)}"
    row("History log", DEFAULT_HISTORY_PATH.exists(), history_details)

    config = load_config() if _CONFIG_PATH.exists() else None
    if config:
        provider_ok, provider_details = _check_provider(config)
        row("LLM", provider_ok, provider_details)
        try:
            config.safety.working_directory.resolve().relative_to(Path("/"))
            working_ok = config.safety.working_directory.exists()
        except (OSError, ValueError):
            working_ok = False
        row("Working directory", working_ok, str(config.safety.working_directory))
        registry = build_registry(config)
        row("Tools", bool(registry.names()), f"{len(registry.names())} loaded")
        plugin_count = (
            len(list(config.plugins_dir.glob("*.py"))) if config.plugins_dir.is_dir() else 0
        )
        row("Plugins", True, f"{plugin_count} file(s) · {config.plugins_dir}")
        row(
            "Security",
            bool(config.safety.forbidden_paths or config.safety.forbidden_patterns),
            f"profile={config.safety.profile.value}; mode={config.safety.access_mode.value}",
        )
    else:
        row("Runtime config", False, "run: ja setup")
    console.print(table)
    console.print(
        "[dim]Doctor is read-only. It does not change configuration or contact cloud APIs.[/dim]"
    )


def _check_provider(config: AppConfig) -> tuple[bool, str]:
    if config.llm.provider is not LLMProvider.OLLAMA:
        key_present = bool(config.llm.resolved_api_key)
        return key_present, (
            f"{config.llm.provider.value}:{config.llm.resolved_model} · "
            f"API key {'found' if key_present else 'missing'}"
        )
    request = urllib.request.Request(f"{config.llm.resolved_base_url}/api/tags")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            ok = response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"Ollama unavailable at {config.llm.resolved_base_url}: {exc}"
    return ok, f"Ollama reachable · model={config.llm.resolved_model}"


@app.command()
def version() -> None:
    """Show the installed version."""

    console.print(f"J the Agent {_VERSION}")


@app.command()
def uninstall(
    purge: bool = typer.Option(False, "--purge", help="Also remove J's configuration."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Remove only files recorded by the installer manifest."""

    saved = common_cli.MANIFEST_PATH
    common_cli.MANIFEST_PATH = _MANIFEST_PATH
    try:
        run_uninstall(purge=purge, yes=yes)
    finally:
        common_cli.MANIFEST_PATH = saved


def _chat_loop(config: AppConfig) -> None:
    from modes.quick.agent import QuickAgent

    agent = QuickAgent(config, console=console)
    history = None
    config_mtime = _CONFIG_PATH.stat().st_mtime if _CONFIG_PATH.exists() else 0.0
    console.print(
        Panel.fit(
            f"[bold]J Quick[/bold] · {config.llm.provider.value}:{config.llm.resolved_model}\n"
            "Use @path to attach context. Type exit or press Ctrl-C to finish.",
            border_style="cyan",
        )
    )
    while True:
        try:
            raw = console.input("[bold green]you ›[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            agent.close("terminal interrupt")
            console.print("\nJ stopped.")
            return
        if not raw:
            continue
        if raw.lower() in {"exit", "quit", ":q"}:
            agent.close("user exit")
            console.print("J stopped.")
            return
        if raw.casefold() in {"/switch model", "switch model"}:
            if _choose_model(config):
                config = load_config()
                agent.reload_config(config)
                history = None
                config_mtime = _CONFIG_PATH.stat().st_mtime
            continue
        if raw.casefold() in {
            "/switch dir",
            "/switch directory",
            "switch dir",
            "switch directory",
        }:
            if _choose_directory(config):
                config = load_config()
                agent.reload_config(config)
                config_mtime = _CONFIG_PATH.stat().st_mtime
            continue
        current_mtime = _CONFIG_PATH.stat().st_mtime if _CONFIG_PATH.exists() else 0.0
        if current_mtime != config_mtime:
            config = load_config()
            agent.reload_config(config)
            config_mtime = current_mtime
            console.print("[cyan]Configuration hot-reloaded.[/cyan]")
        query, attachments = _extract_attachments(raw)
        console.print(f"[dim]prompt: {query}[/dim]")
        try:
            result = agent.run(query, history=history, attachments=attachments)
        except SafetyError as exc:
            console.print(f"[red]Attachment blocked: {exc}[/red]")
            continue
        history = result.history
        _render_answer(result)


if __name__ == "__main__":
    app()
