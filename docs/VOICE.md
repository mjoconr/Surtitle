# Voice pipeline

How audio gets in, how it gets out, and why interrupting works.

## The path

```
microphone
   │  getUserMedia({echoCancellation, noiseSuppression, autoGainControl})
   ▼
AudioWorklet  ── downsample to 16 kHz mono ──► PCM16 frames (~32 ms)
   │                                                 │
   │  loudness estimate (barge-in)                   │ binary frame 0x01
   ▼                                                 ▼
audio thread / main thread                        WebSocket
                                                     │
                                                     ▼
                                         Deepgram /v2/listen  (Flux)
                                                     │
                       interim ● final ● EndOfTurn ──┤
                                                     ▼
                                              agent turn starts
                                                     │
                                          DeepSeek streams tokens
                                                     │
                                          SpeakParser → <say> sentences
                                                     │
                                                     ▼
                                          Deepgram /v1/speak
                                                     │  linear16 PCM
                                                     ▼
                                        WebSocket binary frame → browser
                                                     │
                                                     ▼
                                     Web Audio schedules AudioBuffers
```

## Speech in

Two backends, selectable with `SURTITLE_STT_API`:

**`v2` (default) — Flux.** A model trained for *contextual* end-of-turn detection. It
uses the linguistic content, not just silence, so it does not cut you off mid-thought
and does not make you wait out a fixed timeout after you finish.

Flux takes a much smaller parameter set than Nova and **rejects anything it does not
recognise with HTTP 400**. Verified against the live endpoint, these are refused on
`/v2/listen`: `channels`, `language`, `interim_results`, `punctuate`, `smart_format`,
`vad_events`, `endpointing`, `utterance_end_ms` and `multichannel`. Accepted:
`model`, `encoding` + `sample_rate` (together), `keyterm`, `numerals`, `eot_threshold`
and `eot_timeout_ms`.

This is not a detail to get subtly wrong — building one query string for both backends
produced a permanent reconnect loop on the first real run, which is why
`tests/test_voice_clients.py` asserts the exact parameter sets.

The consequence for configuration: **`SURTITLE_ENDPOINTING_MS` does nothing on
v2.** Tune turn boundaries with `SURTITLE_EOT_THRESHOLD` (how confident the model
must be that you have finished; higher waits longer) and
`SURTITLE_EOT_TIMEOUT_MS` (the ceiling on how long a turn stays open).

**`v1` — Nova** with `endpointing`. Kept as a fallback for accounts or regions where
Flux is unavailable, and useful when you want to tune the silence threshold directly.

### Capture: the AudioWorklet message API

Capture prefers an `AudioWorklet` — it runs on the audio thread, so it is never
blocked by rendering — and falls back to a `ScriptProcessorNode` if the worklet
produces no frames.

For a long time **the worklet never produced a frame, in any browser**, and the
fallback silently did all the work. Nothing errored: `addModule()` resolved, the
context was `running`, `process()` was called with real input, and the graph was
correct. The processor simply never emitted anything.

The cause is that browsers disagree about how a processor receives messages:

- the specification delivers them to `port.onmessage`;
- Chrome **never calls** a `handleMessage` method;
- Firefox has historically called **only** `handleMessage`.

The processor defined `handleMessage` alone. So the `{type: "mute", value: false}`
that arms capture never arrived, `_muted` stayed at its constructor default of
`true`, and `process()` took the muted branch on every quantum — no `level`
messages, no `audio` frames, forever. Both entry points are now wired to one
handler, and both are idempotent, so it does not matter which the browser calls.

Measured in a real browser, with the shipped processor and an oscillator as input:

| Wiring | Audio frames in 2 s |
|---|---|
| `handleMessage` only (the bug) | **0** |
| `port.onmessage` only | **62** |
| `port.onmessage` + output to destination | **62** |

Note what the last two rows say: `numberOfOutputs: 0` with no path to the
destination is fine, so this was never a graph problem. `62` frames is exactly
2 s ÷ 32 ms.

The lesson for the diagnostic is in `mic-probe.html`: it loads the **shipped**
processor and arms it with the same message the app sends. A probe that only
checked "does a worklet node construct, and does `process()` run" passes with the
bug present, because both are true.

**Bump `WORKLET_VERSION` in `audio.js` whenever `capture-worklet.js` changes.**
Worklet modules are cached by the browser independently of the page, so an
upgrade served from a bare URL keeps running the previous processor — including
this one, which never worked.

### The Flux response shape

Confirmed by capturing a live socket streaming silence at 16 kHz. There is **no
Nova-style `channel.alternatives` envelope** — the transcript is a top-level field:

```json
{"type": "TurnInfo", "event": "Update", "turn_index": 0,
 "audio_window_start": 0.0, "audio_window_end": 0.24,
 "transcript": "what is the throughput of the line", "words": [...],
 "end_of_turn_confidence": 0.91, "sequence_id": 1}
```

- `event: "Update"` carries an in-progress transcript — this drives the live
  captions.
- `event: "EndOfTurn"` closes the turn and starts the agent's turn.
- **Each update is the transcript for the turn so far, and it is revised.**
  Measured on a real utterance, the sequence grew and was corrected in place:

  ```
  3 words  "Read this part"
  3 words  "Read this project"        <- "part" revised to "project"
  4 words  "Read this project and"
  5 words  "Read this project and tell"
  4 words  "Read this project until"  <- revised back, and shorter
  6 words  "Read this project and tell me"
  …        "Read this project and tell me, uh, if the current status of it"
  ```

  So merging is **replace, never append**. Appending produced this visible
  failure, where the delivered instruction was:

  ```
  "Read this part Read this project Read this project and Read this …"
  ```

  A shorter update still wins, because it is a *correction* of the same utterance
  rather than a fragment of it. The one thing replacement must not do is accept an
  empty update: the end-of-turn message carries no transcript, and treating it as
  the new text discarded everything spoken before it.
- A **zero-length transcript with `Update` is normal** while the channel is open
  (it was every message during silence). It must be ignored rather than treated as
  an empty utterance, or the agent would respond to nothing.
- `end_of_turn_confidence` is how sure the model is that you have finished. It is
  *not* the same quantity as Nova's transcript confidence, so it is what gets
  surfaced as the confidence figure on the Flux path.
- Valid client control messages are only `CloseStream`, `ForceEndTurn` and
  `Configure`. `KeepAlive` is accepted by Nova and **rejected by Flux**, which
  closes the connection — that caused a reconnect every second. The send loop
  therefore sends nothing while idle; an open microphone keeps the socket alive by
  streaming audio.

The receive loop still parses defensively: an exact `type` match is tried first, then
any payload carrying turn-shaped fields (`transcript`, `words`, `end_of_turn`) is
routed to the turn handler, and finally the Nova envelope. A renamed server event
therefore degrades to "captions still work" instead of dropping every transcript.

The tests for this use payloads captured verbatim from the live endpoint rather than
invented ones, in `tests/test_voice_clients.py`.

### Echo control

The agent must not transcribe its own voice. Three layers, in order:

1. Browser echo cancellation (`echoCancellation: true`) does most of the work.
2. The server enables STT suppression whenever TTS starts and clears it when TTS
   finishes, so it cannot drift out of sync with what is actually playing.
3. The capture worklet opens a short grace window after playback begins, ignoring
   input while the speaker tail decays.

Final transcripts that arrive while suppressed are dropped.

## Speech out

One Deepgram socket per turn. For each sentence from the speak layer:

```
{"type": "Speak", "text": "..."}   → synthseise this sentence
{"type": "Flush"}                   → send it now, do not wait for more
```

**Flushing per sentence is the key latency decision.** Flushing only at the end of the
whole reply (as most demo code does) means the *first* audio is not produced until the
model has finished generating. Flushing per sentence means speech starts while the
model is still writing.

Audio arrives as binary frames and is relayed to the browser as WebSocket binary
frames with a one-byte opcode, avoiding base64's 33% overhead on a continuous stream.

`speed` is sent only when it differs from `1.0`. Not every Aura voice accepts it, so a
rejected connection is retried once without the parameter and the browser's
`playbackRate` becomes the fallback.

### Gapless playback

Sentences arrive as separate chunks but must sound like continuous speech. `Playback`
keeps a `nextTime` cursor and schedules each buffer to start where the previous one
ended, with a small 60 ms lead-in so the first chunk is never clipped by scheduling
latency.

## Playback and the browser's autoplay policy

A Web Audio `AudioContext` created **outside a user gesture** starts suspended, and
`resume()` afterwards rejects because the activation has already been consumed. An
agent reply that arrives over a WebSocket is exactly that case: the context was
being created when the first audio chunk landed, so it was suspended forever and
playback silently did nothing. No exception, no console error — just silence,
which is the hardest kind of bug to report.

Three things prevent it:

1. **`Playback.unlock()` runs inside the mic click**, creating and resuming the
   context while activation is still valid, and priming the graph with a
   single-sample buffer so the audio stack is genuinely open.
2. **`onBlocked` reports it.** If audio arrives while the context is not running,
   the UI says the browser blocked playback and tells the user to click. Once per
   turn rather than once per chunk.
3. **The buffer is declared at the synthesis rate, never the context's rate.** The
   context is asked for the rate Deepgram synthesises at, but whatever it ends up
   running at is irrelevant to the buffer: an `AudioBufferSourceNode` resamples its
   buffer into the context's rate, so the buffer must say what the PCM really is.

   This was got wrong, and the wrong version was documented here as correct
   ("read back the context's rate and decode at that rate"). It sounds plausible
   and is backwards. Overwriting the synthesis rate with the context rate makes
   every reply play fast and high whenever the two differ:

   | Context ends up at | 1 s of speech plays in | Speed | Pitch |
   | --- | --- | --- | --- |
   | 24000 Hz | 1.000 s | 1.00× | correct |
   | 44100 Hz | 0.544 s | 1.84× | +10.5 semitones |
   | 48000 Hz | 0.500 s | 2.00× | +12.0 semitones |

   Those figures are measured in a real browser, by declaring 24 000 samples both
   ways and reading `AudioBuffer.duration`. At +10–12 semitones a voice does not
   sound "slightly fast", it sounds like **a different person**, and because it
   depends on which rate the context lands on it appeared intermittently: fine on
   a machine whose Chrome honoured 24 kHz, wrong on the same page in Firefox,
   which commonly ignores the request and uses the hardware rate.

   The rate the server reports is now adopted via `setServerRate()`, and a
   mismatch is reported in the Activity panel rather than left to be heard.

4. **A paused sink element is restarted.** Audio is routed through the `<audio>`
   element because that is the only way to call `setSinkId`. An audio-route change
   (headphones, a dock, a sleeping output) can pause that element underneath the
   page, and a paused element is silence with no error anywhere. Each turn checks
   `element.paused` and calls `play()` again.

If you hear nothing: click once anywhere on the page (that grants activation) and
try again. If you see the "audio is blocked" notice, that is this path reporting
itself rather than failing quietly.

## Diagnosing a silent microphone

Measured on the development machine, which is a useful worked example because the
fault looked like a muted microphone but was not:

| Layer | Evidence | Verdict |
|---|---|---|
| Hardware | macOS `MacBook Pro Microphone`, 8 inputs including a RØDE, VB-Cable, Zoom and Teams virtual devices | Chrome picked the real built-in mic |
| Browser | `/static/mic-probe.html` measured peak amplitude **0.73** (Chrome) and **0.92** (Vivaldi) | The browser receives strong audio |
| Application | Activity panel: `frames=0` | Capture never ran inside the app |

`web/mic-probe.html` (served at `/static/mic-probe.html`) exists precisely to draw
that third line: it grants the microphone, names the chosen device, lists every
input, and measures the real peak amplitude over three seconds, entirely
independently of this application. Without it, "the browser has no signal" and
"the application mishandles the signal" are indistinguishable, and they have
opposite fixes.

The Activity panel reports what capture negotiated, so the app can be diagnosed
without a console:

```
Microphone opened      device="MacBook Pro Microphone" context=running track=live
Capture check (3.5 s)  frames=142 peak=0.0412 context=running
```

`frames=0` means the worklet never ran. `frames>0` with `peak≈0` means capture ran
and the room was quiet. Those are different faults.

Browsers also differ in whether a capture context starts suspended: Chrome began
`suspended` and only ran after an explicit `resume()`, while Vivaldi began
`running`. A suspended context silently produces nothing, so the capture path
resumes before and after loading the worklet and reports the state.

## Choosing input and output devices

Both are selectable in **Settings → Microphone**, because relying on the browser's
defaults is wrong on any machine that has virtual audio devices. The development
machine lists eight inputs — the built-in microphone plus Teams, three RØDE
Connect devices, VB-Cable, Zoom and iThinking — and the default can be a device
that never carries the microphone. Right-clicking the mic button jumps straight to
the picker.

**Input** uses `deviceId` in `getUserMedia`. Device *labels* are only exposed after
microphone permission has been granted, so the list may be anonymous until the mic
has been opened once; the picker says so instead of showing an unexplained single
entry. A saved device that has since been unplugged is reported and the system
default is used, rather than refusing to start.

**Output** needs a different mechanism, and this is not obvious: **Web Audio cannot
select an output device.** Only `HTMLMediaElement.setSinkId()` can. Spoken audio is
therefore routed through a hidden `<audio>` element via a
`MediaStreamAudioDestinationNode` — the audio graph still does all the scheduling,
and the element is purely the sink. Exactly one output path is connected; wiring
both the element and the context destination would play every sentence twice.

Output selection is genuinely partial: Firefox does not implement `setSinkId`, so
the control is disabled with an explanation rather than silently doing nothing.
`setSinkId` can also reject with `NotAllowedError` when the page has not been
granted audio permission, which the UI reports with the fix (open the microphone
once).

## Barge-in

Interrupting mid-sentence is the behaviour users judge a voice agent on. When the
capture worklet detects sustained loudness above threshold while the agent is
speaking:

**Client, immediately (synchronous):**
1. `Playback.stop()` cancels every scheduled `AudioBufferSourceNode` → instant silence.
2. Send `barge_in`.
3. Enter `listening` state.

**Server:**
4. Cancel the agent turn task; cancellation propagates into any running tool subprocess.
5. `TextToSpeech.barge_in()` increments a generation counter, empties the queue, sends
   `Clear` (discard audio Deepgram has generated but not delivered), sends `Close`, and
   closes the socket.

Step 5 is why the socket is closed rather than merely drained. Deepgram has already
synthesised audio for text that was sent; leaving the socket open means that backlog
arrives later and sounds like the agent ignoring the interruption. Closing discards it
by construction, and the next sentence simply opens a fresh socket.

### Interruption requires speech, not loudness

The client detects a *level* for responsiveness, but level cannot tell a person
from the agent's own speakers. In a real session that distinction was missing and
the consequence was visible: replies were cut off mid-sentence, and two user
utterances were stored with no reply after them.

So the client only **requests** an interruption. The server decides, and the
evidence it requires is a **transcript** — someone actually talking while the agent
speaks. An explicit stop passes the guard and always wins.

The client's detector is deliberately conservative for the same reason: about
130 ms of sustained speech above 0.05 RMS, with a 400 ms grace window after
playback begins so the speaker tail cannot trip it before echo cancellation has
converged. A false interruption is worse than one arriving a fraction of a second
late.

### An interrupted turn is still recorded

Cancelling a turn used to skip persisting it: the user's message was stored while
the assistant's reply was not. Every interrupted exchange therefore vanished from
the model's history, which reads as the agent forgetting the conversation — and
left questions looking unanswered. A partial reply is now written on cancellation,
flagged so the next turn does not append the user message twice.

### False-positive control

A single loud spike must not truncate a sentence, so detection requires the threshold
to be exceeded for **three consecutive frames** and a genuine pause before re-arming, so
one utterance cannot fire several barge-ins in a row.

## Push-to-talk and open mic

- Click the mic to toggle listening.
- While listening, captions show interim text live and are replaced by the final
  transcript.
- A completed turn (`EndOfTurn`) starts the agent; a new turn barges in on anything
  still playing.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| STT socket drops | Reconnect with capped backoff (0.5s → 8s); a notice is surfaced, the session continues. |
| STT send half fails | Treated as a dropped socket, so it reconnects. See below. |
| TTS socket drops | Reconnect with backoff and re-queue the sentence once, so it is not silently lost. |
| TTS unavailable | The turn still completes and the transcript is complete; only audio is missing. |
| Microphone denied | Text-only mode; the error explains that typing still works. |
| No Deepgram key | Voice disabled at startup; the mic button is disabled. |

### Why a dead send half used to be permanent

`_connect_and_pump` awaited **only** the receive loop. If `socket.send()` raised,
the sender task died while the receive loop kept waiting on a socket that was
still open — the library's ping/pong held it up, so nothing raised and nothing
reconnected.

The result was the worst kind of failure: audio kept arriving and being queued,
the bounded queue filled, and frames were dropped. The recogniser went deaf for the
rest of the session **with no log line at all**. The symptom was "the microphone is
listening, the first question worked, and nothing is transcribed after that" —
and toggling the microphone did not help, because the dead task was never replaced.

Both halves are now waited on together and whichever ends is surfaced, so the
failure reaches the reconnect path. A backing-up queue is also logged, because
dropped frames are the only visible sign of a stalled sender.

### Attributing a missing transcript

A frame count cannot tell a working microphone from one delivering silence, and
that distinction decides which layer to investigate. Each listening session now
reports the **peak amplitude** it received:

```
microphone closed (#2); received 1348 frame(s), 43.14s of audio, peak 0.000 -- the browser sent silence
microphone closed (#3); received  109 frame(s),  3.49s of audio, peak 0.712
```

The second line means capture is fine and the audio reached recognition; the first
means the browser delivered nothing but zeros, so the fault is in capture. Sessions
with silence are logged as a warning naming capture as the cause.

## Tuning

| Setting | Default | Effect |
|---|---|---|
| `SURTITLE_STT_API` | `v2` | `v2` = Flux turn detection, `v1` = Nova + endpointing |
| `DEEPGRAM_STT_MODEL` | `flux-general-en` | STT model |
| `SURTITLE_ENDPOINTING_MS` | `300` | Silence before end-of-turn (v1 only) |
| `DEEPGRAM_TTS_MODEL` | `aura-2-thalia-en` | Voice |
| `SURTITLE_TTS_SPEED` | `1.0` | Speaking rate, 0.5–2.0 |
| `SURTITLE_TTS_SAMPLE_RATE` | `24000` | Output sample rate |

To verify the voice path end to end, run `./scripts/run.sh doctor` — it opens brief
sockets to both the STT and TTS endpoints and reports whether the key is accepted,
and prints which configuration files were read.

### If voice fails to connect

Read the warning. A rejected WebSocket is reported with the server's own reason,
which names the problem exactly:

```
Deepgram STT disconnected (HTTP 400: Failed to deserialize query parameters:
Model must have exactly 3 parts separated by hyphens, such as "flux-general-en".
Got: nova-3)
```

That message means a Nova model reached the Flux endpoint — almost always an
overriding `DEEPGRAM_STT_MODEL` from the environment or a `.env` file. `doctor`
lists the files it read, so the source is identifiable rather than guessed at.

| Symptom | Cause | Fix |
|---|---|---|
| `Model must have exactly 3 parts` | A Nova model on `/v2/listen` | Use `flux-general-en`, or set `SURTITLE_STT_API=v1` with `DEEPGRAM_STT_MODEL=nova-3` |
| `Unknown query parameters: …` | A parameter this endpoint does not take | Report it; the parameter sets are pinned by `tests/test_voice_clients.py` |
| `HTTP 401: Invalid credentials` | The key itself | Check the key in Settings or `doctor` |
