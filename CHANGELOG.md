# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.2] - 2026-09-19

### Fixed

- **In-place updates on Windows did nothing.** The tray offered the release, the
  download and checksum succeeded, Surtitle closed — and it came back on the old
  version. `run.bat` starts the server with the installation folder as its working
  directory, and the updater inherited that directory, so PowerShell was running
  *inside* the folder it was about to rename. Windows refuses to rename a directory
  that any process has as its current directory, so the first `Move-Item` failed
  every time, the script threw, and nothing was swapped. The updater now runs from
  the app's data folder, and both scripts change directory there before touching
  anything. The moves also retry for a directory that is briefly still held, while
  a source that does not exist fails immediately rather than slowly.
- **A failed update is no longer silent, and no longer leaves the app down.** The
  swap happens after Surtitle has exited, so its outcome can only be reported in a
  file: every step is now written to
  `%LOCALAPPDATA%\Surtitle\updates\apply-update.log` with the result in
  `last-update.txt` beside it. The tray shows a failure as its own row naming the
  reason, selecting it explains what to do and where the log is, the Status dialog
  and `surtitle status` carry it, and retrying says what went wrong last time
  rather than repeating a blind attempt. If the swap fails the old installation is
  put back *and started again* — previously a failure left the user with no running
  app, and a leftover `.old` folder that could not be removed aborted the script
  before it relaunched anything.

  The tests missed this because they run the POSIX swap script, which allows that
  rename, from outside the directory it moves. There is now a test that asserts the
  updater's working directory is outside the installation, and the whole reporting
  path is covered.

## [0.5.1] - 2026-09-19

### Fixed

- **The folder picker leads with the folder you are browsing.** At `C:\` it listed
  the other drives first, and on a machine with a lot of mapped drives that was the
  entire visible panel — the folders inside `C:\` sat below the fold, and the one
  row on screen was the hidden `$Recycle.Bin`. The folder's own entries now come
  first, the other volumes follow under an "Other drives" label, the level reports
  how many folders it holds, and a level whose folders are all hidden says so
  instead of looking empty. The Hidden toggle shows that it is on, the up button is
  disabled at a filesystem root, and each level starts scrolled to its first entry.
- **Two 0.5.0 tests that misbehaved on Windows.** The Windows chooser-refusal test
  called the dialog entry point on Windows, where it is not the refusal branch — it
  opens a real modal dialog with nobody to close it, and both `windows-latest` CI
  jobs sat in "Run the test suite" for 43 minutes. It skips there now, as do the
  symlink tests on a platform that cannot create symlinks without elevation, and a
  POSIX-only file-name test.

## [0.5.0] - 2026-09-19

### Added

- **The project folder is chosen from a picker that works everywhere.** The native
  folder chooser could not be seen on Windows: it ran in-process on a worker thread
  of a detached server, so its window had no main thread and no claim to the
  foreground, and clicking Browse appeared to do nothing at all. The server now
  answers the question itself — `GET /api/dialog/browse` lists one directory level
  with the breadcrumb chain leading to it, `POST` creates a child folder — so
  **Browse…** is a JSON round trip that works from any browser on any machine,
  including a remote browser and a headless host the native chooser can never serve.
  The Windows chooser is kept as **System…** and now runs in its own process, with
  COM, per-monitor-v2 DPI awareness and a synthesized Alt press to take the
  foreground. Both halves are behind one capability the UI reads from `/api/health`,
  so the native button appears only where it could work. Listing is directories
  only, paths must be fully qualified, and both endpoints are loopback only.
- **A project can be deleted, and its conversations with it, without touching your
  files.** Project rows gained rename and delete actions. `DELETE /api/projects/{id}`
  drops the project's live conversations before forgetting it, and reports how many
  conversations went and which folder it left alone; the confirm dialog says the
  same thing before anything happens. A conversation can also be deleted straight
  from the live list, rather than only after archiving it.
- **Portable git and svn, so the agent has history on a machine with neither.** The
  tray's **Install git and svn…**, `surtitle tools install`, or the app downloads
  MinGit and Apache Subversion into `%LOCALAPPDATA%\Surtitle\tools` — no installer,
  no administrator rights, nothing added to your own `PATH`. Each archive is
  verified against a pinned SHA-256 and the unpacked binary must then run and report
  the version it should, because a corrupt download is common and a tree that
  unpacks but does not execute is the failure worth catching. Windows only,
  deliberately: elsewhere Surtitle uses the `git` and `svn` on your `PATH`, and says
  so instead of installing a second copy your system cannot see. `surtitle tools
  status`, `surtitle doctor` and `GET/POST /api/tools/vcs` report and drive the same
  thing; the install endpoint is loopback only.
- **The agent is told to use version control, and to ask before saving with it.**
  When a piece of work is done — the idea mostly works or is actually finished — the
  agent asks, in one short question, whether to add, commit and push and how
  detailed the commit message should be: one line, a summary, or detailed. It never
  commits, tags or pushes unasked, and an earlier yes does not cover later work. A
  session also reports which git and svn are installed and what the project's
  working copy currently is.
- **`vcs_status`, `vcs_guide` and `vcs_commit`.** `vcs_status` reports the working
  copy — system, branch or revision, uncommitted work, ahead/behind, remote.
  `vcs_guide` is the correct usage for git and svn: the model of each system, the
  commands that matter, how to undo at each level of destruction, and what is never
  committed. It is a tool result rather than prompt text, because it is long and only
  needed once the agent is about to touch history. `vcs_commit` is the one mutating
  step, behind an approval: it refuses an empty change rather than leaving an empty
  commit, refuses a body when you asked for one line, and keeps `.surtitle/` out of
  the commit — reporting it as excluded rather than including it silently.
- **The tray notices a new release and says so a few times.** At most three mentions
  of a given version, never two within twelve hours, and the count resets when a
  *newer* version appears; it lives in the data directory, so restarting the app does
  not restart the nagging, and the tray and the browser share one budget. The lookup
  is cached for hours, because the tray polls every couple of seconds and GitHub is
  not free. On Windows the announcement is a notification balloon rather than a
  dialog, and the menu gains a row naming the version and offering whichever update
  this installation can perform. A prerelease is never announced to somebody running
  a released build.

### Fixed

- **A conversation or project deletion no longer leaves a live session behind.** The
  runtimes hold the project root, so they outlived the record they belonged to and
  could still answer a WebSocket for a project that no longer existed.

## [0.4.0] - 2026-09-18

### Added

- **The launcher entry and start-at-sign-in are reachable from the tray.** Both
  were previously available only by re-running Setup, which is how someone who
  extracted the archive ended up with no Start Menu entry and no way to start at
  sign-in without going looking for a script. The right-click menu now offers
  **Add Start Menu entry** (which also repairs a stale entry, such as the iconless
  shortcut an earlier release created) and **Start at sign-in** / **Don't start at
  sign-in**. The work stays in the installers, run in a new shortcuts-only mode
  that touches nothing else — important, because the app is running out of the
  environment a full install would rebuild.
- **A downloaded release can update itself.** The tray's **Update to the latest
  release…** now works for a zip install instead of opening a browser. It
  downloads the build for this platform, verifies it against the release's
  published `SHA256SUMS.txt`, stages it beside the install, and — once the server
  has exited, which Windows requires because a running program's files are locked
  — swaps the directories and starts the new version. A failed swap puts the old
  directory back, so a failed update leaves a working installation rather than
  half of one. The data directory (settings, database, ~400 MB of speech models)
  lives outside the install and is untouched, which is why an update never
  re-downloads the models. `surtitle update` does the same from a terminal. An
  unpacked *source* ZIP still cannot replace itself and is pointed at the download
  page, as is a folder the user cannot write to.

### Fixed

- **An unattended installer run no longer deletes a sign-in entry it was never
  asked about.** With no `-Startup`/`-NoStartup` (or `--startup`/`--no-startup`)
  and nobody to answer the prompt, it used to take the answer as "no" and remove
  an existing entry. It now leaves the setting exactly as it found it — which is
  also what lets the tray add a Start Menu entry without disturbing sign-in.
- **A release archive's shortcut and app keep their icon.** Both installers
  resolved the icon from `src/surtitle/web`, which a release archive deliberately
  excludes — the copy inside the installed package is the one that travels — so
  the Start Menu entry was created with a generic icon and the macOS app was built
  without one. They now ask the interpreter that runs Surtitle where its own
  package lives, which is right in both layouts, and say so when no icon is found
  rather than failing quietly.
- **Launching no longer re-syncs the environment.** `run.bat`, `run.ps1` and
  `run.sh` ran `uv sync` on every start, which was wrong twice over: without
  `--extra voice-local` it uninstalls that extra even with `--inexact` (the extra
  is in the lock, so `--inexact` does not protect it), so the offline engines had
  to be installed again after every launch from a shortcut; and an environment
  that had drifted from the lock was rebuilt, re-downloading Python and every
  dependency. A warm environment is now launched as it is, and the bootstrap runs
  only when nothing is installed yet. Refreshing dependencies belongs to Setup,
  `-Update`, the tray's update, or an explicit `uv sync`.

## [0.3.0] - 2026-09-18

### Added

- **A release archive can add a launcher entry and a sign-in entry.** `Setup.bat`
  and `Setup.command` now ship inside the archive and detect it: they skip the
  Python and model steps the bundled runtime has already done, and go straight to
  a Start Menu entry (or `~/Applications/Surtitle.app`) and the "start when you
  sign in?" question. Before this the archive carried only the installer that
  refuses to run inside an archive, so someone who downloaded the zip had no way
  to get a menu entry without opening a terminal — which is exactly the case the
  setup path was asked to cover.

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

[Unreleased]: https://github.com/mjoconr/Surtitle/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.4.0
[0.3.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.3.0
[0.2.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.2.0
[0.1.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.1.0
