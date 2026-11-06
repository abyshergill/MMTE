#!/usr/bin/env bash
# One-step launcher for macOS and Linux.
#
#   ./run.sh              set everything up if needed, then start the app
#   ./run.sh --gguf       also install the optional local-GGUF model runtime
#   ./run.sh --reinstall  delete .venv and build it again from scratch
#   ./run.sh --setup-only prepare .venv but do not start the app
#
# It finds Python 3.10+, creates .venv next to this script, installs requirements.txt
# (again only when that file changes) and runs main.py. Nothing outside this folder is touched.
set -euo pipefail

cd "$(dirname "$0")"
APP_DIR="$(pwd)"
VENV="$APP_DIR/.venv"
VPY="$VENV/bin/python"
STAMP="$VENV/.mmte-requirements"

WITH_GGUF=0
REINSTALL=0
SETUP_ONLY=0
APP_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --gguf) WITH_GGUF=1 ;;
    --reinstall) REINSTALL=1 ;;
    --setup-only) SETUP_ONLY=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) APP_ARGS+=("$arg") ;;
  esac
done

say()  { printf '\033[1;36m[MMTE]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[MMTE]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[MMTE]\033[0m %s\n' "$*" >&2; exit 1; }

OS="$(uname -s)"

python_ok() {  # $1 = interpreter; succeeds for Python >= 3.10 with the venv module
  "$1" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
  local candidate
  for candidate in python3 python3.13 python3.12 python3.11 python3.10 python3.14 python; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  # Common install locations that may not be on PATH (python.org / Homebrew on macOS).
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
                   /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    if [ -x "$candidate" ] && python_ok "$candidate"; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

install_hint() {
  if [ "$OS" = "Darwin" ]; then
    warn "Install Python 3.12 from https://www.python.org/downloads/macos/"
    warn "or with Homebrew:  brew install python@3.12"
  elif command -v apt-get >/dev/null 2>&1; then
    warn "Install it with:  sudo apt-get install -y python3 python3-venv python3-pip"
  elif command -v dnf >/dev/null 2>&1; then
    warn "Install it with:  sudo dnf install -y python3 python3-pip"
  elif command -v pacman >/dev/null 2>&1; then
    warn "Install it with:  sudo pacman -S python python-pip"
  elif command -v zypper >/dev/null 2>&1; then
    warn "Install it with:  sudo zypper install python3 python3-pip"
  fi
}

checksum() {
  if command -v sha256sum >/dev/null 2>&1; then
    cat "$@" 2>/dev/null | sha256sum | cut -d' ' -f1
  else
    cat "$@" 2>/dev/null | shasum -a 256 | cut -d' ' -f1
  fi
}

# --- 1. virtual environment -------------------------------------------------------------
if [ "$REINSTALL" = 1 ] && [ -d "$VENV" ]; then
  say "Removing the old virtual environment ..."
  rm -rf "$VENV"
fi

# A venv whose interpreter no longer runs (e.g. Python was upgraded) is rebuilt.
if [ -d "$VENV" ] && ! "$VPY" -c 'import sys' >/dev/null 2>&1; then
  warn "The existing .venv is broken; rebuilding it."
  rm -rf "$VENV"
fi

if [ ! -x "$VPY" ]; then
  PY="$(find_python)" || { warn "Python 3.10 or newer was not found."; install_hint; exit 1; }
  say "Creating virtual environment with $("$PY" --version 2>&1) ..."
  if ! "$PY" -m venv "$VENV"; then
    rm -rf "$VENV"
    warn "Could not create the virtual environment."
    if command -v apt-get >/dev/null 2>&1; then
      warn "On Debian/Ubuntu install the venv module:  sudo apt-get install -y python3-venv"
    fi
    exit 1
  fi
fi

# --- 2. dependencies (only when the requirement files changed) -----------------------------
REQ_FILES=("$APP_DIR/requirements.txt")
[ "$WITH_GGUF" = 1 ] && REQ_FILES+=("$APP_DIR/requirements-gguf.txt")
WANT="$(checksum "${REQ_FILES[@]}")-gguf$WITH_GGUF"
HAVE="$(cat "$STAMP" 2>/dev/null || true)"
# Keep the GGUF runtime installed once it has been added, even on a plain ./run.sh.
if [ "$WITH_GGUF" = 0 ] && [[ "$HAVE" == *-gguf1 ]]; then
  WITH_GGUF=1
  REQ_FILES+=("$APP_DIR/requirements-gguf.txt")
  WANT="$(checksum "${REQ_FILES[@]}")-gguf1"
fi

if [ "$WANT" != "$HAVE" ]; then
  say "Installing dependencies (first run or requirements changed) ..."
  "$VPY" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
  for req in "${REQ_FILES[@]}"; do
    say "  pip install -r $(basename "$req")"
    "$VPY" -m pip install --disable-pip-version-check -r "$req" \
      || die "Installing $(basename "$req") failed. Check your internet connection and run ./run.sh again."
  done
  echo "$WANT" > "$STAMP"
  say "Dependencies are ready."
fi

# --- 3. Linux: Qt needs a few system libraries ------------------------------------------
if [ "$OS" = "Linux" ] && [ -z "${QT_QPA_PLATFORM:-}" ] && command -v ldconfig >/dev/null 2>&1; then
  if ! ldconfig -p 2>/dev/null | grep -q 'libxcb-cursor\.so\.0'; then
    warn "Qt 6 needs the xcb-cursor library to open windows on X11/XWayland."
    if command -v apt-get >/dev/null 2>&1; then
      warn "  sudo apt-get install -y libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-keysyms1"
    elif command -v dnf >/dev/null 2>&1; then
      warn "  sudo dnf install -y xcb-util-cursor libxkbcommon-x11"
    elif command -v pacman >/dev/null 2>&1; then
      warn "  sudo pacman -S xcb-util-cursor libxkbcommon-x11"
    fi
  fi
fi

[ "$SETUP_ONLY" = 1 ] && { say "Setup complete. Start the app with ./run.sh"; exit 0; }

# --- 4. launch -----------------------------------------------------------------------------
say "Starting Modern Multi Terminal Emulator ..."
exec "$VPY" "$APP_DIR/main.py" ${APP_ARGS[@]+"${APP_ARGS[@]}"}
