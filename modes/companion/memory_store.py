"""Persistent markdown memory for Companion mode."""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path

MEMORY_FILES = ("user.md", "soul.md", "memory.md")


@dataclass(frozen=True)
class MemoryPaths:
    """Resolved paths for the three Companion memory files."""

    root: Path

    @property
    def user(self) -> Path:
        return self.root / "user.md"

    @property
    def soul(self) -> Path:
        return self.root / "soul.md"

    @property
    def memory(self) -> Path:
        return self.root / "memory.md"

    def path_for(self, name: str) -> Path:
        key = name.strip().lower().removesuffix(".md")
        mapping = {
            "user": self.user,
            "soul": self.soul,
            "memory": self.memory,
        }
        if key not in mapping:
            raise ValueError(f"Unknown memory file '{name}'. Use: {', '.join(MEMORY_FILES)}")
        return mapping[key]


class MemoryStore:
    """Read/write Companion memory files with size budgets for prompt injection."""

    def __init__(
        self,
        root: str | Path,
        *,
        templates_dir: str | Path | None = None,
        max_chars: dict[str, int] | None = None,
    ) -> None:
        self.paths = MemoryPaths(Path(root).expanduser())
        self.templates_dir = Path(templates_dir).expanduser() if templates_dir else None
        self.max_chars = max_chars or {
            "user": 4_000,
            "soul": 3_000,
            "memory": 8_000,
        }

    @property
    def root(self) -> Path:
        return self.paths.root

    def ensure(self) -> None:
        """Create the memory directory and seed missing files from templates."""

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in MEMORY_FILES:
            destination = self.root / name
            if destination.exists():
                continue
            if self.templates_dir is not None:
                template = self.templates_dir / name
                if template.is_file():
                    shutil.copy2(template, destination)
                    destination.chmod(0o600)
                    continue
            destination.write_text(self._fallback_template(name), encoding="utf-8")
            destination.chmod(0o600)

    def read(self, name: str, *, max_chars: int | None = None) -> str:
        path = self.paths.path_for(name)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        budget = (
            max_chars
            if max_chars is not None
            else self.max_chars.get(name.removesuffix(".md"), 8_000)
        )
        if len(text) <= budget:
            return text
        keep = budget // 2
        return f"{text[:keep]}\n\n... [memory truncated for context] ...\n\n{text[-keep:]}"

    def write(self, name: str, content: str) -> Path:
        path = self.paths.path_for(name)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def append(self, name: str, content: str) -> Path:
        path = self.paths.path_for(name)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        block = content.strip()
        if not block:
            return path
        separator = "" if not existing or existing.endswith("\n\n") else "\n"
        path.write_text(existing + separator + block + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def prompt_block(self) -> str:
        """Compact memory bundle for the system prompt."""

        sections = [
            ("soul.md", self.read("soul")),
            ("user.md", self.read("user")),
            ("memory.md", self.read("memory")),
        ]
        parts = ["# Companion memory (trusted local files)"]
        for title, body in sections:
            body = body.strip() or "(empty)"
            parts.append(f"## {title}\n{body}")
        return "\n\n".join(parts)

    def purge(self) -> None:
        """Remove only Companion-owned memory files, never an arbitrary parent tree."""

        for name in MEMORY_FILES:
            (self.root / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self.root.rmdir()

    @staticmethod
    def _fallback_template(name: str) -> str:
        if name == "user.md":
            return (
                "# User\n\n"
                "- Name:\n"
                "- Preferred language:\n"
                "- Timezone:\n"
                "- Preferences:\n"
                "- Important context:\n"
            )
        if name == "soul.md":
            return (
                "# Soul\n\n"
                "You are J Companion — a warm, practical personal assistant "
                "on the user's computer.\n"
                "Be friendly and attentive, mirror the user's communication style, stay useful.\n"
                "Do not overdo personality. Prefer clear help over fluff.\n"
            )
        return (
            "# Long-term memory\n\n"
            "Write durable facts, decisions, and open loops here.\n"
            "Prefer short dated bullets. Remove stale notes when they no longer matter.\n"
        )
