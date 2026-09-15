# Surtitle

A voice-first agentic workbench. Talk to an agent that reads the documents in a
project directory, writes and runs code, and produces real PDFs and
spreadsheets — then answers you out loud, conversationally.

Runs on **macOS and Windows**. Ships with a self-contained Windows runner: extract
and double-click, no installer, no administrator rights, no system Python.

```
You:  "Open the Q3 report and tell me how revenue moved."

Agent: <say>Let me open the Q3 report.</say>                        ← spoken aloud
       <display>read_file("reports/q3.pdf") → 42 pages</display>     ← on screen only
       <say>Revenue is 1.24 million, up eight percent. I'll build the summary sheet.</say>
       <display>make_spreadsheet("q3-summary.xlsx") → created</display>
       <say>Done. The summary is in your project folder.</say>
```

## Why this exists

Most "voice agents" are a wrapper that reads the model's entire output aloud:
file paths, markdown tables, code blocks, all of it. That is unbearable to listen
to. Surtitle instead gives the model **two output channels** — one to speak,
one to display — and streams the spoken channel to text-to-speech sentence by
sentence while the model is still generating. You hear the conclusion, not the log.

## Quickstart

### Windows

1. Download `surtitle-<version>-win32-amd64.zip` from [Releases](../../releases).
2. Extract anywhere — your Desktop is fine. **No installer, no admin rights.**
3. Double-click **`run.bat`**. Your browser opens the app.

The archive bundles its own Python and every dependency, so nothing is downloaded
on first run and no system Python is required.

To install from source instead, or to add the offline speech engines later:

```powershell
.\scripts\install.ps1 -Yes       # Python, dependencies, local voice, models
.\scripts\install.ps1 -Update    # update an existing installation
```

### macOS / Linux

```bash
git clone <this repo> && cd Surtitle
./scripts/install.sh          # Python, dependencies, and optionally offline voice
./scripts/run.sh              # start it
```

`install.sh` uses [uv](https://docs.astral.sh/uv/) for Python and dependencies —
installing uv if you do not have it — and is safe to re-run. `--no-voice` skips the
local speech engines, and `--update` refreshes an existing install. A released archive
is just as easy: extract and run `./scripts/run.sh`.

If you would rather not install anything up front, `./scripts/run.sh` alone still
bootstraps a working environment; `install.sh` simply does the same thing, plus the
local voice extra and the model download, in one step.

### First run

1. The app opens at `http://127.0.0.1:8765`.
2. **Settings → API keys**: add a DeepSeek key and, for hosted voice, a Deepgram key.
   They are stored locally with owner-only permissions and are never sent back to the
   browser.
3. **New project**: create one, or point it at a folder you already have.
4. Click the mic and talk — or type. Click again to stop listening.

To install everything — Python, dependencies, and optionally offline voice — use the
installer instead of doing the above by hand. It is idempotent and has an update path:

```bash
./scripts/install.sh                 # macOS/Linux
./scripts/install.sh --update        # refresh an existing install
```

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Yes
.\scripts\install.ps1 -Update
```

### Do I need both keys?

| Key | Needed for | Without it |
|---|---|---|
| `DEEPSEEK_API_KEY` | Everything — it is the agent | The app will not start |
| `DEEPGRAM_API_KEY` | Hosted voice in and out | Voice works only if you set the engines to `local` |

Run `./scripts/run.sh doctor` to check both keys with live probes before starting.

### Voice engines: hosted or local

Each direction chooses its engine independently, in Settings → Voice or with
`SURTITLE_STT_BACKEND` / `SURTITLE_TTS_BACKEND`:

| | `deepgram` (default) | `local` |
|---|---|---|
| Runs | hosted WebSocket | on your CPU, offline |
| Key | required | none |
| Cost | per minute | none |
| Install | an API key | `uv sync --extra voice-local` + `surtitle models download` (~90 MB) |
| Turn taking | contextual (uses what you said) | trailing silence, plus a completeness heuristic |
| Accuracy | low word error | higher word error, especially proper nouns |

Local voice is genuinely usable — recognition runs at about **0.13× real time** and
speech synthesis at **0.63–0.70×**, so it keeps ahead of playback on a laptop CPU
with no GPU. It is not a straight upgrade: turn detection is a timer rather than an
understanding of what you said, and a 70 MB model transcribes proper nouns and jargon
noticeably worse. [`docs/VOICE.md`](docs/VOICE.md) has the measured figures and the
trade-offs, including the hybrid "local ears, hosted voice" setting, which is usually
the right one.

```bash
surtitle models list       # what is installed, and what it would cost
surtitle models download   # fetch or repair the speech models
surtitle models verify     # re-check every installed file's checksum
```

## Projects

A project is a directory plus a conversation history. The agent can **only** read
and write inside that directory.

- Create one and give an existing folder path to work in place, or
- leave the folder blank for a managed directory under the app data folder.

Confinement is enforced in one place and tested adversarially: path traversal,
symlink escapes, absolute paths, Windows `\\?\` device paths, UNC shares and
`C:` drive-relative paths are all refused. Per-project settings live in
`.surtitle.json`, so a project inside a git repo travels with its
configuration.

## What it can do

| Tool | Purpose |
|---|---|
| `list_dir`, `read_file`, `search_files` | Read the project. PDFs are text-extracted automatically. |
| `write_file`, `edit_file` | Write or surgically edit text files. |
| `run_python` | Execute Python in the project directory. |
| `run_shell` | Run a shell command (`cmd.exe` on Windows, `/bin/sh` elsewhere). |
| `make_pdf`, `make_spreadsheet`, `make_chart` | Produce formatted documents directly. |
| `convert_document` | Convert Word/Excel/PowerPoint/OpenDocument via local LibreOffice. |
| `environment_info` | Report which Python environment code runs in. |
| `search_packages`, `install_packages` | **Acquire new capability on demand** (below). |
| Attachments | Drag, paste or pick files; they are saved into the project and read like any other file. |
| `<server>__<tool>` | Any tool from a configured **MCP server**. |

Everything that can change something on disk asks first. Read-only tools never
interrupt you.

### The agent can extend itself

The agent is not limited to the libraries it ships with. When a task needs
something else it installs it — into an **isolated per-project environment**, so
the application itself is never modified:

```
Agent: <say>I need pandas for this. Installing it now.</say>
       install_packages(["pandas>=2.2", "openpyxl"])
```

- Packages land in `<project>/.surtitle/venv`, never in the app, and
  `run_python` immediately executes against them.
- Approvals are **remembered per project**: approving `pandas>=2.2` once records it
  in `.surtitle/requirements.txt`, so a fresh clone installs it without asking
  again. A *new* package still asks.
- Requirement strings are strictly validated. A flag such as `--index-url=…` or a
  direct URL install is refused, so a crafted "package name" cannot become a
  package-manager option or pull code from an untrusted host.
- Deleting the project deletes the environment. Nothing is left behind.

Installing a package runs third-party code, which is exactly why it is gated and
why the environment is isolated.

### What the agent is primed with

Priming is layered, and you can add to it without touching code:

| Layer | Where it comes from |
|---|---|
| Voice contract | The system prompt — what to speak versus show |
| Process rules | Workspace is authoritative over memory; resolve by inspection, ask only what you cannot; work in a stated order; read how a command exited |
| **Anti-assumption rule** | "Never state an assumption as fact" — Checked / Told / Assumed, with assumptions labelled and offered for checking |
| **Repeat-call guard** | A tool call repeated with identical arguments is refused after the third attempt, and handed the result it already has |
| **Work memory** | Each turn records `[work this turn]`, so the agent can see what it already read or ran |
| **Project instructions** | `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `CONTRIBUTING.md`, and `instructions` in `.surtitle.json` — loaded automatically every turn |
| **Project notebook** | `.surtitle/notes.md`, written by the agent's own `remember` tool and injected into every future session |
| **Conversation search** | SQLite FTS5 over every stored message, via the `search_history` tool — finds what the notebook did not record |
| Project briefing | The working directory and its top-level entries |

The notebook is how knowledge accumulates across conversations instead of each new
chat re-deriving everything: the agent records conclusions — which machine is down,
where a command lives, what a term means — and sees them again next time.

Two layers, deliberately: the **notebook** holds what the agent chose to write down,
and **search** finds what it did not. Searching is a tool call rather than an
automatic context injection, so it costs nothing until it is useful, and the current
session is excluded so the agent is not handed its own last utterance as memory.

Documents are read from the project root **and** `docs/`, because a project that
keeps its orientation material there was otherwise invisible — including the file
that documented how to reach its own systems. Only the routing layer (typically
`AGENTS.md` plus safety rules) is kept resident; the rest is named so the agent
reads what it needs. A document too large to show honestly is named rather than
truncated, since a fragment reads as the whole thing.

Editing `AGENTS.md` mid-session takes effect on the very next turn. Everything is
capped, so nothing can crowd out the conversation.

### Attachments

Drag files onto the window, paste a screenshot, or use the **+** button. Files are
saved into a visible **`uploads/`** folder inside the project and referenced in the
message by path, so the agent reads them with the same `read_file` it uses for
anything else — an attached PDF goes through the same extraction path as one you
put there yourself.

They go in `uploads/` rather than hidden tooling state deliberately: that keeps
them visible in your file manager *and* discoverable by the agent's own
`list_dir` and `search_files`.

Filenames are treated as hostile — directory components stripped, characters
restricted, Windows-reserved names escaped — so an uploaded name can never become
a path. Requests are capped at 32 MB per file and 12 files at a time, and a
rejected upload leaves nothing behind.

### Documents and CAD

LibreOffice conversion works on both platforms — it is discovered automatically, or
set `soffice_path` in `.surtitle.json` to point at `soffice`. For **CAD**
(Fusion 360, OpenCASCADE, or your own tooling), the answer is
[MCP](#mcp-servers) rather than a bespoke integration.

## MCP servers

[Model Context Protocol](https://modelcontextprotocol.io) is how the agent reaches
systems that are not files: Autodesk Fusion 360, a vendor CAD tool, or an in-house
server for a sampling line. Add servers to `.surtitle.json`:

```json
{
  "mcp_servers": [
    {
      "name": "fusion",
      "command": "fusion-mcp",
      "args": ["--stdio"],
      "namespace": "fusion360",
      "trusted_tools": ["get_design"]
    },
    {
      "name": "sampling-line",
      "command": "C:/tools/sampling-mcp.exe",
      "env": { "SAMPLING_TOKEN": "SAMPLING_TOKEN" }
    }
  ]
}
```

Their tools appear to the agent as `fusion360__<tool>` and
`sampling-line__<tool>`, and inherit everything else: approval prompts, transcript
rows, and the speak layer. Remote tools require approval by default unless listed
in `trusted_tools`.

Notes:

- `env` maps the child's variable name to a variable to pass through. **Never put a
  secret in this file** — it is meant to be committed with the project.
- A server that fails to start does not break the session; its tools are simply
  absent and the reason is reported in the UI.
- Implemented directly over stdio JSON-RPC rather than through the official SDK,
  whose dependency chain needs a Rust build and would defeat the no-toolchain
  Windows release.

## Configuration

Global preferences live in the app data directory and are editable in the app:

| Platform | Location |
|---|---|
| macOS | `~/Library/Application Support/Surtitle` |
| Windows | `%LOCALAPPDATA%\Surtitle` |

- `settings.json` — preferences. Safe to read and share.
- `.credentials.json` — API keys only. Created `0600` inside a `0700` directory, and
  **refused on load** if it is readable beyond its owner.

Configuration is read from exactly one place per setting, in this order:

1. **Real environment variables** (`DEEPSEEK_API_KEY=…` in your shell)
2. **`.env.local`**, then **`.env`**, in the *repository root*
3. **Saved settings** in the app data directory

Two consequences worth knowing:

- **A key supplied by steps 1–2 is read-only in the UI**, and the field says why.
  Environment configuration wins, so editing it in the app would appear to do
  nothing. Unset it (or remove it from `.env`) and restart to manage it in the
  app instead.
- **Environment files are resolved to absolute paths, anchored to the repository
  root** — never relative to wherever the process started. Relative resolution
  previously meant a stray `scripts/.env` could silently override every default,
  which produced an STT model mismatch that looked like a code bug. Run
  `./scripts/run.sh doctor` to see exactly which config files were read.

Copy `.env.example` to `.env` in the repository root for a documented template of
every setting. Do not put a `.env` in `scripts/`: it is not read, and it will
confuse you.

## Architecture

```
Browser ── mic PCM16 ──► FastAPI ──┬─► Deepgram /v2/listen (Flux turn detection)
        ◄── audio ─────  127.0.0.1 │   or sherpa-onnx OnlineRecognizer (local)
        ◄── events ────       │    ├─► Deepgram /v1/speak
                              │    │   or sherpa-onnx OfflineTts (local)
                              │    └─► DeepSeek chat completions (streaming + tools)
                              │              │
                              │              └─► tool registry → project directory
```

Four decisions shape everything:

**Audio never touches the Python process.** Capture and playback both live in the
browser (an `AudioWorklet` and Web Audio). No PortAudio, no device enumeration, one
code path for macOS and Windows — and the same one for both voice engines, since
the contract is PCM16 either way.

**One process, one port.** HTTP and WebSocket share a FastAPI app — no second server
to pair up, and no macOS `fork` hazard.

**Turn detection is contextual — on the hosted engine.** Flux (Deepgram's Listen v2)
decides when you have finished based on *what you said*, not a silence timer, so it
neither cuts you off mid-thought nor makes you wait out a timeout. Nova with
`endpointing` is the configurable fallback. The local engine has no such judgement and
uses a silence timer with a completeness heuristic; that difference is the single
biggest reason the hosted engine remains the default.

**The voice engine is a backend, not the architecture.** `voice/engine.py` chooses
which implementation each direction uses, so the session, the browser protocol and the
agent loop are unaware of it. The local engines are an optional extra, imported lazily
— the app, its test suite and its release archive never require them.

### Barge-in

Interrupting the agent stops audio in two stages: the browser silences its local
playback queue the instant speech is detected, and the server cancels the model
stream and abandons the voice engine so audio already synthesised for cancelled text
is never delivered. On the hosted engine that means closing the Deepgram socket; on
the local engine it means discarding the synthesis result that arrives after the
interruption, because an ONNX call already running cannot be cancelled. A short grace
window after playback begins prevents the speaker tail from triggering a false
interruption before echo cancellation has converged.

### The speak layer

`<say>` and `<display>` tags are parsed **incrementally**, so a tag split across a
token boundary is handled and the first sentence is spoken while the model is still
generating. If the model forgets the tags entirely, a repair pass synthesises a
spoken summary — a malformed turn is never silent. Markdown, code blocks, tables,
URLs and file paths are stripped before speech, because they read terribly aloud.

## Development

```bash
uv sync                              # install
uv sync --extra voice-local          # plus the offline speech engines
uv run pytest                        # full suite, offline
uv run pytest -m live                # the subset that reaches the network
uv run ruff format src tests scripts
uv run ruff check src tests scripts
uv run surtitle doctor           # live-probe your configuration
uv run surtitle models download  # fetch local speech models (~90 MB)
```

The default suite is **fully offline**: every DeepSeek and Deepgram interaction is
replayed through scripted fakes, so tests are deterministic and need no API keys.
The local engines are tested the same way — a fake `sherpa_onnx` is injected, so no
test needs the optional extra, a model file, or ONNX. `pytest -m live` is the opt-in
subset that really installs a package from PyPI and really converts a document.

```
src/surtitle/
  core/       agent loop, speak layer, session orchestration, event protocol
  llm/        DeepSeek streaming client
  voice/      engine selection, Deepgram STT/TTS clients, local (sherpa-onnx)
              STT/TTS adapters, the model registry and installer
  tools/      registry, path guard, filesystem, shell, artifacts, documents,
              environments, MCP client
  store/      SQLite sessions, settings and credentials
  web/        browser UI (no build step — plain ES modules)
```

More detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
[`docs/VOICE.md`](docs/VOICE.md), [`docs/TOOLS.md`](docs/TOOLS.md),
[`docs/WINDOWS.md`](docs/WINDOWS.md), [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Security

What the design does and does not protect against:

- ✅ The agent **cannot escape the project directory** — one enforced guard, tested
  against traversal, symlinks and platform-specific path tricks.
- ✅ **Credentials stay local.** They are never returned to the browser, logged, or
  included in an error message — only `configured: true/false` crosses the wire. The
  credentials file is owner-only and refused if it is world-readable.
- ✅ **Mutating actions require approval**, and the prompt names the specific action:
  the actual command, the actual packages. *Allow once* and *Always allow* are
  separate buttons, and remembering a decision is per project.
- ✅ **Installs are isolated** in a per-project environment and strictly validated.
- ⚠️ Approved tools run as **your user, with your permissions**. Approving
  `run_shell` means exactly that; review what you approve. The guard confines *this
  app's* file tools, not what an approved shell command can do.
- ⚠️ The server binds `127.0.0.1` and refuses non-loopback hosts unless you pass
  `--host` explicitly. Do not expose it to a network.

## License

MIT — see [`LICENSE`](LICENSE).
