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

# Not a release archive: bootstrap from source.
if command -v uv >/dev/null 2>&1; then
  info "Syncing dependencies with uv…"
  if ! uv sync --quiet; then
    die "uv sync failed. Run 'uv sync' yourself to see the full output."
  fi
  exec uv run --quiet surtitle "$@"
fi

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  # An existing development venv is enough to run.
  info "Using the existing .venv."
  exec "$SCRIPT_DIR/.venv/bin/python" -m surtitle "$@"
fi

printf '%s\n' "${RED}Surtitle cannot start: no Python environment found.${RESET}" >&2
cat >&2 <<'EOF'

This looks like a source checkout with nothing installed yet.

Either install uv (recommended — it handles Python and dependencies for you):

    curl -LsSf https://astral.sh/uv/install.sh | sh
    ./scripts/run.sh

Or create a virtual environment yourself:

    python3 -m venv .venv
    .venv/bin/pip install -e .
    ./scripts/run.sh
EOF
exit 1
