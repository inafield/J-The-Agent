"""Local reminders with JSON storage and cron / launchd schedules."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from core.config import APP_NAME
from modes.companion.launch import activate_for_reminder, resolve_ja_command

Status = Literal["pending", "due", "done", "cancelled"]


@dataclass
class Reminder:
    id: str
    text: str
    when: str  # ISO datetime or daily:HH:MM
    due_at: str  # ISO datetime of next/due fire
    status: Status = "pending"
    recurring: str | None = None  # "daily" | None
    created_at: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))
    schedule_ref: str | None = None  # plist label or cron tag

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reminder:
        return cls(
            id=str(data["id"]),
            text=str(data["text"]),
            when=str(data["when"]),
            due_at=str(data["due_at"]),
            status=data.get("status", "pending"),  # type: ignore[arg-type]
            recurring=data.get("recurring"),
            created_at=str(data.get("created_at") or _now().isoformat(timespec="seconds")),
            schedule_ref=data.get("schedule_ref"),
        )


def default_store_path() -> Path:
    state = os.getenv("J_AGENT_STATE_DIR")
    root = Path(state).expanduser() if state else Path.home() / ".local/state" / APP_NAME
    return root / "companion" / "reminders.json"


class ReminderStore:
    """Persist reminders and install OS schedules that call ``ja deliver-reminder``."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or default_store_path()).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def list(self, *, include_done: bool = False) -> list[Reminder]:
        items = self._load()
        if include_done:
            return items
        return [item for item in items if item.status not in {"done", "cancelled"}]

    def get(self, reminder_id: str) -> Reminder | None:
        return next((item for item in self._load() if item.id == reminder_id), None)

    def add(self, text: str, when: str) -> Reminder:
        text = text.strip()
        if not text:
            raise ValueError("Reminder text cannot be empty.")
        due_at, recurring = parse_when(when)
        reminder = Reminder(
            id=uuid.uuid4().hex[:10],
            text=text,
            when=when.strip(),
            due_at=due_at.isoformat(timespec="seconds"),
            recurring=recurring,
        )
        items = self._load()
        items.append(reminder)
        self._save(items)
        try:
            reminder.schedule_ref = install_schedule(reminder)
        except ScheduleError:
            self._save(items)
            raise
        self._save(items)
        return reminder

    def mark_done(self, reminder_id: str) -> Reminder | None:
        items = self._load()
        for item in items:
            if item.id != reminder_id:
                continue
            item.status = "done"
            remove_schedule(item)
            self._save(items)
            return item
        return None

    def cancel(self, reminder_id: str) -> Reminder | None:
        items = self._load()
        for item in items:
            if item.id != reminder_id:
                continue
            item.status = "cancelled"
            remove_schedule(item)
            self._save(items)
            return item
        return None

    def overdue(self, *, now: datetime | None = None) -> list[Reminder]:
        moment = (now or _now()).replace(tzinfo=None)
        due: list[Reminder] = []
        items = self._load()
        changed = False
        for item in items:
            if item.status in {"done", "cancelled"}:
                continue
            try:
                due_at = datetime.fromisoformat(item.due_at)
            except ValueError:
                continue
            if due_at.tzinfo is not None:
                due_at = due_at.astimezone().replace(tzinfo=None)
            if item.status == "due" or due_at <= moment:
                if item.status != "due":
                    item.status = "due"
                    changed = True
                due.append(item)
        if changed:
            self._save(items)
        return due

    def deliver(self, reminder_id: str, *, activate: bool = True) -> Reminder | None:
        """Called by launchd/cron. Notifies the user and updates schedule state."""

        items = self._load()
        for item in items:
            if item.id != reminder_id:
                continue
            if item.status in {"done", "cancelled"}:
                return item
            if item.recurring == "daily":
                previous = datetime.fromisoformat(item.due_at)
                now = _now()
                nxt = previous + timedelta(days=1)
                while nxt <= now:
                    nxt += timedelta(days=1)
                item.due_at = nxt.isoformat(timespec="seconds")
                item.status = "pending"
                remove_schedule(item)
                try:
                    item.schedule_ref = install_schedule(item)
                except ScheduleError:
                    item.schedule_ref = None
            else:
                item.status = "due"
                remove_schedule(item)
            self._save(items)
            if activate:
                activate_for_reminder(item.id, item.text)
            return item
        return None

    def remove_all_schedules(self) -> int:
        """Disable OS jobs while preserving reminder data for a later reinstall."""

        items = self._load()
        removed = 0
        for item in items:
            if not item.schedule_ref:
                continue
            remove_schedule(item)
            item.schedule_ref = None
            removed += 1
        if removed:
            self._save(items)
        return removed

    def reconcile_schedules(self) -> int:
        """Restore missing schedules for pending reminders after startup/reinstall."""

        items = self._load()
        restored = 0
        for item in items:
            if item.status != "pending" or item.schedule_ref:
                continue
            try:
                item.schedule_ref = install_schedule(item)
            except ScheduleError:
                item.schedule_ref = None
                continue
            if item.schedule_ref:
                restored += 1
        if restored:
            self._save(items)
        return restored

    def purge(self) -> None:
        """Remove schedules and the Companion-owned reminder database."""

        self.remove_all_schedules()
        self.path.unlink(missing_ok=True)

    def _load(self) -> list[Reminder]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [Reminder.from_dict(item) for item in raw.get("reminders", [])]

    def _save(self, items: list[Reminder]) -> None:
        payload = {"reminders": [asdict(item) for item in items]}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


_RELATIVE = re.compile(
    r"^(?:in\s+|через\s+)?(\d+)\s*"
    r"(minutes?|mins?|hours?|hrs?|days?|минут[уыа]?|мин\.?|час(?:а|ов)?|ч\.?|дн(?:я|ей|ень)?)$",
    re.IGNORECASE,
)
_DAILY = re.compile(
    r"^(?:every\s+day(?:\s+at)?|каждый\s+день(?:\s+в)?)\s+(\d{1,2}):(\d{2})$",
    re.IGNORECASE,
)
_AT_CLOCK = re.compile(
    r"^(?:at|в)\s+(\d{1,2}):(\d{2})$",
    re.IGNORECASE,
)


def parse_when(when: str) -> tuple[datetime, str | None]:
    """Parse human-ish when-strings into a due datetime and optional recurrence."""

    text = when.strip()
    now = _now()

    match = _RELATIVE.match(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith(("min", "мин")):
            return now + timedelta(minutes=amount), None
        if unit.startswith(("hour", "hr", "час", "ч")):
            return now + timedelta(hours=amount), None
        return now + timedelta(days=amount), None

    match = _DAILY.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        _validate_clock(hour, minute)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate, "daily"

    match = _AT_CLOCK.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        _validate_clock(hour, minute)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate, None

    # ISO / common datetime
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            "Unsupported when=. Try 'in 2 hours' / 'через 2 часа', "
            "'at 07:00' / 'в 07:00', 'every day at 07:00' / 'каждый день в 07:00', "
            "or an ISO datetime."
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed, None


@dataclass(frozen=True)
class ScheduleResult:
    """Outcome of installing or updating an OS schedule."""

    ok: bool
    ref: str | None = None
    detail: str = ""
    backend: str = ""


class ScheduleError(RuntimeError):
    """Raised when an OS scheduler cannot be updated."""

    def __init__(self, message: str, *, reminder_id: str | None = None) -> None:
        super().__init__(message)
        self.reminder_id = reminder_id


def install_schedule(reminder: Reminder) -> str | None:
    """Install a one-shot/daily OS job. Raises ``ScheduleError`` on failure."""

    ja = resolve_ja_command()
    due = datetime.fromisoformat(reminder.due_at)
    label = f"com.j-the-agent.reminder.{reminder.id}"
    if platform.system() == "Darwin":
        result = _install_launchd(
            label, ja, reminder.id, due, recurring=reminder.recurring
        )
    else:
        result = _install_linux_reminder(
            label, ja, reminder.id, due, recurring=reminder.recurring
        )
    if not result.ok:
        raise ScheduleError(
            result.detail or "Failed to install reminder schedule",
            reminder_id=reminder.id,
        )
    return result.ref


def remove_schedule(reminder: Reminder) -> None:
    if not reminder.schedule_ref:
        return
    if platform.system() == "Darwin":
        _remove_launchd(reminder.schedule_ref)
        return
    if reminder.schedule_ref.startswith("systemd:"):
        _remove_systemd_unit(reminder.schedule_ref.removeprefix("systemd:"))
        return
    _remove_cron(reminder.schedule_ref)


def _install_launchd(
    label: str,
    ja: str,
    reminder_id: str,
    due: datetime,
    *,
    recurring: str | None,
) -> ScheduleResult:
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / f"{label}.plist"
    if recurring == "daily":
        calendar = {"Hour": due.hour, "Minute": due.minute}
    else:
        calendar = {
            "Year": due.year,
            "Month": due.month,
            "Day": due.day,
            "Hour": due.hour,
            "Minute": due.minute,
        }
    with plist.open("wb") as handle:
        plistlib.dump(
            {
                "Label": label,
                "ProgramArguments": [ja, "deliver-reminder", reminder_id],
                "StartCalendarInterval": calendar,
                "RunAtLoad": False,
            },
            handle,
        )
    load = _launchctl_load(plist)
    if not load.ok:
        return ScheduleResult(ok=False, detail=load.detail, backend="launchd")
    return ScheduleResult(
        ok=True,
        ref=label,
        detail=f"launchd job {label}",
        backend="launchd",
    )


def _remove_launchd(label: str) -> None:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    uid = _uid()
    if plist.exists():
        # Already-unloaded jobs return non-zero; that is fine.
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(plist)],
            capture_output=True,
            check=False,
        )
        plist.unlink(missing_ok=True)


def _launchctl_load(plist: Path) -> ScheduleResult:
    uid = _uid()
    domain = f"gui/{uid}"
    # bootout fails when the job is not loaded yet — ignore that case only.
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist)],
        capture_output=True,
        check=False,
    )
    completed = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip() or "unknown error"
        return ScheduleResult(
            ok=False,
            detail=f"launchctl bootstrap failed for {plist.name}: {err}",
            backend="launchd",
        )
    return ScheduleResult(ok=True, detail=str(plist), backend="launchd")


def _install_linux_reminder(
    label: str,
    ja: str,
    reminder_id: str,
    due: datetime,
    *,
    recurring: str | None,
) -> ScheduleResult:
    # Prefer user systemd timers when available; otherwise crontab.
    if _systemd_user_available():
        return _install_systemd_reminder(
            label, ja, reminder_id, due, recurring=recurring
        )
    return _install_cron(label, ja, reminder_id, due, recurring=recurring)


def _install_cron(
    label: str,
    ja: str,
    reminder_id: str,
    due: datetime,
    *,
    recurring: str | None,
) -> ScheduleResult:
    if shutil.which("crontab") is None:
        return ScheduleResult(
            ok=False,
            detail="crontab is not available. Install cron or enable systemd --user timers.",
            backend="cron",
        )
    marker = f"# j-agent-reminder:{label}"
    command = f"{shlex.quote(ja)} deliver-reminder {shlex.quote(reminder_id)}"
    if recurring == "daily":
        line = f"{due.minute} {due.hour} * * * {command} {marker}"
    else:
        line = (
            f"{due.minute} {due.hour} {due.day} {due.month} * "
            f"{command} {marker}"
        )
    try:
        existing = _crontab_lines()
        existing = [row for row in existing if marker not in row]
        existing.append(line)
        _write_crontab(existing)
    except ScheduleError as exc:
        return ScheduleResult(ok=False, detail=str(exc), backend="cron")
    return ScheduleResult(ok=True, ref=label, detail=f"cron job {label}", backend="cron")


def _remove_cron(label: str) -> None:
    if shutil.which("crontab") is None:
        return
    marker = f"# j-agent-reminder:{label}"
    existing = [row for row in _crontab_lines() if marker not in row]
    _write_crontab(existing)


def _crontab_lines() -> list[str]:
    completed = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    # Empty crontab commonly exits 1 with "no crontab for user".
    if completed.returncode != 0:
        stderr = (completed.stderr or "").lower()
        if "no crontab" in stderr or not completed.stderr:
            return []
        raise ScheduleError(
            f"Could not read crontab: {(completed.stderr or completed.stdout).strip()}"
        )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def _write_crontab(lines: list[str]) -> None:
    payload = ("\n".join(lines) + "\n").encode() if lines else b""
    completed = subprocess.run(
        ["crontab", "-"],
        input=payload,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or b"").decode(errors="replace").strip()
        raise ScheduleError(f"Could not update crontab: {err or 'unknown error'}")


def _systemd_user_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    completed = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True,
        text=True,
        check=False,
    )
    # "running", "degraded", "offline" still mean the user bus is usable enough.
    return completed.returncode in {0, 1} or "offline" in (completed.stdout or "")


def _systemd_unit_dir() -> Path:
    path = Path.home() / ".config" / "systemd" / "user"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _install_systemd_reminder(
    label: str,
    ja: str,
    reminder_id: str,
    due: datetime,
    *,
    recurring: str | None,
) -> ScheduleResult:
    unit = label.replace(".", "-")
    service = _systemd_unit_dir() / f"{unit}.service"
    timer = _systemd_unit_dir() / f"{unit}.timer"
    service.write_text(
        "[Unit]\n"
        f"Description=J Companion reminder {reminder_id}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={shlex.quote(ja)} deliver-reminder {shlex.quote(reminder_id)}\n",
        encoding="utf-8",
    )
    if recurring == "daily":
        on_calendar = f"*-*-* {due.hour:02d}:{due.minute:02d}:00"
    else:
        on_calendar = (
            f"{due.year}-{due.month:02d}-{due.day:02d} "
            f"{due.hour:02d}:{due.minute:02d}:00"
        )
    timer.write_text(
        "[Unit]\n"
        f"Description=Timer for J Companion reminder {reminder_id}\n\n"
        "[Timer]\n"
        f"OnCalendar={on_calendar}\n"
        "Persistent=true\n"
        "Unit=" + unit + ".service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n",
        encoding="utf-8",
    )
    reload = subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        text=True,
        check=False,
    )
    if reload.returncode != 0:
        return ScheduleResult(
            ok=False,
            detail=f"systemctl --user daemon-reload failed: "
            f"{(reload.stderr or reload.stdout).strip()}",
            backend="systemd",
        )
    enable = subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{unit}.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    if enable.returncode != 0:
        return ScheduleResult(
            ok=False,
            detail=f"systemctl --user enable --now {unit}.timer failed: "
            f"{(enable.stderr or enable.stdout).strip()}",
            backend="systemd",
        )
    return ScheduleResult(
        ok=True,
        ref=f"systemd:{unit}",
        detail=f"systemd user timer {unit}.timer",
        backend="systemd",
    )


def _remove_systemd_unit(unit: str) -> None:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", f"{unit}.timer"],
        capture_output=True,
        check=False,
    )
    base = _systemd_unit_dir()
    (base / f"{unit}.timer").unlink(missing_ok=True)
    (base / f"{unit}.service").unlink(missing_ok=True)
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        check=False,
    )


def _uid() -> int:
    return os.getuid()


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _validate_clock(hour: int, minute: int) -> None:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Time must use a valid 24-hour HH:MM value.")


LOGIN_LABEL = "com.j-the-agent.companion.login-check"
_SYSTEMD_LOGIN_UNIT = "j-the-agent-companion-login-check"
_CRON_LOGIN_MARKER = "# j-agent-companion-login-check"


def install_login_check() -> ScheduleResult:
    """Install login/startup job: ``ja check-reminders`` (no LLM)."""

    ja = resolve_ja_command()
    if platform.system() == "Darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / f"{LOGIN_LABEL}.plist"
        with plist.open("wb") as handle:
            plistlib.dump(
                {
                    "Label": LOGIN_LABEL,
                    "ProgramArguments": [ja, "check-reminders"],
                    # Run when the agent is loaded at login (user GUI session).
                    "RunAtLoad": True,
                    "LimitLoadToSessionType": ["Aqua"],
                },
                handle,
            )
        load = _launchctl_load(plist)
        if not load.ok:
            return ScheduleResult(
                ok=False,
                detail=load.detail,
                backend="launchd",
            )
        return ScheduleResult(
            ok=True,
            ref=LOGIN_LABEL,
            detail=f"launchd RunAtLoad job {LOGIN_LABEL}",
            backend="launchd",
        )

    if _systemd_user_available():
        return _install_systemd_login(ja)
    return _install_cron_login(ja)


def _install_systemd_login(ja: str) -> ScheduleResult:
    service = _systemd_unit_dir() / f"{_SYSTEMD_LOGIN_UNIT}.service"
    timer = _systemd_unit_dir() / f"{_SYSTEMD_LOGIN_UNIT}.timer"
    service.write_text(
        "[Unit]\n"
        "Description=J Companion overdue reminder check\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={shlex.quote(ja)} check-reminders\n",
        encoding="utf-8",
    )
    timer.write_text(
        "[Unit]\n"
        "Description=Run J Companion reminder check after login/boot\n\n"
        "[Timer]\n"
        "OnStartupSec=30\n"
        "Persistent=true\n"
        f"Unit={_SYSTEMD_LOGIN_UNIT}.service\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    reload = subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        text=True,
        check=False,
    )
    if reload.returncode != 0:
        return ScheduleResult(
            ok=False,
            detail=f"systemctl --user daemon-reload failed: "
            f"{(reload.stderr or reload.stdout).strip()}",
            backend="systemd",
        )
    enable = subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{_SYSTEMD_LOGIN_UNIT}.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    if enable.returncode != 0:
        return ScheduleResult(
            ok=False,
            detail=(
                f"systemctl --user enable --now {_SYSTEMD_LOGIN_UNIT}.timer failed: "
                f"{(enable.stderr or enable.stdout).strip()}. "
                "If lingering is disabled, try: loginctl enable-linger $USER"
            ),
            backend="systemd",
        )
    return ScheduleResult(
        ok=True,
        ref=f"systemd:{_SYSTEMD_LOGIN_UNIT}",
        detail=f"systemd user timer {_SYSTEMD_LOGIN_UNIT}.timer (OnStartupSec=30)",
        backend="systemd",
    )


def _install_cron_login(ja: str) -> ScheduleResult:
    if shutil.which("crontab") is None:
        return ScheduleResult(
            ok=False,
            detail=(
                "Neither systemd --user nor crontab is available. "
                "Run `ja check-reminders` manually after login."
            ),
            backend="cron",
        )
    line = f"@reboot {shlex.quote(ja)} check-reminders {_CRON_LOGIN_MARKER}"
    try:
        existing = [row for row in _crontab_lines() if _CRON_LOGIN_MARKER not in row]
        existing.append(line)
        _write_crontab(existing)
    except ScheduleError as exc:
        return ScheduleResult(ok=False, detail=str(exc), backend="cron")
    return ScheduleResult(
        ok=True,
        ref=LOGIN_LABEL,
        detail=f"crontab @reboot job ({LOGIN_LABEL})",
        backend="cron",
    )


def remove_login_check() -> None:
    if platform.system() == "Darwin":
        _remove_launchd(LOGIN_LABEL)
        return
    _remove_systemd_unit(_SYSTEMD_LOGIN_UNIT)
    if shutil.which("crontab") is None:
        return
    try:
        existing = [row for row in _crontab_lines() if _CRON_LOGIN_MARKER not in row]
        _write_crontab(existing)
    except ScheduleError:
        pass


def login_check_installed() -> bool:
    if platform.system() == "Darwin":
        return (Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_LABEL}.plist").exists()
    unit_timer = _systemd_unit_dir() / f"{_SYSTEMD_LOGIN_UNIT}.timer"
    if unit_timer.exists():
        return True
    try:
        return any(_CRON_LOGIN_MARKER in row for row in _crontab_lines())
    except ScheduleError:
        return False


def check_reminders(*, notify_overdue: bool = True) -> list[Reminder]:
    """Reconcile schedules and surface overdue items (used at login / manually)."""

    from modes.companion.launch import notify
    from modes.companion.settings import load_companion_settings

    settings = load_companion_settings()
    store = ReminderStore(settings.reminders_path)
    store.reconcile_schedules()
    overdue = store.overdue()
    if notify_overdue and overdue:
        preview = "; ".join(item.text for item in overdue[:3])
        notify("J Companion", f"{len(overdue)} reminder(s) due: {preview}")
    return overdue
