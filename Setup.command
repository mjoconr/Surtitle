#!/usr/bin/env bash
#
# Surtitle setup for macOS - double-click this file.
#
# This is the one-step entry point for a source checkout. It installs uv and
# Python, the dependencies, optionally the offline speech engines and their
# models, adds Surtitle to ~/Applications, and asks whether Surtitle should start
# when you sign in.
#
# Arguments are forwarded to scripts/install.sh, so the flags still work:
#
#     Setup.command --no-voice    hosted voice only (smaller, faster)
#     Setup.command --no-models   install the engines but skip the download
#     Setup.command --update      update an existing installation
#     Setup.command --check       report what is installed; change nothing
#
# Finder runs a .command file in Terminal. That is why this exists at all: it is
# the thing a user can double-click, without a terminal and without knowing a
# command. The launchers are untouched - scripts/run.sh still starts the app and
# forwards any command.

set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

"$HERE/scripts/install.sh" "$@"
STATUS=$?

printf '\n'
if [ "$STATUS" -ne 0 ]; then
  printf 'Setup did not finish successfully (exit %s). The messages above say why.\n' "$STATUS"
fi
printf 'Press Return to close this window...'
# A double-click always has a terminal; a piped or automated run may not, and a
# failed read there must not change the exit status.
read -r _ || true

exit "$STATUS"
