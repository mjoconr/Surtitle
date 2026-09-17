# Architecture

## Process and transport

One Python process serves everything on `127.0.0.1`:

```
Browser ──HTTP──► FastAPI ──► Store (SQLite)
   │                │
   └──WebSocket─────┤
      audio up      │
      audio down    ├──► Deepgram /v2/listen      (STT, Flux turn detection)
      events out    │    or sherpa-onnx OnlineRecognizer   (STT, local)
      commands in   ├──► Deepgram /v1/speak       (TTS)
                    │    or sherpa-onnx OfflineTts        (TTS, local)
                    └──► DeepSeek /chat/completions (streaming + tools)
                              │
                              └──► ToolRegistry ──► project directory
                                                └──► project venv (installs)
                                                └──► MCP servers (stdio)
```

Each half of the voice pipeline chooses its engine independently
(`SURTITLE_STT_BACKEND`, `SURTITLE_TTS_BACKEND`), and
`voice/engine.py` is the only place that decides — so the session never learns
where its audio came from. The local engines are an optional extra, imported
lazily inside `start()`, so the application, its test suite and its release
archive never require them.

Because a local engine is CPU-bound ONNX work, its inference runs on one dedicated
thread per engine: sherpa-onnx streams and ONNX sessions are not documented as
thread-safe, and the cost of being certain is a single thread. Audio is batched
(~320 ms) before decoding so per-frame overhead does not dominate, and the batch
queue is bounded — a decoder that falls behind drops audio rather than falling
further behind the speaker.

Nothing else about the audio path changes. Capture is still 16 kHz mono PCM16 and
playback is still PCM16 scheduled by Web Audio, so the browser cannot tell which
engine produced the audio it is given.

There is deliberately **one** server and **one** port. Serving HTTP and the
realtime channel from the same app removes a whole class of problems: no second
process to supervise, no port pair to keep in sync, and no `multiprocessing` fork
(which is a real hazard on macOS when audio libraries are involved).

### Status, and the Windows tray icon

`GET /api/status` is the one place that answers "what is this process doing".
`/api/health` remains the cheap route the UI polls for configuration state;
`/api/status` adds the run counters and reads the database, so the two are kept
apart rather than growing one endpoint with two audiences and two costs.

The counters live in `stats.RunStats`, owned by `AppState`, and are written from
the two emit paths in `Session` — the turn stream and the side channel. Those two
are the only places that see everything: usage blocks arrive on the side channel
while tool calls and turn boundaries arrive on the turn stream, so counting in
one of them would silently miss half the run. Cost is accumulated per completion
against the rate in force at that moment, because DeepSeek publishes peak and
off-peak rates that differ by a factor of two; a run priced entirely at one of
them would be wrong by 100% half the time. A model with no rate card makes the
whole run report `priced: false`, and the UI then shows tokens with no money
figure, because a confident `$0.00` is a worse answer than none.

The tray icon (`tray.py`, `win32_tray.py`) is a *client* of that endpoint rather
than a second reader of the process's state. It polls over loopback on its own
thread, which costs one HTTP round trip every two seconds and buys two things:
the standalone `surtitle tray` command and the icon started by `surtitle run`
cannot disagree, and the icon never touches the event loop the agent is using.

There is no dependency behind it. `win32_tray.py` calls `Shell_NotifyIcon`
through `ctypes` — a registered window class, a hidden window, a message loop on
a dedicated thread, a popup menu built fresh on every right-click, and a timer
that repaints the tooltip. That is a few hundred lines, and it is the difference
between an archive that works offline with nothing installed and one that carries
a GUI toolkit for a status icon. Every Win32 entry point is given explicit
`argtypes`/`restype` (ctypes otherwise assumes 32-bit `int` and truncates
handles) and the window procedure is held on the icon object for the window's
lifetime (Windows keeps the bare function pointer).

Stopping is `POST /api/shutdown`, refused unless the connection comes from this
machine. The route does not signal the process: the command line owns the
`uvicorn.Server` object and hands the app a callback that sets `should_exit`, so
a tray-initiated stop drains in-flight requests and runs the lifespan teardown
that closes sessions and the database — the same path as Ctrl+C. The running
server also writes a small `server.json` into the data directory so a separately
started tray can find a probed port and confirm the process is alive; the file is
a hint, and every read is confirmed against the server before anything is offered
in a menu.

### Why audio never touches Python

Microphone capture and speaker playback both live in the browser:

- **Capture** prefers an `AudioWorklet` (`web/js/capture-worklet.js`) that
  downsamples to 16 kHz mono PCM16 in the audio thread and posts ~32 ms frames,
  computing a loudness estimate for barge-in as it goes. It has a real failure
  mode: `addModule()` resolves on a **suspended** AudioContext, the node
  constructs, and `process()` is never driven — no error, no audio. So the context
  is resumed and then *verified* running before the worklet is attached, and if no
  frames arrive shortly after starting, capture falls back automatically to a
  `ScriptProcessorNode`, which needs no module loading and works everywhere.
  Falling back to silence is not acceptable.
- **Playback** is Web Audio (`web/js/audio.js`), scheduling `AudioBuffer`s so
  consecutive sentences join without gaps and `stop()` silences everything
  synchronously.

This means no PortAudio, no `sounddevice`, no device enumeration, and identical
behaviour on macOS and Windows. It also keeps the packaged Windows runtime free of
native audio dependencies, which is the usual reason such bundles break.

Deepgram streams raw `linear16` PCM with no container, so an `<audio>` element
could not play it anyway; scheduling buffers directly is both simpler and lower
latency.

## The agent loop

`core/agent.py` runs one user turn as a loop, because the model may call tools,
read results, and continue.

```
run(history, user_text)
  └─ for step in range(max_steps):
       stream completion ──► per token ──► SpeakParser ──► <say>  ──► TTS (immediately)
       │                                              └──► <display> ──► UI
       ├─ tool calls present? ──► execute (with approval) ──► feed results back ──► continue
       └─ none ──► persist transcript ──► done
```

Three things make this a *voice* agent rather than a generic tool runner:

1. **Narration before action.** The system prompt requires a `<say>` block before
   slow tool calls, so the user hears "let me check that" instead of silence while a
   42-page PDF is parsed.
2. **Speech starts before the turn ends.** `on_chunk` is a callback, not a yielded
   event, precisely so audio can start while the loop is still running.
3. **A malformed turn is never silent.** If the model emits no `<say>` tags at all,
   `repair_fallback` derives a spoken summary from the prose.

Assistant messages are assembled into OpenAI-compatible `tool_calls` shape, so the
conversation replays correctly on the next turn.

### What survives a growing conversation

Only the last `_HISTORY_LIMIT` messages are replayed to the model, so the
conversation is not durable memory: anything worked out and kept only in the
transcript decays away, and a later session never sees it at all. Two things carry
knowledge forward instead.

**The project's own Markdown is re-read from disk on every turn.** `AGENTS.md` and
the files under `docs/` are assembled into the *system* prompt, not the
conversation, so they are unaffected by history trimming and a file the agent
writes mid-session takes effect on the next turn. That is why the agent is told as
a standing rule to record what it learns in those files rather than only saying it:
they are the only channel that reaches the next session.

The assistant's notebook (`.surtitle/notes.md`) is injected the same way, but it
is app-local and unshared, so it is for scratch notes. Anything affecting future
work belongs in the project's Markdown, where it is reviewable and committed.

**The primary instruction file is never dropped.** Other oversized documents are
named rather than included, because a document cut to a tenth of itself reads as
the whole thing. The first one is the exception: it carries the project's
conventions and its accumulated learnings, so its absence would mean starting a
session knowing nothing about the project. It is shown in part and listed under
"Instructions shown in part", which addresses the fragment problem directly instead
of by omission.

### The completion sentinel

`_stream_completion` is an async generator that yields UI events *and* needs to hand
back the assembled assistant message. Rather than mixing a return value with a
stream (which Python does not allow), it yields a single `_completion` event at the
end. `run()` consumes that event and never forwards it. This keeps everything on one
stream, so ordering is guaranteed without correlating two channels.

## The speak layer

`core/speak.py`. The most important file in the project.

`SpeakParser.feed(delta)` accepts arbitrary token fragments and returns zero or more
`Chunk`s. It maintains a pending buffer because a tag can be split across deltas
(`"<sa"` then `"y>"`). Boundaries that trigger a spoken chunk:

- a sentence terminator followed by whitespace or end of buffer,
- a clause boundary once a sentence is getting long (so a run-on sentence is not
  held silent),
- the closing `</say>` tag, which flushes whatever remains.

Deliberate choice: a sentence ending exactly at the end of the buffer **is** emitted.
Holding it back would delay speech for no benefit, since the next delta simply starts
a fresh utterance.

`strip_for_speech` removes what reads badly aloud: fenced code, tables, rules,
markdown emphasis, bullet markers, headings (converted to a sentence), URLs and file
paths. Each path pattern is written not to swallow trailing punctuation, because
losing a full stop changes what the synthesizer does with the sentence.

## Sessions and concurrency

`core/session.py` owns one conversation over one WebSocket.

- One task runs the agent turn, held as a cancellable `asyncio.Task`.
- Another drains an `asyncio.Queue` of events to the socket, in order.
- Barge-in must interrupt the first from the second, so cancellation propagates from
  the WebSocket pump through the loop into any running tool subprocess.

State changes use `put_nowait` on the outbox rather than awaiting, so a state update
can never deadlock a caller that is holding the agent's execution.

### Reconnecting to a live conversation

A conversation can have more than one connection asking for it: a page open in two
tabs, or any reconnect. `SessionManager` keys sessions by id and stores the
connection that currently owns each one.

A second connection for a session that is already live **reuses that session and
rebinds its transport** to the new socket, rather than creating a replacement. The
turn in progress keeps running and its queued events go to the new socket, so a
reconnect never costs the user the answer that was being written when it happened.
The reused session re-sends `ready` (with `resumed: true`) so a browser that was
away resynchronises.

Both of the obvious alternatives are wrong, and both were tried:

- **Overwriting the registry entry** orphans the previous session — its Deepgram
  socket and outbox task keep running, so two speech pipelines answer one question.
- **Closing the previous session** cancels the in-flight turn and delivers its
  answer to a socket that no longer exists. Two tabs then destroy each other in a
  loop, each teardown provoking the other's reconnect: six connections in six
  seconds, and a spoken question that was transcribed, sent, and never answered.

Only the connection that still owns a session may close it (`release` compares a
per-connection token), so a superseded tab finishing its handler leaves the live
session alone.

### Approvals

`ApprovalBroker` separates **registration** from **waiting**:

```python
decision = broker.register(call_id, name, args)   # before announcing
yield approval_request(call_id)
allowed, remember = await broker.decision(call_id, decision)
```

The order matters. Registering *after* announcing the request creates a race where a
fast UI answer arrives before anything is waiting, and the turn hangs forever — this
was a real bug found by the test suite. The broker additionally records answers that
arrive before the wait begins, so the race is impossible rather than merely unlikely.

An approval hook on `Tool` lets a tool narrow the decision by argument:
`install_packages` only asks about packages the project has not already approved.

## Storage

`store/db.py` is stdlib `sqlite3` in WAL mode: no extra dependency, no server, one
file. Tables: `projects`, `sessions`, `messages` (with a separate `spoken` column so
the two output channels survive a reload), `tool_calls`.

`store/settings_store.py` splits preferences from secrets:

- `settings.json` — non-secret preferences, safe to share.
- `.credentials.json` — API keys, created `0600` in a `0700` directory, **refused on
  load** if group- or other-readable.

The invariant enforced throughout: **a secret value never leaves the process toward
the UI.** Credentials cross the wire only as `{configured, source, writable}` — never
a value, a suffix, or a length. `describe()` is the contract; adding a value field to
it would be a regression.

Credentials resolve in the order the process actually resolves them: real environment
→ pydantic-loaded config (which covers `.env`) → stored file. Missing the middle case
was a bug that made a working `.env` key appear unconfigured and then get silently
shadowed.

## Tools

`tools/registry.py` holds a `Tool` per capability: JSON Schema for the model, a
human summary for the approval prompt, an approval policy, and a `mutating` flag —

- `path_guard.py` — the confinement boundary. Resolves symlinks *before* testing
  containment, so the comparison is between real paths.
- `shell_tools.py` — runs subprocesses in their own process group so a timeout kills
  the whole tree, not just the parent.
- `artifacts.py` — PDF/XLSX/chart generation with typed schemas, so the model does not
  hand-roll reportlab boilerplate.
- `documents.py` — document conversion. Picks between the built-in engine and
  LibreOffice for each call. LibreOffice ignores the requested output filename and
  always writes `<source stem>.<ext>`, so that path stages into a private directory and
  moves the result into place.
- `document_native.py` — the built-in engine, which needs nothing installed. Extractors
  turn every supported source into one neutral document model and renderers write every
  target out of it, so adding a format is one function on one side rather than a
  converter per pair.
- `environment.py` — per-project virtual environments.
- `mcp.py` — stdio JSON-RPC client.

## Packaging

`scripts/build_release.py` produces one archive containing a standalone Python
(runtime, not installer), a virtual environment with dependencies installed, an
offline wheelhouse, the app, and platform launchers. See
[`WINDOWS.md`](WINDOWS.md).

Local voice is two independent, opt-in halves of the build:

| Flag | Adds | Result |
|---|---|---|
| `--with-voice-local` | the `sherpa-onnx` runtime (~30 MB) | local engines work; models download on first use |
| `--with-local-models` | the speech models (~90 MB) | the archive is fully offline out of the box |

The default build contains neither, so the documented "extract and run, no
network" property is unchanged and the archive stays small. `verify_archive()`
asserts the local engines actually import when they were requested, because a
wheel/ABI mismatch is invisible until the first model load.

Building must happen **on** the target OS, because compiled wheels are
platform-specific — `release.yml` runs the build on `windows-latest` and
`macos-latest` rather than cross-compiling. That applies double to the local
engines, whose `sherpa-onnx` wheels are per-platform binaries.

## Installation and updates

`scripts/install.sh` (macOS/Linux) and `scripts/install.ps1` (Windows) exist
because "clone the repo and hope `uv` is installed" is not an installation story.
Each one finds or installs `uv`, creates the virtual environment, optionally
installs the local voice extra and downloads models, then runs `doctor --offline`
to prove the result.

The layout they create is what makes updating cheap and safe: **code and models
live in different places.** The code and its virtual environment are in the
checkout; the models are in the app data directory. Re-running the installer with
`--update` refreshes the environment and re-verifies the models without
re-downloading them, and deleting the checkout does not delete them.

