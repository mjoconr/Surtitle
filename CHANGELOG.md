# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **The project is now Surtitle.** The previous working name has been removed
  from the package, CLI, environment variables, configuration paths, app data
  directory, and release artifacts.

  | Previous | Current |
  | --- | --- |
  | `src/agenticvoice/` | `src/surtitle/` |
  | `agenticvoice` (CLI) | `surtitle` |
  | `AGENTICVOICE_*` | `SURTITLE_*` |
  | `.agenticvoice.json` (per-project config) | `.surtitle.json` |
  | `.agenticvoice/` (per-project state) | `.surtitle/` |
  | `…/Application Support/AgenticVoice` (macOS app data) | `…/Application Support/Surtitle` |
  | `%LOCALAPPDATA%\AgenticVoice` (Windows app data) | `%LOCALAPPDATA%\Surtitle` |
  | `agenticvoice.db`, `agenticvoice.log` | `surtitle.db`, `surtitle.log` |
  | `agenticvoice-<version>-<platform>-<arch>` (archive) | `surtitle-<version>-<platform>-<arch>` |

### Added

- Local, offline speech recognition and synthesis, with models that never leave
  the machine. Each direction — recognition and synthesis — independently
  chooses a hosted or local engine. See [`docs/VOICE.md`](docs/VOICE.md).
- `surtitle models list | download | verify` to manage the local speech models.
- `scripts/install.sh` and `scripts/install.ps1` to install Python,
  dependencies, and optionally the offline voice engines in one step, each with
  an update path (`--update` on macOS/Linux, `-Update` on Windows).
- `pythonpath` in the pytest configuration so tests can share helpers through
  `tests.*` imports.

### Migration — this rename is breaking

An existing installation will not pick up its settings, credentials, or history
until the data directory is moved. To carry them across:

```bash
# macOS
cd ~/Library/Application\ Support
mv AgenticVoice Surtitle
mv Surtitle/agenticvoice.db  Surtitle/surtitle.db
mv Surtitle/agenticvoice.log Surtitle/surtitle.log
```

```powershell
# Windows (PowerShell)
Set-Location $env:LOCALAPPDATA
Move-Item AgenticVoice Surtitle
Move-Item Surtitle\agenticvoice.db  Surtitle\surtitle.db
Move-Item Surtitle\agenticvoice.log Surtitle\surtitle.log
```

Then:

- Rename any `AGENTICVOICE_*` variables in your `.env` to `SURTITLE_*`. Run
  `surtitle init` if you would rather scaffold a fresh `.env`.
- Rename per-project configuration from `.agenticvoice.json` to
  `.surtitle.json`; a project picks its settings back up once renamed, and is
  otherwise treated as new.
- Rename per-project state directories from `.agenticvoice/` to `.surtitle/` to
  keep the isolated virtual environment and remembered package approvals.
  Leaving them in place is harmless — they are simply rebuilt on demand.
- Speech models are **not** carried over by the `.env` change alone. If you skip
  the data-directory move, run `surtitle models download` to fetch them again.
