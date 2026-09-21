#!/usr/bin/env bash
#
# Surtitle launcher for macOS and Linux.
#
# Works in two situations:
#
#   1. From a git clone, where it bootstraps the development environment with uv.
#   2. From a release archive, where a self-contained Python and virtual
#      environment are already bundled and nothing needs downloading.
#
# Everything is resolved relative to this script, so it can be run from any
# working directory.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# No arguments means "start the app". The .app bundle this installer creates runs
# this script with none, and forwarding an empty list printed the CLI's help and
# exited instead of starting anything.
if [ "$#" -eq 0 ]; then
  set -- run
fi

# --------------------------------------------------------------------------- #
# Colours, but only when attached to a terminal.
# --------------------------------------------------------------------------- #
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  BOLD=''; DIM=''; RED=''; GREEN=''; YELLOW=''; RESET=''
fi

die() { printf '%s\n' "${RED}error:${RESET} $*" >&2; exit 1; }
info() { printf '%s\n' "${DIM}$*${RESET}"; }

# --------------------------------------------------------------------------- #
# Locate an interpreter: bundled release first, then uv, then system python.
# --------------------------------------------------------------------------- #
# uv's installer updates the shell profile but not the environment of the shell
# that ran it, so a uv installed a moment ago is invisible to `command -v` in the
# very terminal that installed it. Its documented locations are checked too,
# which is what makes "install uv, then run.sh" work as written.
find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  for candidate in "$HOME/.local/bin/uv" "${CARGO_HOME:-$HOME/.cargo}/bin/uv"; do
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

find_bundled_python() {
  for candidate in \
    "$SCRIPT_DIR/venv/bin/python" \
    "$SCRIPT_DIR/venv/Scripts/python.exe" \
    "$SCRIPT_DIR/python/bin/python3" \
    "$SCRIPT_DIR/python/bin/python"
  do
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

# Where a development virtual environment would be, if there is one.
#
# Two layouts have to be covered. In a release archive this script sits at the
# archive root, next to venv/ and python/. In a source checkout it lives in
# scripts/, and .venv belongs at the checkout root - so the parent directory is
# the one to look in. Checking only the launcher's own directory is why an
# existing .venv still reported that no Python environment was found.
find_dev_python() {
  for candidate in \
    "$SCRIPT_DIR/.venv/bin/python" \
    "$SCRIPT_DIR/../.venv/bin/python"
  do
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

DEV_PYTHON="$(find_dev_python || true)"

if BUNDLED_PYTHON="$(find_bundled_python)"; then
  info "Using the bundled Python runtime."
  # A "fat" release archive carries the speech models inside it. Pointing the app
  # at that directory keeps an extracted archive fully offline: the models are
  # found without a download and without touching the user's data directory.
  if [ -d "$SCRIPT_DIR/models" ] && [ -z "${SURTITLE_MODELS_DIR:-}" ]; then
    SURTITLE_MODELS_DIR="$SCRIPT_DIR/models"
    export SURTITLE_MODELS_DIR
  fi
  exec "$BUNDLED_PYTHON" -m surtitle "$@"
fi

# A source checkout that has already installed local voice models: find them so an
# explicit models directory is not required.
if [ -z "${SURTITLE_MODELS_DIR:-}" ] && [ -d "$SCRIPT_DIR/models" ]; then
  SURTITLE_MODELS_DIR="$SCRIPT_DIR/models"
  export SURTITLE_MODELS_DIR
fi

# An existing environment is launched as it is. Syncing here used to happen on
# every start, and it was wrong twice over: `uv sync` without --extra voice-local
# uninstalls that extra even with --inexact (it is in the lock, so --inexact does
# not protect it), so the offline engines had to be installed again after every
# launch from a shortcut; and a warm environment that had drifted from the lock
# was rebuilt, re-downloading Python and every dependency. Dependencies are an
# install/update concern - Setup, the tray's update, or an explicit
# `uv sync` - not something to redo each time the app starts.
if [ -n "$DEV_PYTHON" ]; then
  info "Using the existing .venv."
  exec "$DEV_PYTHON" -m surtitle "$@"
fi

# Nothing installed yet: bootstrap once. This is the only path that touches uv,
# and the only one where silence would look like a hang.
if UV="$(find_uv)"; then
  info "First run: fetching Python and dependencies."
  info "This takes a few minutes, and only happens once."
  if ! "$UV" sync --inexact; then
    die "uv sync failed. Run 'uv sync' yourself to see the full output."
  fi
  # The engines that recognise and speak on this machine are an optional extra, and
  # this bootstrap deliberately does not install 30 MB nobody asked for. Saying so
  # here is the difference between a choice and a dead end: picking a local engine
  # in Settings otherwise fails with an import error and no sign of what to do,
  # which is how it was reported from a fresh macOS install.
  info ""
  info "Optional: the offline speech engines (no key, nothing leaves this machine)"
  info "are not installed. In Settings ▸ Voice, choose local and press Install — or run"
  info "    uv sync --extra voice-local"
  exec "$UV" run --no-sync --quiet surtitle "$@"
fi

printf '%s\n' "${RED}Surtitle cannot start: no Python environment found.${RESET}" >&2
cat >&2 <<'EOF'

This looks like a source checkout with nothing installed yet.

Either install uv (recommended — it handles Python and dependencies for you):

    curl -LsSf https://astral.sh/uv/install.sh | sh

uv unpacks into ~/.local/bin and only joins the PATH of shells started
afterwards. This launcher looks there too, so either open a new terminal and run
this again, or add it to this one:

    export PATH="$HOME/.local/bin:$PATH"
    ./scripts/run.sh

Or create a virtual environment yourself:

    python3 -m venv .venv
    .venv/bin/pip install -e .
    ./scripts/run.sh
EOF
exit 1
