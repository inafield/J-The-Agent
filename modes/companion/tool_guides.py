"""Progressive tool documentation loaded from Companion markdown guides."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_TOOL_NAME = re.compile(r"^###\s+`([a-z][a-z0-9_]*)`", re.MULTILINE)


@dataclass(frozen=True)
class ToolGuide:
    """One category of Companion tools, backed by a markdown file."""

    name: str
    path: Path
    title: str
    summary: str

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def tool_names(self) -> set[str]:
        return set(_TOOL_NAME.findall(self.read()))


class ToolGuideCatalog:
    """Loads ``tools/index.md`` plus per-category guides for Companion."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"Tool guides directory not found: {self.root}")

    @property
    def index_path(self) -> Path:
        return self.root / "index.md"

    def index(self) -> str:
        if not self.index_path.is_file():
            return self._synthetic_index()
        return self.index_path.read_text(encoding="utf-8").strip()

    def categories(self) -> list[str]:
        names = sorted(
            path.stem
            for path in self.root.glob("*.md")
            if path.name != "index.md" and not path.name.startswith("_")
        )
        return names

    def get(self, category: str) -> ToolGuide:
        key = category.strip().lower().removesuffix(".md")
        path = self.root / f"{key}.md"
        if not path.is_file():
            available = ", ".join(self.categories()) or "(none)"
            raise FileNotFoundError(f"Unknown tool category '{category}'. Available: {available}")
        title, summary = self._title_and_summary(path)
        return ToolGuide(name=key, path=path, title=title, summary=summary)

    def load(self, category: str) -> str:
        return self.get(category).read()

    def tool_names(self, category: str) -> set[str]:
        return self.get(category).tool_names()

    def all_tool_names(self) -> set[str]:
        names: set[str] = set()
        for category in self.categories():
            names |= self.tool_names(category)
        return names

    def _synthetic_index(self) -> str:
        lines = ["# Tool categories", ""]
        for category in self.categories():
            guide = self.get(category)
            lines.append(f"- `{guide.name}` — {guide.summary or guide.title}")
        return "\n".join(lines)

    @staticmethod
    def _title_and_summary(path: Path) -> tuple[str, str]:
        text = path.read_text(encoding="utf-8")
        title = path.stem
        summary = ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                continue
            if stripped.startswith("#"):
                continue
            summary = stripped
            break
        return title, summary
