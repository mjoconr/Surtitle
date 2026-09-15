#!/usr/bin/env bash
#
# Surtitle installer for macOS (and Linux).
#
# One command that leaves a working installation:
#
#   ./scripts/install.sh              # install everything, including local voice
#   ./scripts/install.sh --update     # bring an existing install up to date
#   ./scripts/install.sh --no-voice   # hosted voice only (smaller, faster)
#   ./scripts/install.sh --check      # verify without changing anything
#
# What it does, and why in this order:
#
#   1. Finds (or installs) uv, which supplies Python too. Nothing is installed
#      system-wide and no administrator rights are needed.
#   2. Creates a virtual environment and installs Surtitle into it.
#   3. Optionally installs the `voice-local` extra (sherpa-onnx) and downloads the
#      speech models into the *app data* directory.
#
# Step 3's split matters for updates. The code lives in the checkout; the models
# live under ~/Library/Application Support/Surtitle/models. Replacing or
# updating the code therefore never re-downloads ~86 MB of models, and removing
# the checkout never destroys them.
#
# The script is idempotent: running it twice is a fast no-op, not a second
# installation. `--update` additionally refreshes the environment.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=''; DIM=''; RED=''; GREEN=''; YELLOW=''; CYAN=''; RESET=''
fi

step() { printf '%s\n' "${CYAN}==>${RESET} ${BOLD}$*${RESET}"; }
info() { printf '%s\n' "    $*"; }
warn() { printf '%s\n' "${YELLOW}warning:${RESET} $*" >&2; }
die()  { printf '%s\n' "${RED}error:${RESET} $*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #
WITH_VOICE=1
DO_MODELS=1
UPDATE=0
CHECK_ONLY=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh [options]

  --no-voice       Do not install the local speech engines or download models.
                   Hosted (Deepgram) voice still works with an API key.
  --no-models      Install the local engine but do not download speech models.
                   Run `surtitle models download` later to fetch them.
  --update         Update an existing installation and re-verify it.
  --check          Report what is installed and what is missing; change nothing.
  -y, --yes        Do not prompt for confirmation.
  -h, --help       Show this message.

Environment:
  SURTITLE_HOME      Data directory (models, settings, database).
  SURTITLE_MODELS_DIR  Model cache, if you want it separate from the data dir.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-voice) WITH_VOICE=0 ;;
    --no-models) DO_MODELS=0 ;;
    --update) UPDATE=1 ;;
    --check) CHECK_ONLY=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# --------------------------------------------------------------------------- #
# Platform
# --------------------------------------------------------------------------- #
OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM="macOS" ;;
  Linux)  PLATFORM="Linux" ;;
  *) die "this installer supports macOS and Linux; on Windows use scripts\\install.ps1" ;;
esac

if [ "$PLATFORM" = "macOS" ]; then
  DATA_DIR_DEFAULT="$HOME/Library/Application Support/Surtitle"
else
  DATA_DIR_DEFAULT="${XDG_DATA_HOME:-$HOME/.local/share}/surtitle"
fi
DATA_DIR="${SURTITLE_HOME:-$DATA_DIR_DEFAULT}"
MODELS_DIR="${SURTITLE_MODELS_DIR:-$DATA_DIR/models}"

printf '\n%s\n' "${BOLD}Surtitle installer — $PLATFORM${RESET}"
info "project: $PROJECT_DIR"
info "data:    $DATA_DIR"

# --------------------------------------------------------------------------- #
# uv: the only prerequisite, and it also supplies Python
# --------------------------------------------------------------------------- #
find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

install_uv() {
  step "Installing uv (Python toolchain, no admin rights needed)"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    die "neither curl nor wget is available, so uv cannot be installed automatically.
Install uv from https://docs.astral.sh/uv/getting-started/installation/ and re-run."
  fi
}

if ! UV="$(find_uv)"; then
  if [ "$CHECK_ONLY" = "1" ]; then
    die "uv is not installed. Run without --check to install it."
  fi
  install_uv
  UV="$(find_uv)" || die "uv was installed but is not on PATH. Open a new terminal and re-run."
fi
info "uv:      $UV ($("$UV" --version 2>/dev/null | head -1))"

# --------------------------------------------------------------------------- #
# Virtual environment
# --------------------------------------------------------------------------- #
VENV="$PROJECT_DIR/.venv"
VENV_PY="$VENV/bin/python"

if [ "$CHECK_ONLY" = "0" ]; then
  step "Preparing the Python environment"
  if [ ! -x "$VENV_PY" ]; then
    "$UV" venv --allow-existing "$VENV" --quiet
    info "created $VENV"
  else
    info "$VENV already exists"
  fi

  SYNC_ARGS=(sync --python "$VENV_PY" --quiet)
  [ "$UPDATE" = "1" ] && SYNC_ARGS+=(--refresh)
  if [ "$WITH_VOICE" = "1" ]; then
    SYNC_ARGS+=(--extra voice-local)
  fi
  if ! "$UV" "${SYNC_ARGS[@]}"; then
    if [ "$WITH_VOICE" = "1" ]; then
      warn "the local voice extra failed to install (no wheel for this platform?)"
      warn "falling back to a hosted-voice-only install"
      "$UV" sync --python "$VENV_PY" --quiet
    else
      die "dependency installation failed. Run 'uv sync' to see the full output."
    fi
  fi
  info "dependencies installed"
fi

# --------------------------------------------------------------------------- #
# Local speech models
# --------------------------------------------------------------------------- #
run_app() {
  if [ -x "$VENV_PY" ]; then
    SURTITLE_HOME="$DATA_DIR" "$VENV_PY" -m surtitle "$@"
  else
    SURTITLE_HOME="$DATA_DIR" "$UV" run --quiet --python "$VENV_PY" surtitle "$@"
  fi
}

if [ "$CHECK_ONLY" = "1" ]; then
  step "Checking the installation"
  if [ -x "$VENV_PY" ]; then
    run_app models list || true
  else
    warn "no virtual environment at $VENV"
  fi
  printf '\n%s\n' "${DIM}Re-run without --check to install.${RESET}"
  exit 0
fi

if [ "$WITH_VOICE" = "1" ] && [ "$DO_MODELS" = "1" ]; then
  step "Local speech models"
  info "cache: $MODELS_DIR (this survives updates and is removed only by you)"
  MODEL_ARGS=(models download)
  [ "$ASSUME_YES" = "1" ] && MODEL_ARGS+=(--yes)
  if [ "$ASSUME_YES" = "0" ] && [ ! -t 0 ]; then
    # No terminal to answer the confirmation prompt; proceed with defaults
    # rather than hanging or failing.
    MODEL_ARGS+=(--yes)
  fi
  if ! run_app "${MODEL_ARGS[@]}"; then
    warn "model download did not complete. Retry later with:"
    warn "  $PROJECT_DIR/scripts/run.sh models download"
  fi
fi

# --------------------------------------------------------------------------- #
# Verify, then say what to do next
# --------------------------------------------------------------------------- #
step "Verifying"
run_app doctor --offline || true

printf '\n%s\n' "${GREEN}${BOLD}Installation complete.${RESET}"
cat <<EOF

Start it with:

    $PROJECT_DIR/scripts/run.sh

Then, in the app:
  • Settings → API keys: add a DeepSeek key (required) and a Deepgram key if you
    want hosted voice.
  • Settings → Voice: set Speech-to-text and Text-to-speech to "local" to use the
    downloaded models — no key, no network.

Useful commands:

    $PROJECT_DIR/scripts/run.sh models list      # what is installed
    $PROJECT_DIR/scripts/run.sh models download  # fetch or repair models
    $PROJECT_DIR/scripts/run.sh doctor           # live check of keys and engines
    $PROJECT_DIR/scripts/install.sh --update     # update this installation

Your data lives in: $DATA_DIR
EOF
