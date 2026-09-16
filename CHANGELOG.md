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
