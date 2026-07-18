"""Unified ``ja`` / ``agent`` entrypoint — routes to the *installed* mode only.

Quick and Companion are separate products. An install ships ``core`` plus exactly
one of ``modes/quick`` or ``modes/companion``. They must not import each other;
shared helpers live in ``modes.common_cli`` / ``modes.runtime``.
"""

from __future__ import annotations

from modes.runtime import resolve_mode


def _load_app():
    mode = resolve_mode()
    try:
        if mode == "companion":
            from modes.companion.cli import app as mode_app
        else:
            from modes.quick.cli import app as mode_app
    except ImportError as exc:
        raise SystemExit(
            f"Mode '{mode}' is not present in this installation. "
            "Re-run scripts/install.sh and choose Quick or Companion."
        ) from exc
    return mode_app


app = _load_app()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
