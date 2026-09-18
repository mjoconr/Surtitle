# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-18

### Added

- **Choose the project folder with a native dialog.** The new-project dialog has
  a **Browse…** button wherever the machine can show a chooser — the Win32 folder
  picker on Windows, `choose folder` on macOS, zenity or kdialog on Linux — and
  the chosen folder pre-fills the project name. The browser cannot supply a
  usable path (a file handle is not a location the agent could be confined to and
  read), so `POST /api/dialog/folder` opens the dialog on the machine running the
  server. That endpoint is loopback-only — a remote caller must not be able to put
  a modal window on someone's desktop — and a machine with no desktop answers 501
  instead of hanging on a window nobody can see.
- **Update from the tray.** A git checkout gets two rows — **Update to the latest
  release…** and **Update to current main…** — each confirmed before anything
  moves, then run by the server (`POST /api/update`, loopback only) so the menu
  can report progress. The update is a fast-forward or a tag checkout, never a
  rebase: local work stops it rather than being overwritten, and `uv sync
  --inexact` refreshes the dependencies afterwards. The running process keeps the
  code it started with, so the result says to restart. A release archive cannot
  replace its own running files, so it is offered **Get the latest release…**,
  which opens the download page instead of failing. `surtitle update
  [--target release|main]` with `--check` does the same from a terminal.
- **Install the offline speech engines from the tray.** The right-click menu has
  an **Install local voice…** item whenever offline speech is incomplete. It
  asks before a large download, then hands the work to the server
  (`POST /api/voice/install`, loopback only) so it continues whichever icon
  started it. While it runs the menu row becomes progress, and it reads "Local
  voice is installed" once there is nothing left to do. `surtitle voice install`
  and `surtitle voice status` do the same from a terminal, and exit non-zero when
  offline speech is not ready.
- **One double-clickable setup file per platform.** `Setup.bat` on Windows and
  `Setup.command` on macOS install everything a source checkout needs: uv and
  Python, the dependencies, optionally the offline speech engines and their
  models, a launcher entry, and the sign-in question. Neither needs a terminal, a
  typed command, or administrator rights — the Windows one passes
  `-ExecutionPolicy Bypass` for its own invocation rather than asking anyone to
  change a machine-wide setting.
- **A launcher entry and an optional sign-in entry**, both per-user. On Windows
  the installer adds a Start Menu shortcut (already there) and now asks whether
  Surtitle should start when you sign in, creating or removing a Start-folder
  shortcut; the sign-in launch starts minimized with `--no-browser`. On macOS it
  writes `Surtitle.app` into `~/Applications` with the application icon, and a
  per-user LaunchAgent for sign-in. `-Startup`/`-NoStartup` and
  `--startup`/`--no-startup` answer without a prompt; `-Yes`/`--yes` means "take
  the defaults", which is not to add anything to sign-in silently.

### Fixed

- **An installation is no longer told the wrong reason it cannot update.** A
  release archive, an unpacked source ZIP and a git clone with git missing all
  used to report "not installed from a git checkout". They are now three separate
  answers: a clone pulls, a clone without git is told git is not installed, and
  something with no history at all is pointed at the download page. `surtitle
  update --check` says which applies.
- **The installers refuse to run inside a release archive.** `scripts/install.ps1`
  and `scripts/install.sh` ship inside the archive, and running one there built a
  second environment beside the bundled runtime — and since the launcher prefers
  `venv\`, it quietly changed which interpreter ran. They now say the archive is
  already installed and exit without touching anything.
- The download size quoted before fetching speech models was stale by more than
  four times: the registry's models are about 395 MB, not the ~86 MB the
  installers and prompts claimed. The app now reports the real figure from the
  model registry, and the scripts and docs no longer name a number at all.
- A launcher started with **no arguments now starts the app** instead of printing
  help and exiting. `run.bat`, `run.ps1` and `run.sh` forwarded an empty argument
  list to the CLI, so the double-click the documentation recommends left a window
  that flashed and vanished. Passing a command is unchanged: `run.bat doctor`,
  `run.bat models list`, `run.bat run --port 9000` all behave exactly as before.

## [0.1.0] - 2026-09-17

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

- **Document conversion no longer requires LibreOffice.** A built-in engine reads
  Word, Excel, PowerPoint, OpenDocument and PDF files and writes PDF, text, CSV,
  HTML and XLSX with nothing installed. It carries content rather than layout.
  LibreOffice is still used when it is present — for the formats the built-in
  engine cannot write (`docx`, `odt`, `rtf`, `ods`, `odp`, `pptx`, `png`, `jpg`)
  and as a fallback that preserves appearance. The new `backend` argument
  (`auto`, `builtin`, `libreoffice`) selects one, and the result reports which
  ran in `data.backend`.
- `read_file` reads Office documents directly, extracting headings, paragraphs,
  lists and tables, so a `.docx` or `.xlsx` no longer has to be converted before
  it can be read.

### Added

- A **taskbar notification icon on Windows**, with a menu that reports live
  status, shows what the run has cost, and stops the server gracefully. It is on
  by default for `surtitle run` (`--no-tray` turns it off, `--tray` forces it),
  and `surtitle tray` attaches one to a server started any other way. The icon is
  built directly on `Shell_NotifyIcon` through `ctypes`, so it adds no dependency
  to the release archive.

  `Status…` reports the address, uptime, model, voice engines, which credentials
  are set, and what is stored; `Usage…` reports turns, tool calls, tokens in and
  out, cache hits, and an estimated cost. The estimate prices the peak and
  off-peak rates separately and applies the cache-hit discount, and shows no
  money figure at all for a model with no published rate.
- `GET /api/status`, the payload behind both the icon and the new
  `surtitle status` command (`--json` for scripting).
- `POST /api/shutdown`, which stops the server gracefully and refuses any request
  that does not come from this machine.
- `SURTITLE_PRICE_INPUT`, `SURTITLE_PRICE_CACHED_INPUT` and
  `SURTITLE_PRICE_OUTPUT` to override the published per-million-token rates
  without waiting for a release.
- An application icon — a waveform on the brand blue — used for the tray, the
  browser favicon and the Start Menu entry, regenerable with
  `uv run python scripts/make_icon.py`.
- `scripts/install.ps1` adds a Surtitle entry to the Start Menu for the current
  user (`-NoShortcut` skips it). It is a per-user shortcut, so it still needs no
  administrator rights.
- Local, offline speech recognition and synthesis, with models that never leave
  the machine. Each direction — recognition and synthesis — independently
  chooses a hosted or local engine. See [`docs/VOICE.md`](docs/VOICE.md).
- `surtitle models list | download | verify` to manage the local speech models.
- `scripts/install.sh` and `scripts/install.ps1` to install Python,
  dependencies, and optionally the offline voice engines in one step, each with
  an update path (`--update` on macOS/Linux, `-Update` on Windows).
- `pythonpath` in the pytest configuration so tests can share helpers through
  `tests.*` imports.

### Fixed

- `run.bat`, `run.ps1` and `run.sh` now find a `uv` that is installed but not on
  the current `PATH`. uv's installer updates the *user* `PATH`, which does not
  affect the shell that ran it, so the documented "install uv, then run the
  launcher" sequence failed in the same window — with `run.bat` then advising you
  to install the uv you had just installed. All three launchers now also check
  uv's documented install location (`%USERPROFILE%\.local\bin\uv.exe`,
  `~/.local/bin/uv`), which is what `install.ps1` had always done.
- The launchers find an existing `.venv` at the **checkout root**. They live in
  `scripts/` in a checkout but at the archive root in a release, and they only
  ever looked in their own directory — so `scripts\.venv` was checked and the
  real `.venv` was not. The manual path in the README (`python -m venv .venv`,
  `pip install -e .`, `run.bat`) reported "no Python environment was found" with
  a working environment sitting right there.
- A first run from a source checkout is no longer silent. It downloads the whole
  dependency set, and `uv sync --quiet` gave no sign of progress for several
  minutes — indistinguishable from the hang the user had just escaped. The sync
  is verbose until a `.venv` exists, and quiet afterwards.
- The "no Python environment was found" message now says which of the two
  situations you are in and what to type. It also names `install.ps1` as the
  one-step option, and `docs/WINDOWS.md` covers the `RemoteSigned` case where a
  downloaded script still needs `Unblock-File`.

- `scripts/run.sh` and `scripts/run.ps1` no longer remove the optional
  `voice-local` extra. A plain `uv sync` prunes anything the lock does not name,
  and `uv run` synced a second time on every launch, so installing the offline
  engines appeared to work and was undone by the next run.
- The `voice-local` extra now installs `sherpa-onnx-core` next to the bindings.
  That wheel marks its dependency as dynamic, which hides the edge from the
  resolver, so `sherpa-onnx` was installed without the native runtime it links
  against and failed to import.
- Release archives run on the machine that receives them. The bundled virtual
  environment linked `venv/bin/python` to an absolute path inside the build
  directory, so an extracted archive could only start where it was built — and
  Python 3.12 and later refused to extract it at all. Archive verification now
  asserts that the interpreter resolves inside the extracted tree, which is the
  check that would have caught this.
- Two tests that passed only on a machine with the right tools installed: the
  document-conversion tests needed a real LibreOffice, and the search test
  assumed ripgrep was on `PATH`.
- Windows archive verification tested the wrong interpreter. It looked for the
  POSIX venv layout and the Windows runtime layout but not `venv\Scripts\python.exe`,
  then silently fell back to the bundled runtime — which cannot import the
  application, so the smoke test failed while proving nothing about the archive.
- Path confinement on Windows refused paths it should have allowed.
  `Path.resolve()` returns an extended-length (`\\?\`) path for some inputs, and
  that prefix is a different first component, so a contained path looked like an
  escape. Long project paths hit this too.
- The Windows archive could not run anywhere but the build machine. A Windows
  virtual environment's `Scripts\python.exe` is a launcher that reads an absolute
  base path from `pyvenv.cfg`, so an extracted archive failed with
  `No Python at '...'` — and verification never noticed, because it extracted on
  the machine that built it. Windows archives now install into the bundled
  runtime, which is self-contained and moves with the tree.
- Release archives are pinned to `uv.lock`. The build looked for a
  `requirements-release.txt` that never existed and fell back to installing the
  project by path, so every archive was a fresh resolution — the last one carried
  `uvicorn` 0.53.0 while the lock pins 0.52.4, a version nothing had tested. The
  lock is now exported and installed from.
- The bundled wheelhouse is actually built. It asked `uv` for `pip download`,
  which is not a subcommand uv has, so on every platform the step failed and was
  reported as skipped, leaving archives without the offline repair their
  documentation promised. It now uses the bundled interpreter's own pip. That
  adds roughly 44 MB to a macOS archive; `--skip-wheelhouse` still produces the
  smaller build.
- A Windows release can be cut. The release workflow's smoke test asserted
  `venv\Scripts\python.exe`, which a Windows archive deliberately does not have —
  dependencies go into the bundled runtime, because a Windows venv cannot be
  relocated — so every `v*` tag would have failed after a full build. Archive
  verification now lives in one place, `scripts/build_release.py`, which decides
  the expected layout from the archive's own `BUILD-INFO.json`, and the new
  `--verify-only` re-runs that same verifier over `dist/` or over a download the
  user names. CI's completeness check had been passing for the wrong reason: it
  matched the substring `venv` inside the bundled runtime's `Lib\venv`.

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

[Unreleased]: https://github.com/mjoconr/Surtitle/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.2.0
[0.1.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.1.0
