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
# Usually reached by double-clicking Setup.command at the top of the checkout,
# which needs no terminal and no command.
#
# What it does, and why in this order:
#
#   1. Finds (or installs) uv, which supplies Python too. Nothing is installed
#      system-wide and no administrator rights are needed.
#   2. Creates a virtual environment and installs Surtitle into it.
#   3. Optionally installs the `voice-local` extra (sherpa-onnx) and downloads the
#      speech models into the *app data* directory.
#   4. On macOS, puts Surtitle in ~/Applications so it can be launched from
#      Finder, the Dock or Spotlight instead of a terminal.
#   5. Asks whether Surtitle should start when you sign in, and writes or removes
#      a per-user LaunchAgent accordingly. Ask once, never silently: --startup and
#      --no-startup answer it for an automated run.
#
# Step 3's split matters for updates. The code lives in the checkout; the models
# live under ~/Library/Application Support/Surtitle/models. Replacing or
# updating the code therefore never re-downloads the speech models, and removing
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
# Empty means "ask", the same as it does on Windows.
STARTUP=""

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh [options]

  --no-voice       Do not install the local speech engines or download models.
                   Hosted (Deepgram) voice still works with an API key.
  --no-models      Install the local engine but do not download speech models.
                   Run `surtitle models download` later to fetch them.
  --update         Update an existing installation and re-verify it.
  --check          Report what is installed and what is missing; change nothing.
  --startup        Start Surtitle when you sign in (a LaunchAgent on macOS).
  --no-startup     Do not start Surtitle at sign-in; remove an existing entry.
  -y, --yes        Do not prompt for confirmation. Sign-in defaults to off.
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
    --startup) STARTUP=1 ;;
    --no-startup) STARTUP=0 ;;
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

# The macOS equivalents of the Windows Start Menu entry and Startup folder:
# a launcher in ~/Applications and a per-user LaunchAgent. Both stay inside the
# home directory, so neither needs administrator rights.
APP_BUNDLE="$HOME/Applications/Surtitle.app"
AGENT_LABEL="com.surtitle.launcher"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"

printf '\n%s\n' "${BOLD}Surtitle installer — $PLATFORM${RESET}"
info "project: $PROJECT_DIR"
info "data:    $DATA_DIR"

# A release archive already carries a complete runtime. Running this inside one
# would build a second environment beside it — and the launcher prefers venv/,
# so it would quietly change which interpreter runs. There is nothing to install.
if [ -f "$PROJECT_DIR/BUILD-INFO.json" ] &&
  { [ -x "$PROJECT_DIR/python/bin/python3" ] || [ -x "$PROJECT_DIR/venv/bin/python" ]; }; then
  printf '\n%s\n' "${YELLOW}This is a Surtitle release archive, and it is already installed.${RESET}"
  cat <<EOF

Start it with ./scripts/run.sh.

To update it, download the newest release and replace this folder. Your settings,
database and speech models live in the app data directory and are kept.
EOF
  exit 0
fi

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
  if [ "$PLATFORM" = "macOS" ]; then
    if [ -d "$APP_BUNDLE" ]; then info "launcher:        $APP_BUNDLE"; else info "launcher:        not created"; fi
    if [ -f "$AGENT_PLIST" ]; then info "sign-in agent:   installed"; else info "sign-in agent:   not installed"; fi
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
# Launcher and sign-in (macOS)
# --------------------------------------------------------------------------- #
ask_yes_no() {
  local question="$1" default="${2:-n}" reply suffix
  if [ "$default" = "y" ]; then suffix="[Y/n]"; else suffix="[y/N]"; fi
  printf '    %s %s ' "$question" "$suffix"
  read -r reply || reply=""
  [ -n "$reply" ] || reply="$default"
  case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

create_app_bundle() {
  local version png iconset size
  version="$("$VENV_PY" -c 'import surtitle; print(surtitle.__version__)' 2>/dev/null || true)"
  [ -n "$version" ] || version="0.0.0"

  mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
  cat > "$APP_BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Surtitle</string>
    <key>CFBundleDisplayName</key><string>Surtitle</string>
    <key>CFBundleIdentifier</key><string>$AGENT_LABEL</string>
    <key>CFBundleExecutable</key><string>Surtitle</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$version</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

  # The launcher runs this checkout's own run.sh, so opening the app from Finder
  # and running ./scripts/run.sh in a terminal are the same code path.
  cat > "$APP_BUNDLE/Contents/MacOS/Surtitle" <<LAUNCHER
#!/bin/sh
exec "$PROJECT_DIR/scripts/run.sh" "\$@"
LAUNCHER
  chmod +x "$APP_BUNDLE/Contents/MacOS/Surtitle"

  # The application's own mark, so it is recognisable in the Dock. Best effort:
  # sips and iconutil ship with macOS, but a missing icon is a cosmetic loss, not
  # a failed install.
  png="$PROJECT_DIR/src/surtitle/web/surtitle-icon.png"
  if [ -f "$png" ] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
    iconset="$(mktemp -d)/Surtitle.iconset"
    mkdir -p "$iconset"
    for size in 16 32 64 128 256 512; do
      sips -z "$size" "$size" "$png" --out "$iconset/icon_${size}x${size}.png" >/dev/null 2>&1 || true
      sips -z "$((size * 2))" "$((size * 2))" "$png" \
        --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
    done
    iconutil -c icns "$iconset" -o "$APP_BUNDLE/Contents/Resources/AppIcon.icns" >/dev/null 2>&1 || true
    rm -rf "$(dirname "$iconset")"
  fi
  info "created $APP_BUNDLE"
}

write_agent() {
  mkdir -p "$(dirname "$AGENT_PLIST")"
  cat > "$AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$AGENT_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/scripts/run.sh</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
</dict>
</plist>
PLIST
}

load_agent() {
  # bootstrap is the modern spelling; load -w is the fallback for older systems.
  # Either can refuse in a restricted session without meaning the install is
  # broken: the file is on disk, and the next sign-in reads it.
  launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST" 2>/dev/null \
    || launchctl load -w "$AGENT_PLIST" 2>/dev/null \
    || warn "the sign-in agent could not be loaded now; it will run at your next sign-in"
}

unload_agent() {
  launchctl bootout "gui/$(id -u)/$AGENT_LABEL" 2>/dev/null \
    || launchctl unload -w "$AGENT_PLIST" 2>/dev/null \
    || true
}

if [ "$PLATFORM" = "macOS" ]; then
  step "Launcher and sign-in"
  create_app_bundle || warn "could not create the application bundle in ~/Applications"

  # -y means "take the defaults", and adding something to sign-in is not a
  # default worth taking silently, so it answers no. No terminal to ask on means
  # the same.
  if [ "$STARTUP" = "1" ]; then
    startup=1
  elif [ "$STARTUP" = "0" ] || [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then
    startup=0
  elif ask_yes_no "Start Surtitle when you sign in?" n; then
    startup=1
  else
    startup=0
  fi

  if [ "$startup" = "1" ]; then
    write_agent
    load_agent
    info "Surtitle will start when you sign in ($AGENT_PLIST)"
    info "delete that file to turn it off"
  elif [ -f "$AGENT_PLIST" ]; then
    unload_agent
    rm -f "$AGENT_PLIST"
    info "removed the sign-in agent"
  else
    info "Surtitle will not start automatically"
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

EOF

if [ "$PLATFORM" = "macOS" ]; then
  cat <<EOF
or open Surtitle from ~/Applications — Finder, Spotlight or the Dock all reach
it, and it runs the same launcher (there is no taskbar icon on macOS).

EOF
fi

cat <<EOF
To run this installer again without a terminal, double-click Setup.command at the
top of the checkout. It takes the same flags:

    --no-voice     hosted voice only
    --update       update this installation
    --startup      start Surtitle when you sign in (or --no-startup)

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
