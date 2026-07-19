"""First-run / reconfigure introduction interview for Companion (English UI)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from rich.panel import Panel

from modes.common_cli import BACK, console, select
from modes.companion.memory_store import MemoryStore
from modes.companion.settings import CompanionSettings, save_companion_settings
from modes.companion.tools import DEFAULT_TEMPLATES_DIR

# City label → IANA timezone (arrow menu). UI stays English.
TIMEZONE_CITIES: list[tuple[str, str]] = [
    ("UTC", "UTC"),
    ("London", "Europe/London"),
    ("Berlin", "Europe/Berlin"),
    ("Paris", "Europe/Paris"),
    ("Moscow", "Europe/Moscow"),
    ("Istanbul", "Europe/Istanbul"),
    ("Dubai", "Asia/Dubai"),
    ("Delhi", "Asia/Kolkata"),
    ("Singapore", "Asia/Singapore"),
    ("Hong Kong", "Asia/Hong_Kong"),
    ("Tokyo", "Asia/Tokyo"),
    ("Seoul", "Asia/Seoul"),
    ("Sydney", "Australia/Sydney"),
    ("Vladivostok", "Asia/Vladivostok"),
    ("Auckland", "Pacific/Auckland"),
    ("Los Angeles", "America/Los_Angeles"),
    ("Denver", "America/Denver"),
    ("Chicago", "America/Chicago"),
    ("New York", "America/New_York"),
    ("Sao Paulo", "America/Sao_Paulo"),
    ("Custom IANA id…", "__custom__"),
]

FIELD_LABELS: dict[str, str] = {
    "user_name": "Your name",
    "agent_name": "Agent name",
    "language": "Preferred language",
    "timezone_city": "Timezone (city)",
    "timezone": "Timezone (IANA)",
}


@dataclass
class Profile:
    user_name: str = ""
    agent_name: str = "J"
    language: str = "en"  # en | ru
    timezone_city: str = "UTC"
    timezone: str = "UTC"

    @classmethod
    def from_settings(cls, settings: CompanionSettings) -> Profile:
        return cls(
            user_name=settings.user_name or "",
            agent_name=settings.agent_name or "J",
            language=settings.language or "en",
            timezone_city=settings.timezone_city or "UTC",
            timezone=settings.timezone or "UTC",
        )

    def has_data(self) -> bool:
        return bool(self.user_name.strip())


def run_hello(
    settings: CompanionSettings,
    *,
    config_path: Path,
    full: bool | None = None,
) -> CompanionSettings:
    """Run introduction interview. ``full=None`` auto-picks full vs field picker."""

    memory = MemoryStore(settings.memory_dir, templates_dir=DEFAULT_TEMPLATES_DIR)
    memory.ensure()
    profile = Profile.from_settings(settings)

    do_full = full if full is not None else not (settings.hello_completed and profile.has_data())
    if do_full:
        console.print(_intro_panel())
        updated = _interview_full(profile)
        if updated is None:
            console.print("[yellow]Introduction cancelled.[/yellow]")
            return settings
        profile = updated
    else:
        updated = _interview_selective(profile)
        if updated is None:
            console.print("[yellow]No changes.[/yellow]")
            return settings
        profile = updated

    settings = _apply_profile(settings, profile, memory)
    save_companion_settings(settings, config_path)
    console.print(
        f"[green]Nice to meet you"
        f"{', ' + profile.user_name if profile.user_name else ''}![/green] "
        f"I am {profile.agent_name}."
    )
    return settings


def _intro_panel() -> Panel:
    return Panel.fit(
        "[bold cyan]Getting to know each other[/bold cyan]\n"
        "A short introduction so Companion can help you better.\n"
        "UI is English; you can still chat in Russian or English later.\n"
        "I will learn more about you over time and save it to memory myself.",
        border_style="cyan",
    )


def _interview_full(profile: Profile) -> Profile | None:
    import questionary

    current = Profile(**asdict(profile))
    step = 0
    while True:
        if step == 0:
            name = questionary.text(
                "What should I call you? (type 'back' to cancel)",
                default=current.user_name or "",
            ).ask()
            if name is None or name.strip().lower() == "back":
                return None
            if not name.strip():
                console.print("[yellow]Please enter a name.[/yellow]")
                continue
            current.user_name = name.strip()
            step = 1
            continue
        if step == 1:
            agent = questionary.text(
                "What should I call myself? (default: J)",
                default=current.agent_name or "J",
            ).ask()
            if agent is None or agent.strip().lower() == "back":
                step = 0
                continue
            current.agent_name = agent.strip() or "J"
            step = 2
            continue
        if step == 2:
            lang = select(
                "Preferred chat language:",
                [
                    questionary.Choice("English", value="en"),
                    questionary.Choice("Russian", value="ru"),
                ],
            )
            if lang in (None, BACK):
                step = 1
                continue
            current.language = lang
            step = 3
            continue
        if step == 3:
            tz = _pick_timezone(current)
            if tz is None:
                step = 2
                continue
            current.timezone_city, current.timezone = tz
            step = 4
            continue

        confirm = select(
            "Save this introduction?",
            [
                questionary.Choice("Save", value=True),
                questionary.Choice("Cancel", value=False),
            ],
        )
        if confirm in (None, BACK):
            step = 3
            continue
        if not confirm:
            return None
        return current


def _interview_selective(profile: Profile) -> Profile | None:
    import questionary

    current = Profile(**asdict(profile))
    choices = [
        questionary.Choice(
            f"{FIELD_LABELS[key]} — {_preview(getattr(current, key))}",
            value=key,
        )
        for key in ("user_name", "agent_name", "language", "timezone_city")
    ]
    choices.append(questionary.Choice("Replace all fields…", value="__all__"))
    choices.append(questionary.Choice("Cancel", value="__cancel__"))

    picked = select("Which introduction field should I update?", choices, back=False)
    if picked in (None, "__cancel__"):
        return None
    if picked == "__all__":
        return _interview_full(current)

    if picked == "language":
        lang = select(
            "Preferred chat language:",
            [
                questionary.Choice("English", value="en"),
                questionary.Choice("Russian", value="ru"),
            ],
            back=False,
        )
        if lang is None:
            return None
        current.language = lang
    elif picked == "timezone_city":
        tz = _pick_timezone(current)
        if tz is None:
            return None
        current.timezone_city, current.timezone = tz
    else:
        default = getattr(current, picked) or ""
        if picked == "agent_name":
            default = default or "J"
        entered = questionary.text(
            f"New value for {FIELD_LABELS[picked]}:",
            default=str(default),
        ).ask()
        if entered is None:
            return None
        value = entered.strip()
        if picked == "user_name" and not value:
            console.print("[yellow]Name cannot be empty.[/yellow]")
            return None
        if picked == "agent_name":
            value = value or "J"
        setattr(current, picked, value)

    confirm = select(
        f"Replace {FIELD_LABELS.get(picked, picked)}?",
        [
            questionary.Choice("Yes, save", value=True),
            questionary.Choice("No, discard", value=False),
        ],
        back=False,
    )
    if not confirm:
        return None
    return current


def _pick_timezone(profile: Profile) -> tuple[str, str] | None:
    import questionary

    labels = [label for label, _ in TIMEZONE_CITIES]
    choice = select(
        "Timezone (choose a city):",
        [questionary.Choice(label, value=label) for label in labels],
    )
    if choice in (None, BACK):
        return None
    mapping = dict(TIMEZONE_CITIES)
    zone = mapping[choice]
    if zone == "__custom__":
        entered = questionary.text(
            "IANA timezone (e.g. Europe/Moscow):",
            default=profile.timezone if profile.timezone != "UTC" else "Europe/Moscow",
        ).ask()
        if entered is None or entered.strip().lower() == "back":
            return None
        custom = entered.strip() or "UTC"
        return (f"Custom ({custom})", custom)
    return (choice, zone)


def _apply_profile(
    settings: CompanionSettings,
    profile: Profile,
    memory: MemoryStore,
) -> CompanionSettings:
    settings.user_name = profile.user_name
    settings.agent_name = profile.agent_name or "J"
    settings.language = profile.language if profile.language in {"en", "ru"} else "en"
    settings.address_as = profile.user_name
    settings.timezone_city = profile.timezone_city
    settings.timezone = profile.timezone
    settings.hello_completed = True

    memory.write("user", render_user_md(profile))
    memory.write("soul", render_soul_md(profile))
    return settings


def render_user_md(profile: Profile) -> str:
    lang = "Russian" if profile.language == "ru" else "English"
    return (
        "# User\n\n"
        f"- Name: {profile.user_name}\n"
        f"- Preferred language: {lang} ({profile.language})\n"
        f"- Timezone: {profile.timezone_city} ({profile.timezone})\n"
        "- Important ongoing context:\n"
        "  (Companion will fill this in as we work together.)\n"
    )


def render_soul_md(profile: Profile) -> str:
    name = profile.agent_name or "J"
    lang_line = (
        "Reply in Russian by default; switch to English if the user writes in English."
        if profile.language == "ru"
        else "Reply in English by default; switch to Russian if the user writes in Russian."
    )
    return (
        f"# Soul\n\n"
        f"You are **{name}** — a warm, practical personal assistant living on this computer.\n"
        f"The user may call you {name}.\n\n"
        f"## Character\n"
        f"- Friendly and attentive, never clingy or theatrical.\n"
        f"- Mirror the user's language and energy; stay clear and useful.\n"
        f"- {lang_line}\n"
        f"- Prefer short, concrete help over long essays unless asked.\n"
        f"- Remember what matters; ask before overwriting important facts.\n"
        f"- Learn about the user over time and persist durable facts in user.md / memory.md.\n\n"
        f"## Working style\n"
        f"- Use tools when facts or actions are needed; do not invent system state.\n"
        f"- Load a tool category via `load_tool_guide` for tips, then call the unlocked tools.\n"
        f"- Keep memory tidy: write durable facts to `memory.md`, profile updates to `user.md`.\n"
        f"- Respect SafetyGuard. Dangerous actions need explicit user confirmation.\n\n"
        f"## Boundaries\n"
        f"- You are not a therapist substitute or a sycophant.\n"
        f"- Do not claim feelings you do not have; be sincere and practical.\n"
        f"- Private local data stays local unless the user asks to share it.\n"
    )


def _preview(value: str) -> str:
    text = (value or "").strip() or "(empty)"
    return text if len(text) <= 40 else text[:37] + "…"
