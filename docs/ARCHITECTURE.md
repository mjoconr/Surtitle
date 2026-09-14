# Architecture

## Process and transport

One Python process serves everything on `127.0.0.1`:

```
Browser ──HTTP──► FastAPI ──► Store (SQLite)
   │                │
   └──WebSocket─────┤
      audio up      │
      audio down    ├──► Deepgram /v2/listen   (STT, Flux turn detection)
      events out    ├──► Deepgram /v1/speak    (TTS)
      commands in   └──► DeepSeek /chat/completions (streaming + tools)
                              │
                              └──► ToolRegistry ──► project directory
                                                └──► project venv (installs)
                                                └──► MCP servers (stdio)
```

There is deliberately **one** server and **one** port. Serving HTTP and the
realtime channel from the same app removes a whole class of problems: no second
process to supervise, no port pair to keep in sync, and no `multiprocessing` fork
(which is a real hazard on macOS when audio libraries are involved).

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
- `documents.py` — LibreOffice conversion. LibreOffice ignores the requested output
  filename and always writes `<source stem>.<ext>`, so conversion stages into a
  private directory and moves the result into place.
- `environment.py` — per-project virtual environments.
- `mcp.py` — stdio JSON-RPC client.

## Packaging

`scripts/build_release.py` produces one archive containing a standalone Python
(runtime, not installer), a virtual environment with dependencies installed, an
offline wheelhouse, the app, and platform launchers. See
[`WINDOWS.md`](WINDOWS.md).

Building must happen **on** the target OS, because compiled wheels are
platform-specific — `release.yml` runs the build on `windows-latest` and
`macos-latest` rather than cross-compiling.
