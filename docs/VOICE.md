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
3. **The sample rate is read back.** A browser may ignore the requested 24 kHz, so
   the context's actual rate is stored and PCM is decoded at that rate. Assuming
   the requested rate would resample every sample and play the voice at the wrong
   pitch and speed.

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
| TTS socket drops | Reconnect with backoff and re-queue the sentence once, so it is not silently lost. |
| TTS unavailable | The turn still completes and the transcript is complete; only audio is missing. |
| Microphone denied | Text-only mode; the error explains that typing still works. |
| No Deepgram key | Voice disabled at startup; the mic button is disabled. |

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
