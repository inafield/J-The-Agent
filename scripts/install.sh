#!/usr/bin/env bash
# J the Agent installer — suitable for a checkout or a GitHub curl invocation.
set -euo pipefail

REPO_URL="${J_AGENT_REPO_URL:-https://github.com/inafield/J-The-Agent.git}"
REPO_BRANCH="${J_AGENT_REPO_BRANCH:-main}"
INSTALL_DIR="${J_AGENT_HOME:-$HOME/.local/share/j-the-agent}"
BIN_DIR="${J_AGENT_BIN_DIR:-$HOME/.local/bin}"
CONFIG_PATH="${J_AGENT_CONFIG:-$HOME/.config/j-the-agent/config.yaml}"
STATE_DIR="${J_AGENT_STATE_DIR:-$HOME/.local/state/j-the-agent}"
HISTORY_PATH="$STATE_DIR/history.log"
VENV_DIR="$INSTALL_DIR/.venv"
MANIFEST_PATH="$INSTALL_DIR/install-manifest.json"
export J_AGENT_HOME="$INSTALL_DIR"
export J_AGENT_CONFIG="$CONFIG_PATH"
export J_AGENT_STATE_DIR="$STATE_DIR"

BOLD="$(printf '\033[1m')"; CYAN="$(printf '\033[36m')"; GREEN="$(printf '\033[32m')"
YELLOW="$(printf '\033[33m')"; RED="$(printf '\033[31m')"; RESET="$(printf '\033[0m')"
info() { printf '%b\n' "${CYAN}$*${RESET}"; }
ok() { printf '%b\n' "${GREEN}$*${RESET}"; }
warn() { printf '%b\n' "${YELLOW}$*${RESET}"; }
fail() { printf '%b\n' "${RED}$*${RESET}" >&2; exit 1; }

arrow_menu() {
  local prompt="$1"; shift
  local options=("$@") selected=0 key i
  printf '%b\n' "${BOLD}${prompt}${RESET}" >&2
  while true; do
    for i in "${!options[@]}"; do
      if [[ "$i" -eq "$selected" ]]; then
        printf '  %b❯ %s%b\n' "$GREEN" "${options[$i]}" "$RESET" >&2
      else
        printf '    %s\n' "${options[$i]}" >&2
      fi
    done
    IFS= read -rsn1 key
    if [[ "$key" == $'\x1b' ]]; then
      # macOS ships Bash 3.2, where fractional `read -t` values are invalid.
      read -rsn2 -t 1 key || true
      case "$key" in
        "[A") selected=$(((selected - 1 + ${#options[@]}) % ${#options[@]})) ;;
        "[B") selected=$(((selected + 1) % ${#options[@]})) ;;
      esac
    elif [[ -z "$key" ]]; then
      break
    fi
    printf '\033[%dA' "${#options[@]}" >&2
  done
  printf '%d' "$selected"
}

pick_python() {
  local candidate version
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      version="$("$candidate" -c 'import sys; print(sys.version_info >= (3, 11))')"
      if [[ "$version" == "True" ]]; then
        printf '%s' "$candidate"
        return
      fi
    fi
  done
  fail "Python 3.11+ is required. Existing Python installations are never modified."
}

resolve_source() {
  local mode="$1"
  local script_dir project_dir source
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  project_dir="$(cd "$script_dir/.." 2>/dev/null && pwd || true)"
  if [[ -f "$project_dir/pyproject.toml" && -d "$project_dir/core" && -d "$project_dir/modes" ]]; then
    case "$INSTALL_DIR/" in
      "$project_dir/"|"$project_dir/"*)
        fail "J_AGENT_HOME must be outside the project source directory."
        ;;
    esac
    [[ -d "$project_dir/modes/$mode" ]] || fail "Mode '$mode' is not available in this checkout."
    source="$INSTALL_DIR/source"
    rm -rf "$source"
    mkdir -p "$source/modes"
    # Core + shared mode router only; Quick and Companion never ship together.
    cp -R "$project_dir/core" "$source/"
    cp "$project_dir/modes/__init__.py" \
       "$project_dir/modes/cli.py" \
       "$project_dir/modes/runtime.py" \
       "$project_dir/modes/common_cli.py" \
       "$source/modes/"
    cp -R "$project_dir/modes/$mode" "$source/modes/"
    cp "$project_dir/pyproject.toml" "$project_dir/README.md" "$source/"
    printf '%s' "$source"
    return
  fi
  command -v git >/dev/null 2>&1 || fail "git is required for a GitHub installation."
  mkdir -p "$INSTALL_DIR"
  source="$INSTALL_DIR/source"
  if [[ -d "$source/.git" ]]; then
    git -C "$source" fetch --depth 1 origin "$REPO_BRANCH" >&2
    git -C "$source" checkout -f FETCH_HEAD >&2
  else
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$source" >&2
  fi
  prune_other_modes "$mode" "$source"
  printf '%s' "$source"
}

# Keep one product mode under modes/; both depend on core, never on each other.
prune_other_modes() {
  local mode="$1" source="$2" other
  for other in quick companion; do
    if [[ "$other" != "$mode" && -d "$source/modes/$other" ]]; then
      rm -rf "$source/modes/$other"
    fi
  done
  [[ -d "$source/modes/$mode" ]] || fail "Mode '$mode' missing after fetch."
}

write_manifest() {
  local mode="$1"
  mkdir -p "$INSTALL_DIR"
  cat >"$MANIFEST_PATH" <<JSON
{
  "version": 3,
  "mode": "$mode",
  "install_dir": "$INSTALL_DIR",
  "venv": "$VENV_DIR",
  "bin_dir": "$BIN_DIR",
  "symlinks": ["$BIN_DIR/agent", "$BIN_DIR/ja"],
  "config_path": "$CONFIG_PATH",
  "state_dir": "$STATE_DIR",
  "history_path": "$HISTORY_PATH"
}
JSON
}

remove_legacy_ka() {
  local legacy="$BIN_DIR/ka" target=""
  if [[ -L "$legacy" ]]; then
    target="$(readlink "$legacy" || true)"
    if [[ "$target" == *"j-the-agent"* ]]; then
      rm -f "$legacy"
    fi
  fi
}

install_mode() {
  local mode="$1" label="$2"
  local source python
  source="$(resolve_source "$mode")"
  python="$(pick_python)"
  info "\nInstalling J ${label} into $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  touch "$HISTORY_PATH"
  chmod 600 "$HISTORY_PATH"
  "$python" -m venv --clear "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
  # Mode selection is done by this script (prune + manifest). pip only installs
  # the pruned tree (core + shared router + the chosen mode) and dependencies.
  "$VENV_DIR/bin/pip" install "$source"
  ln -sfn "$VENV_DIR/bin/agent" "$BIN_DIR/agent"
  ln -sfn "$VENV_DIR/bin/ja" "$BIN_DIR/ja"
  remove_legacy_ka
  write_manifest "$mode"
  # Ensure the unified entrypoint routes to this mode even before setup saves config.
  export J_AGENT_MODE="$mode"

  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not in PATH. Add this to your shell profile:"
       warn "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac

  ok "\nJ ${label} is installed. Starting first-run setup…"
  J_AGENT_MODE="$mode" "$VENV_DIR/bin/ja" setup || warn "Setup was cancelled. Run 'ja setup' later."
  ok "\nStart J with:  ${BOLD}ja${RESET}  or  ${BOLD}agent${RESET}"
  info "Detailed interaction history: $HISTORY_PATH"
}

uninstall_from_manifest() {
  local purge="${1:-false}"
  if [[ -x "$VENV_DIR/bin/ja" ]]; then
    if [[ "$purge" == "true" ]]; then
      "$VENV_DIR/bin/ja" uninstall --yes --purge
    else
      "$VENV_DIR/bin/ja" uninstall --yes
    fi
    return
  fi
  local python
  python="$(pick_python)"
  MANIFEST_PATH="$MANIFEST_PATH" PURGE="$purge" "$python" <<'PY'
import json, os, shutil
from pathlib import Path

manifest_path = Path(os.environ["MANIFEST_PATH"])
if not manifest_path.exists():
    raise SystemExit("No J the Agent install manifest found.")
data = json.loads(manifest_path.read_text())
install_dir = Path(data["install_dir"]).expanduser()
for raw in data.get("symlinks", []):
    link = Path(raw).expanduser()
    if link.is_symlink() or link.is_file():
        link.unlink(missing_ok=True)
manifest_path.unlink(missing_ok=True)
shutil.rmtree(install_dir, ignore_errors=True)
if os.environ.get("PURGE") == "true":
    config_path = Path(data.get("config_path", "")).expanduser()
    state_dir = Path(data.get("state_dir", "")).expanduser()
    if config_path.name:
        shutil.rmtree(config_path.parent, ignore_errors=True)
    if str(state_dir) not in {"", "."}:
        shutil.rmtree(state_dir, ignore_errors=True)
PY
  if [[ "$purge" == "true" ]]; then
    ok "J the Agent, configuration, and owned state were removed."
  else
    ok "J the Agent was removed. Configuration and user data were kept."
  fi
}

greet() {
  printf '%b\n' "${CYAN}${BOLD}
             ███████████
                   ███
       █████       ███
          ██       ███
                   ███
             ██    ███
             ████████

              J THE AGENT
${RESET}        Modular personal & server intelligence"
}

main() {
  if [[ "${1:-}" == "uninstall" || "${1:-}" == "--uninstall" ]]; then
    if [[ "${2:-}" == "--purge" ]]; then
      uninstall_from_manifest true
    else
      uninstall_from_manifest false
    fi
    return
  fi
  if [[ ! -t 0 ]]; then
    fail "Interactive input requires a terminal. Use: bash <(curl -fsSL RAW_INSTALL_URL)"
  fi
  greet
  local choice
  choice="$(arrow_menu "Choose one mode to install (↑/↓, Enter):" \
    "Quick — server assistant" \
    "Companion — personal assistant with memory" \
    "Manager — coming soon" \
    "Exit")"
  case "$choice" in
    0) install_mode "quick" "Quick" ;;
    1) install_mode "companion" "Companion" ;;
    2) warn "Manager is coming soon. Nothing was installed." ;;
    *) info "Installation cancelled." ;;
  esac
}

main "$@"
