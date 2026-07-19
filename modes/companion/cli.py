"""Command-line interface for J Companion (invoked via unified ``ja`` / ``agent``)."""

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
from core.config import AppConfig, LLMProvider, load_config, save_config
from core.safety import SafetyError
from core.utils import human_size
from modes.common_cli import (
    BACK,
    CONFIG_PATH,
    VERSION,
    choose_directory,
    choose_model,
    console,
    extract_attachments,
    model_wizard,
    render_answer,
    run_uninstall,
    select,
)
from modes.companion.hello import run_hello
from modes.companion.memory_store import MemoryStore
from modes.companion.permissions import (
    deny_path,
    run_companion_permission_wizard,
)
from modes.companion.reminders import (
    ReminderStore,
    check_reminders,
    install_login_check,
    login_check_installed,
    remove_login_check,
)
from modes.companion.settings import (
    WEB_SEARCH_OPTIONS,
    CompanionSettings,
    WebSearchProvider,
    load_companion_settings,
    save_companion_settings,
)

app = typer.Typer(no_args_is_help=False, add_completion=False, help="J the Agent — Companion")
switch_app = typer.Typer(help="Switch model or working directory.")
app.add_typer(switch_app, name="switch")


def _web_wizard(initial: CompanionSettings | None = None) -> CompanionSettings | None:
    import questionary

    current = initial or CompanionSettings()
    labels = [label for _, label in WEB_SEARCH_OPTIONS]
    defaults = {label: provider for provider, label in WEB_SEARCH_OPTIONS}
    choice = select(
        "Web search provider:",
        [questionary.Choice(label, value=label) for label in labels],
    )
    if choice in (None, BACK):
        return None
    provider = defaults[choice]
    api_key = current.web_api_key
    base_url = current.web_base_url

    if provider is WebSearchProvider.BRAVE or provider is WebSearchProvider.TAVILY:
        entered = questionary.password(
            f"API key for {provider.value} (type 'back' to return):"
        ).ask()
        if entered is None or entered.strip().lower() == "back":
            return None
        api_key = entered.strip() or None
        base_url = None
    elif provider is WebSearchProvider.SEARXNG:
        entered = questionary.text(
            "SearXNG instance URL (type 'back' to return):",
            default=base_url or "http://localhost:8080",
        ).ask()
        if entered is None or entered.strip().lower() == "back":
            return None
        base_url = entered.strip()
        api_key = None
    else:
        api_key = None
        base_url = None

    return current.model_copy(
        update={
            "web_provider": provider,
            "web_api_key": api_key,
            "web_base_url": base_url,
        }
    )


def _run_setup(
    config_path: Path,
    initial: AppConfig | None = None,
    companion_initial: CompanionSettings | None = None,
) -> tuple[AppConfig, CompanionSettings]:
    import questionary

    config = initial or AppConfig(mode="companion")
    config.mode = "companion"
    companion = companion_initial or load_companion_settings(config_path)
    console.print(
        Panel.fit(
            "[bold cyan]J the Agent — Companion setup[/bold cyan]\n"
            "Model, filesystem safety, web search, and presentation.",
            border_style="cyan",
        )
    )
    step = 0
    while True:
        if step == 0:
            llm = model_wizard(config.llm)
            if llm is None:
                raise typer.Abort
            config.llm = llm
            step = 1
            continue
        if step == 1:
            safety = run_companion_permission_wizard(config.safety, allow_back=True)
            if safety is None:
                step = 0
                continue
            config.safety = safety
            step = 2
            continue
        if step == 2:
            web = _web_wizard(companion)
            if web is None:
                step = 1
                continue
            companion = web
            step = 3
            continue
        if step == 3:
            reasoning = select(
                "Show progress, tool calls, and observations?",
                [
                    questionary.Choice("Yes", value=True),
                    questionary.Choice("No — prompt and final answer only", value=False),
                ],
            )
            if reasoning in (None, BACK):
                step = 2
                continue
            config.ui.show_reasoning = reasoning
            step = 4
            continue

        confirm = select(
            "Save this configuration?",
            [
                questionary.Choice("Save and finish", value=True),
                questionary.Choice("Cancel setup", value=False),
            ],
        )
        if confirm in (None, BACK):
            step = 3
            continue
        if not confirm:
            raise typer.Abort
        saved = save_config(config, config_path)
        save_companion_settings(companion, config_path)
        MemoryStore(companion.memory_dir).ensure()
        login = install_login_check()
        if login.ok:
            console.print(
                "[green]Login reminder check installed[/green] "
                f"({login.backend}: {login.detail})."
            )
        else:
            console.print(
                "[red]Failed to install login reminder check:[/red] "
                f"{login.detail}\n"
                "[dim]Run `ja check-reminders` manually after login.[/dim]"
            )
        console.print(f"[green]Configuration saved:[/green] {saved}")
        console.print()
        companion = run_hello(companion, config_path=config_path, full=True)
        return config, companion


def _load_or_setup() -> tuple[AppConfig, CompanionSettings]:
    if CONFIG_PATH.exists():
        return load_config(), load_companion_settings(CONFIG_PATH)
    console.print("[yellow]No configuration found; starting first-run setup.[/yellow]")
    return _run_setup(CONFIG_PATH)


def _show_overdue(items) -> None:
    if not items:
        return
    console.print(
        Panel(
            "\n".join(f"• [{item.id}] {item.text} (due {item.due_at})" for item in items),
            title="[bold yellow]Due reminders[/bold yellow]",
            border_style="yellow",
        )
    )


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Start interactive J Companion when no subcommand is given."""

    if ctx.invoked_subcommand is None:
        config, companion = _load_or_setup()
        if not companion.hello_completed:
            companion = run_hello(companion, config_path=CONFIG_PATH, full=True)
        _chat_loop(config, companion)


@app.command()
def setup() -> None:
    """Run the Companion setup wizard (including web search)."""

    initial = load_config() if CONFIG_PATH.exists() else None
    companion_initial = load_companion_settings(CONFIG_PATH) if CONFIG_PATH.exists() else None
    _run_setup(CONFIG_PATH, initial, companion_initial)


@app.command()
def hello() -> None:
    """Introduction interview, or update selected profile fields."""

    _config, companion = _load_or_setup()
    run_hello(companion, config_path=CONFIG_PATH)


@app.command("allow-local")
def allow_local(
    port: Annotated[
        Optional[int],
        typer.Argument(help="Loopback port to allow for fetch_url / open_url."),
    ] = None,
    remove: bool = typer.Option(False, "--remove", help="Remove a previously allowed port."),
    list_ports: bool = typer.Option(False, "--list", help="List allowed local ports."),
) -> None:
    """Allow loopback URLs like http://localhost:PORT (SSRF allowlist)."""

    companion = load_companion_settings(CONFIG_PATH)
    ports = list(companion.allowed_local_ports)
    if list_ports or port is None:
        if ports:
            console.print("Allowed local ports: " + ", ".join(str(p) for p in ports))
        else:
            console.print("[dim]No local ports allowed. Example: ja allow-local 3000[/dim]")
        if port is None:
            return
    if not 1 <= int(port) <= 65535:
        console.print("[red]Port must be between 1 and 65535.[/red]")
        raise typer.Exit(code=2)
    value = int(port)
    if remove:
        if value not in ports:
            console.print(f"[yellow]Port {value} was not on the allowlist.[/yellow]")
            return
        ports = [p for p in ports if p != value]
        companion.allowed_local_ports = ports
        save_companion_settings(companion, CONFIG_PATH)
        console.print(f"[green]Removed local port {value}.[/green]")
        return
    if value not in ports:
        ports.append(value)
        ports.sort()
    companion.allowed_local_ports = ports
    save_companion_settings(companion, CONFIG_PATH)
    console.print(
        f"[green]Allowed[/green] http://localhost:{value} and http://127.0.0.1:{value} "
        "(fetch/open still ask for confirmation)."
    )


reminders_app = typer.Typer(help="Manage reminders without calling the LLM.")
app.add_typer(reminders_app, name="reminders")


@reminders_app.callback(invoke_without_command=True)
def reminders_main(ctx: typer.Context) -> None:
    """List reminders when no subcommand is given."""

    if ctx.invoked_subcommand is None:
        reminders_list()


@reminders_app.command("list")
def reminders_list(
    all_items: bool = typer.Option(
        False, "--all", help="Include done and cancelled reminders."
    ),
) -> None:
    """List reminders (no LLM)."""

    companion = load_companion_settings(CONFIG_PATH)
    store = ReminderStore(companion.reminders_path)
    items = store.list(include_done=all_items)
    if not items:
        console.print("[dim]No reminders.[/dim]")
        return
    table = Table(title="Reminders", border_style="cyan")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Due")
    table.add_column("Text")
    for item in items:
        table.add_row(item.id, item.status, item.due_at, item.text)
    console.print(table)


@reminders_app.command("done")
def reminders_done(
    reminder_id: str = typer.Argument(help="Reminder id to mark done."),
) -> None:
    """Mark a reminder done (no LLM)."""

    companion = load_companion_settings(CONFIG_PATH)
    store = ReminderStore(companion.reminders_path)
    item = store.mark_done(reminder_id)
    if item is None:
        console.print(f"[red]Reminder {reminder_id} not found.[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Done:[/green] {item.id} · {item.text}")


@app.command()
def permissions() -> None:
    """Change Companion filesystem safety (system paths + optional denies)."""

    config, _companion = _load_or_setup()
    safety = run_companion_permission_wizard(config.safety)
    if safety is not None:
        config.safety = safety
        config.mode = "companion"
        save_config(config, CONFIG_PATH)
        console.print("[green]Permissions updated.[/green]")


@app.command()
def deny(
    path: Annotated[str, typer.Argument(help="File or directory path to forbid.")],
) -> None:
    """Forbid a file or directory path (no LLM)."""

    config, _companion = _load_or_setup()
    try:
        config.safety = deny_path(config.safety, path)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    config.mode = "companion"
    save_config(config, CONFIG_PATH)
    console.print(f"[green]Forbidden:[/green] {path}")


@switch_app.command("model")
def switch_model() -> None:
    """Switch LLM provider/model."""

    config, _companion = _load_or_setup()
    choose_model(config)


@switch_app.command("dir")
@switch_app.command("directory")
def switch_directory() -> None:
    """Switch working directory."""

    config, _companion = _load_or_setup()
    choose_directory(config)


@app.command()
def ask(
    question: Annotated[list[str], typer.Argument(help="Question for J (quotes optional).")],
    files: Annotated[
        Optional[list[Path]],
        typer.Option("--file", "-f", help="Attach a file/directory."),
    ] = None,
    max_iterations: Annotated[int, typer.Option(min=1, max=12)] = 8,
) -> None:
    """Ask one question and exit."""

    from modes.companion.agent import CompanionAgent

    config, companion = _load_or_setup()
    prompt = " ".join(question).strip()
    if not prompt:
        console.print("[red]Please provide a question.[/red]")
        raise typer.Exit(code=2)
    agent = CompanionAgent(
        config, companion=companion, console=console, max_iterations=max_iterations
    )
    try:
        result = agent.run(prompt, attachments=list(files or []))
    except SafetyError as exc:
        agent.close("attachment blocked")
        console.print(f"[red]Attachment blocked: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    agent.close("one-shot completed")
    render_answer(result)


@app.command("check-reminders")
def check_reminders_cmd(
    quiet: bool = typer.Option(False, "--quiet", help="Only notify; minimal stdout."),
) -> None:
    """Reconcile schedules and show overdue reminders (no LLM). Used at login."""

    overdue = check_reminders(notify_overdue=True)
    if quiet:
        # Always exit 0 so launchd/cron login jobs are not marked failed.
        raise typer.Exit(code=0)
    if not overdue:
        console.print("[green]No overdue reminders.[/green]")
        return
    _show_overdue(overdue)
    console.print("[dim]Mark done: ja reminders done ID · or ja deliver-reminder ID --interactive[/dim]")


@app.command("deliver-reminder")
def deliver_reminder(
    reminder_id: str = typer.Argument(help="Reminder id from the local store."),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="Show the reminder in this terminal (used after auto-launch).",
    ),
) -> None:
    """Fire a scheduled reminder (called by launchd/cron, or manually)."""

    companion = load_companion_settings(CONFIG_PATH)
    store = ReminderStore(companion.reminders_path)
    if interactive:
        item = store.get(reminder_id)
        if item is None:
            console.print(f"[red]Reminder {reminder_id} not found.[/red]")
            raise typer.Exit(code=1)
        console.print(
            Panel(
                f"{item.text}\n\n[dim]id={item.id} · due={item.due_at}[/dim]",
                title="[bold]J reminder[/bold]",
                border_style="yellow",
            )
        )
        import questionary

        if questionary.confirm("Mark as done?", default=True).ask():
            store.mark_done(reminder_id)
            console.print("[green]Marked done.[/green]")
        return

    item = store.deliver(reminder_id, activate=True)
    if item is None:
        console.print(f"[red]Reminder {reminder_id} not found.[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Delivered reminder {item.id}.[/green]")


@app.command()
def info() -> None:
    """Show Companion configuration."""

    from modes.companion.agent import CompanionAgent

    config, companion = _load_or_setup()
    table = Table(title="J Companion", show_header=False, border_style="cyan")
    table.add_row("Mode", "companion")
    table.add_row("Provider", config.llm.provider.value)
    table.add_row("Model", config.llm.resolved_model)
    table.add_row("Agent name", companion.agent_name or "J")
    table.add_row("User", companion.user_name or "(not set)")
    table.add_row("Language", companion.language)
    table.add_row("Timezone", f"{companion.timezone_city} ({companion.timezone})")
    table.add_row("Hello", "done" if companion.hello_completed else "pending")
    table.add_row("Memory", str(companion.memory_dir))
    table.add_row("Web search", companion.web_provider.value)
    table.add_row("Login check", "on" if login_check_installed() else "off")
    table.add_row("Working directory", str(config.safety.working_directory))
    agent = CompanionAgent(config, companion=companion, console=console)
    table.add_row("Tools", ", ".join(agent.registry.names()))
    agent.close("info")
    console.print(table)


@app.command()
def doctor() -> None:
    """Diagnose Companion install, memory, web, reminders, and login check."""

    from modes.common_cli import manifest_path
    from modes.companion.agent import CompanionAgent

    table = Table(title="J Companion Doctor", border_style="cyan")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    def row(name: str, ok: bool, details: str) -> None:
        table.add_row(name, "[green]OK[/green]" if ok else "[yellow]WARN[/yellow]", details)

    path = manifest_path()
    row(
        "Installation",
        path.exists(),
        f"manifest: {path}" if path.exists() else "pip/dev install; no manifest",
    )
    for command in ("ja", "agent"):
        found = shutil.which(command)
        row(f"Command {command}", bool(found), found or "not found in PATH")
    try:
        package_version = metadata.version("j-the-agent")
    except metadata.PackageNotFoundError:
        package_version = "source checkout"
    row("Package", package_version == VERSION, f"runtime {VERSION}; installed {package_version}")
    row("Python", sys.version_info >= (3, 11), f"{platform.python_version()} · {sys.executable}")
    row("Config", CONFIG_PATH.exists(), str(CONFIG_PATH))

    if CONFIG_PATH.exists():
        companion = load_companion_settings(CONFIG_PATH)
    else:
        companion = CompanionSettings()
    memory = MemoryStore(companion.memory_dir)
    memory.ensure()
    row("Memory dir", companion.memory_dir.is_dir(), str(companion.memory_dir))
    for name in ("user.md", "soul.md", "memory.md"):
        file = companion.memory_dir / name
        row(f"Memory {name}", file.is_file(), str(file))

    reminders = ReminderStore(companion.reminders_path)
    pending = reminders.list()
    overdue = reminders.overdue()
    row(
        "Reminders store",
        True,
        f"{companion.reminders_path} · {len(pending)} active · {len(overdue)} due",
    )
    row(
        "Login check",
        login_check_installed(),
        "ja check-reminders at login/startup"
        if login_check_installed()
        else "missing — re-run ja setup",
    )
    if companion.allowed_local_ports:
        row(
            "Local URL ports",
            True,
            ", ".join(str(p) for p in companion.allowed_local_ports),
        )
    else:
        row("Local URL ports", True, "none (ja allow-local PORT)")

    web_ok = companion.web_provider is not WebSearchProvider.NONE
    web_details = companion.web_provider.value
    if companion.web_provider is WebSearchProvider.DUCKDUCKGO:
        web_details += " (free; paid providers usually more reliable)"
    elif companion.web_provider in {WebSearchProvider.BRAVE, WebSearchProvider.TAVILY}:
        web_ok = bool(companion.web_api_key)
        web_details += " · API key " + ("set" if companion.web_api_key else "missing")
    elif companion.web_provider is WebSearchProvider.SEARXNG:
        web_ok = bool(companion.web_base_url)
        web_details += f" · {companion.web_base_url or 'URL missing'}"
    row("Web search", web_ok or companion.web_provider is WebSearchProvider.NONE, web_details)

    history_details = str(DEFAULT_HISTORY_PATH)
    if DEFAULT_HISTORY_PATH.exists():
        history_details += f" · {human_size(DEFAULT_HISTORY_PATH.stat().st_size)}"
    row("History log", DEFAULT_HISTORY_PATH.exists(), history_details)

    config = load_config() if CONFIG_PATH.exists() else None
    if config:
        provider_ok, provider_details = _check_provider(config)
        row("LLM", provider_ok, provider_details)
        working_ok = config.safety.working_directory.exists()
        row("Working directory", working_ok, str(config.safety.working_directory))
        agent = CompanionAgent(config, companion=companion, console=console)
        row("Tools", bool(agent.registry.names()), f"{len(agent.registry.names())} loaded")
        agent.close("doctor")
    else:
        row("Runtime config", False, "run: ja setup")

    console.print(table)
    console.print(
        "[dim]Doctor is read-only. It does not change configuration or contact cloud APIs "
        "except a local Ollama ping when configured.[/dim]"
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

    console.print(f"J the Agent {VERSION} · Companion")


@app.command()
def uninstall(
    purge: bool = typer.Option(False, "--purge", help="Also remove J's configuration."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Remove files recorded by the installer manifest."""

    if not yes:
        import questionary

        if not questionary.confirm("Confirm complete uninstall?", default=False).ask():
            return
    companion = load_companion_settings(CONFIG_PATH)
    reminder_store = ReminderStore(companion.reminders_path)
    remove_login_check()
    if purge:
        reminder_store.purge()
        MemoryStore(companion.memory_dir).purge()
    else:
        reminder_store.remove_all_schedules()
    run_uninstall(purge=purge, yes=True)


def _chat_loop(config: AppConfig, companion: CompanionSettings) -> None:
    from modes.companion.agent import CompanionAgent

    store = ReminderStore(companion.reminders_path)
    store.reconcile_schedules()
    if not login_check_installed():
        login = install_login_check()
        if not login.ok:
            console.print(
                f"[yellow]Login reminder check not installed:[/yellow] {login.detail}"
            )
    _show_overdue(store.overdue())
    agent = CompanionAgent(config, companion=companion, console=console)
    history = None
    config_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
    agent_label = companion.agent_name or "J"
    user_bit = f" · hi {companion.address_as or companion.user_name}" if companion.user_name else ""
    console.print(
        Panel.fit(
            f"[bold]{agent_label} Companion[/bold] · "
            f"{config.llm.provider.value}:{config.llm.resolved_model}{user_bit}\n"
            f"Lang: {companion.language} · Web: {companion.web_provider.value} · "
            f"TZ: {companion.timezone_city}\n"
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
            if choose_model(config):
                config = load_config()
                companion = load_companion_settings(CONFIG_PATH)
                agent.reload_config(config, companion=companion)
                history = None
                config_mtime = CONFIG_PATH.stat().st_mtime
            continue
        if raw.casefold() in {
            "/switch dir",
            "/switch directory",
            "switch dir",
            "switch directory",
        }:
            if choose_directory(config):
                config = load_config()
                agent.reload_config(config, companion=companion)
                config_mtime = CONFIG_PATH.stat().st_mtime
            continue
        current_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
        if current_mtime != config_mtime:
            config = load_config()
            companion = load_companion_settings(CONFIG_PATH)
            agent.reload_config(config, companion=companion)
            config_mtime = current_mtime
            console.print("[cyan]Configuration hot-reloaded.[/cyan]")
        query, attachments = extract_attachments(raw)
        console.print(f"[dim]prompt: {query}[/dim]")
        try:
            result = agent.run(query, history=history, attachments=attachments)
        except SafetyError as exc:
            console.print(f"[red]Attachment blocked: {exc}[/red]")
            continue
        history = result.history
        render_answer(result)


if __name__ == "__main__":
    app()
