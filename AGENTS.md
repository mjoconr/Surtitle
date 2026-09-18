# Agent notes

Orientation for AI coding agents working in this repository. Human-facing
conventions live in [`CONTRIBUTING.md`](CONTRIBUTING.md); this file covers what
an agent needs that a returning contributor would already know.

## This project was renamed — do not reintroduce the old name

The project is **Surtitle**. It was previously called **AgenticVoice**, and
every identifier was renamed in one pass. If your context, notes, or an earlier
session refer to any of the left-hand names below, they are stale: nothing in
this repository answers to them any more.

| Use this | Stale — do not use |
| --- | --- |
| `surtitle` — package, import name, CLI | `agenticvoice` |
| `SURTITLE_*` — environment variables | `AGENTICVOICE_*` |
| `.surtitle.json` — per-project config | `.agenticvoice.json` |
| `.surtitle/` — per-project state (venv, requirements, notes, uploads) | `.agenticvoice/` |
| `…/Application Support/Surtitle`, `%LOCALAPPDATA%\Surtitle` — app data | `…/AgenticVoice` |
| `surtitle.db`, `surtitle.log` — in the app data dir | `agenticvoice.db`, `agenticvoice.log` |
| `surtitle-<version>-<platform>-<arch>` — release archive | `agenticvoice-<version>-…` |

What this means in practice:

- Import from `surtitle.…`; the package is at `src/surtitle/`, not
  `src/agenticvoice/`. A stale path will simply not exist.
- Read configuration through `Settings` in `src/surtitle/config.py`. Do not
  hardcode an environment-variable prefix, a data directory, or a config
  filename in a new module — they are all derived from `APP_NAME` there.
- Any occurrence of an old identifier in tracked source is a bug. The only
  intentional ones are the rename tables in this file and in
  [`CHANGELOG.md`](CHANGELOG.md), which also documents the steps to migrate an
  existing install.
- `.uv-cache/` may still contain snapshots of the old package from earlier
  builds. It is a gitignored build cache, not source — ignore it, and do not
  copy patterns out of it.

## Fast orientation

- `src/surtitle/` — `server.py` (FastAPI + WebSocket), `core/` (agent loop,
  sessions, events), `tools/` (model-callable tools, `path_guard.py`,
  `mcp.py`), `voice/` (hosted and local STT/TTS), `vcs/` (portable git and svn,
  working-copy state, committing, usage guide), `llm/`, `store/`, `web/`
  (browser assets), `cli.py`, `config.py`, `folder_browse.py`, `releases.py`.
- `tests/` — offline and deterministic by default; every model and voice call
  is replayed through scripted fakes, so no API keys are needed.
- `docs/` — `ARCHITECTURE.md`, `TOOLS.md`, `VOICE.md`, `WINDOWS.md`, `VCS.md`,
  `RELEASING.md` (the release process, and what a release must not do).

## Commands

```bash
uv sync                          # set up Python and dependencies
uv run pytest                    # offline suite (no keys, no network)
uv run pytest -m live            # the network subset, run explicitly
uv run ruff check src tests scripts
uv run ruff format src tests scripts
```

`uv` uses `~/.cache/uv` by default. In a restricted sandbox, set
`UV_CACHE_DIR=.uv-cache` so the cache stays inside the checkout — that is what
CI does.

## Read before changing anything

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — test conventions, the three
  security-critical test areas, and the design rules that are easy to break:
  a secret never travels toward the UI, project confinement always goes through
  `resolve_in_root` in `src/surtitle/tools/path_guard.py`, and a tool failure is
  returned as data rather than raised.
- [`CHANGELOG.md`](CHANGELOG.md) — what changed and how to migrate an install.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the pieces fit together.
- [`docs/RELEASING.md`](docs/RELEASING.md) — read this before cutting a release. The version lives in
  three files and CI checks all three; a published tag is never moved; and the artifact has to be
  checked after it is published, not the workflow run.

## Commits

Conventional commits, one logical change each — see
[`CONTRIBUTING.md`](CONTRIBUTING.md#commits) for the format and examples.
