# Voice pipeline

How audio gets in, how it gets out, and why interrupting works.

## Engines

Each direction has two implementations, chosen independently with
`SURTITLE_STT_BACKEND` and `SURTITLE_TTS_BACKEND`:

| | `deepgram` (default) | `local` |
|---|---|---|
| Where it runs | hosted WebSocket | ONNX in this process |
| Needs a key | yes | no |
| Needs the network | yes | no |
| Cost | per minute | none |
| Install | an API key | `uv sync --extra voice-local` + `surtitle models download` (~90 MB for the default pair) |
| Turn detection | Flux — *contextual*, uses what you said | trailing silence, plus a completeness heuristic |
| Word errors | low | depends on the model; measured below |

They are independent, so "local ears, hosted voice" is supported and is usually
the sensible combination — recognition is where the privacy and cost pressure is.

> **Diagnosing a local engine that will not start.** `surtitle models status` and
> `models verify` inspect the downloaded model *files* only — they never import
> `sherpa_onnx`. So they report every model "installed" even when the
> `voice-local` extra is missing from the active venv, and the session then fails
> with no `local STT ready` line in `surtitle.log`. `scripts/run.sh` does not
> repair this: with an existing `.venv` it launches without syncing, and its
> first-run bootstrap runs `uv sync --inexact` with no extra. The fix is an
> explicit `uv sync --extra voice-local`, then restart the server.

## What the two engines are, measured

Measured on the development machine (2019 Intel i9, macOS, no GPU, CPU only) with
the shipped default models. These are the honest numbers, and they are why the
hosted engine is still the default.

| Metric | Deepgram | Local |
|---|---|---|
| Recogniser real-time factor | n/a (streamed) | **0.05–0.08** (default model) |
| Recogniser model load | n/a | **~3 s**, once per session |
| Word error, far-field spontaneous speech | — | **~32%** (default), see below |
| Word error, close read speech | — | **~6%** (default) |
| First spoken audio | ~0.3–0.7 s (network RTT) | **0.7–1.9 s** (synthesis time) |
| Voice real-time factor | n/a | **0.63–0.70** |
| Resident memory | negligible | ~300 MB peak (default STT), ~210 MB (large STT) |
| Disk | none | ~90 MB for the default pair; ~205 MB for every model |

Two things follow, and both matter more than the raw figures:

- **Local recognition is genuinely real-time.** At 0.05–0.08 RTF a 16-second
  utterance decodes in about a second, spread across the utterance rather than
  collapsed at its end, so captions appear while you speak.
- **Local speech starts slowly but produces faster than it plays.** Synthesising a
  sentence costs 0.63–0.70 of that sentence's duration, so synthesis stays *ahead*
  of playback and never underruns — but the first sentence still takes 0.7–1.9 s to
  begin, because there is no server generating it in parallel with the model that
  is writing the reply.

Accuracy is where the gap is real, and *which* local model you have matters far
more than the fact that it is local. Over 40 AMI utterances recorded on a single
distant microphone — a room, several speakers, spontaneous speech, which is what
this application is actually for — and 40 LibriSpeech test-other clips, which are
read aloud into a close microphone:

| Local STT model | Far-field, spontaneous | Close, read | 15 dB noise | RTF |
|---|---|---|---|---|
| `kroko-2025-08-06` (**default**) | **31.6%** | 6.4% | **27.3%** | 0.08 |
| `zipformer-en-2023-06-26` | 98.5% | **5.1%** | 55.5% | 0.16 |

(A third entry, `zipformer-en-20M`, was removed after the same measurement: 98.8%
far-field, 24.3% on close read speech, and no faster than the default — 44 MB in
every install for a model with no reason to be recommended.)

The old default was trained on read speech and behaves like it: across a room it
returned *nothing at all* for most utterances, and normalising the level only got
it to 55%. From a real session on it — "repeat back to me" heard as "THE PATE BACK
TO ME", "gallon" as "GALLUM", "something went wrong" as "SOMETHING LENT WARM" —
while Deepgram, given the same audio, heard all three correctly. Kroko is trained
on a much larger and more varied corpus of real recordings, and it is the default
for that reason. It also **punctuates and capitalises**, which the older models do
not: that is not cosmetic, because it is what gives the turn heuristic a real
end-of-thought signal.

The two alternatives remain selectable because they are not pointless — the large
one is the more accurate of the three on close, clearly spoken read speech — but
neither should be chosen for a microphone across a room.

## Local turn detection, and why it is weaker

Flux decides the end of a turn from *what was said*. A streaming zipformer offers
no such judgement, so `voice/local_stt.py` grades a timer by what the transcript
looks like:

1. **Trailing silence** — `SURTITLE_LOCAL_EOT_SILENCE_MS` (default 800 ms). The
   floor: a transcript that reads as complete still waits this long.
2. **A completion heuristic** — the transcript tells the timer how much benefit of
   the doubt to give. A trailing function word ("and", "but", "the", "because")
   earns the full `SURTITLE_LOCAL_EOT_EXTEND_MS` (default 1200 ms); anything else
   that cannot be *shown* to be finished earns part of it. Terminal punctuation is
   the one positive sign a thought closed — and the default model punctuates, so
   that sign is real rather than theoretical. The older model emits no
   punctuation, and for it nearly everything looks unfinished.
3. **A backstop, not a turn rule** — `SURTITLE_LOCAL_MAX_UTTERANCE_MS` (default
   60 s). A turn ends when the thought sounds finished, and a clock cannot know
   that, so this only bounds a speaker who never pauses: it fires on the first real
   pause *after* that much continuous speech, and never while audio is still
   arriving. At 20 s it used to close a turn on the clock alone, mid-word.

**No rule may act on less than 800 ms of silence**, whatever `SURTITLE_LOCAL_EOT_SILENCE_MS`
says. A streaming transducer emits a word only once it has heard the audio that
follows it, so the silence after a sentence is also what flushes the last word of
it. Measured on one clip:

```
0.32 s of trailing silence   "I'M FROM THE CUTTER LYING OFF THE COA"
0.80 s of trailing silence   "I'M FROM THE CUTTER LYING OFF THE COAST"
```

A shorter window therefore does not make the agent answer sooner — it truncates
the end of every sentence, which reads as a recognition failure rather than as a
setting. The shipped default is exactly the floor, so the floor changes nothing
until somebody lowers it.

The asymmetry is deliberate: a false positive costs a slightly longer pause before
the agent answers; a false negative cuts you off mid-sentence. So the heuristic
leans toward waiting, and it still cannot tell "and then…" from "and that is all."
If that matters, use the hosted engine for recognition and the local one only for
speech.

## A turn boundary is not a sentence boundary

Neither engine knows where your sentence ends — Flux infers it, the local
heuristic guesses it — and both get it wrong in the same direction. Measured on a
real session:

```
11:03:59  utterance (64.03s): 'So we could work out a simulation'
11:04:00  utterance (65.60s): 'of this.'
```

Two turns, 1.5 seconds apart, half a sentence each. The agent started answering
the first and the second cancelled it, so the user got a reply to a fragment and
none to the question.

`SURTITLE_STT_MERGE_HOLD_MS` (default 1200 ms) fixes this from the session side,
where it applies to both engines: when a turn ends the text is held, and anything
arriving during the hold is merged into one utterance before the model sees it. The
recogniser keeps running throughout — the hold defers the *commit* rather than
blocking the stream, without which the continuation it is waiting for could never
arrive. `SURTITLE_STT_MERGE_MAX_MS` (default 20 s) bounds the wait so a speaker who
never pauses still gets an answer. Set the hold to 0 to commit the instant the
engine declares the turn over.

The cost is up to 1.2 s of extra latency on every spoken turn. That is the trade
the merging makes, and it is the reason the value is a setting rather than a
constant.

## Echo suppression, and how it fails

While the agent's voice is playing, transcripts are discarded as echo. Browser
echo cancellation does most of this; suppression is the safety net for devices
where it is unavailable or imperfect. It is armed when speech starts and released
when the synthesiser reports that it has finished.

Both halves of that have failed in production, and the failure mode is the worst
one available: **every transcript is discarded, so the microphone appears dead**
while the log stays quiet. In the session that produced this section the counter
reported 25 discarded Flux transcripts in one burst — including the complete
sentence "The question is, uh, is the slower speed losing more than we…" — tens of
seconds after playback had stopped.

Two things now bound it:

- `_watch_echo_suppression` releases it once no audio has been sent for longer
  than the audio last sent could still be playing, plus
  `SURTITLE_ECHO_SUPPRESSION_MAX_MS` (default 1500 ms). The bound is measured
  against the audio rather than a flat clock, so an ordinary pause between
  sentences does not release it and let the agent hear itself.
- The UI is told whenever suppression changes, so the microphone reads
  "Listening (agent speaking)" rather than a bare "Listening" that claims to be
  hearing you when it is not.

Lifting it this way logs a warning naming how many transcripts were lost, because
the alternative — silence — is what made this take so long to find.

sherpa-onnx ships its own endpoint rules (`rule1`/`rule2`/`rule3`). They are
deliberately **disabled** (`enable_endpoint_detection=False`) because they fire
inside the recogniser, before this module can apply the extension — using both
would mean the shorter rule won.

## Partial results are cumulative

The local recogniser re-emits the whole utterance for the current stream, and the
engine forwards an update only when the text actually changes: while you pause it
repeats itself every 320 ms, and forwarding that would put ~30 identical caption
updates a second on the wire.

That shape — **replace, never append** — is exactly what `Session._accumulate`
already expects from Flux, so no session-side merging logic differs between the
engines. Whether the text arrives upper-cased or punctuated is up to the model:
the default one produces ordinary sentences, the older ones produce capitals with
no punctuation at all.

## Models

Downloading is explicit and consented, never a side effect of starting the app:

```bash
surtitle models list       # what is installed, and what it would cost
surtitle models download   # fetch what is missing (asks first)
surtitle models verify     # re-check every installed file's checksum
```

Three English recognisers are registered — the default Kroko model plus two
older zipformers, compared in the table above — along with one Piper voice.
The installer fetches all of them; `SURTITLE_LOCAL_STT_MODEL` (or the Settings
screen) chooses which one runs. The quoted download figure counts only what is
actually written: the large model's archive also contains a 260 MB fp32 encoder
that is deliberately skipped.

Everything lands in `<app data>/models` (`SURTITLE_MODELS_DIR` overrides),
which is why updating or replacing the application never re-downloads them, and
why deleting the application does not delete them.

Every file is pinned by SHA-256 and checked before it is installed, because a
truncated ONNX file does not raise — it produces garbage transcripts, which reads
like a bad model rather than a bad download. Installation stages into a private
directory and renames into place, so an interrupted download can never be mistaken
for a complete one.

The Piper voice archive ships espeak-ng data for every language in the world
(19 MB); the installer keeps the files an English voice actually needs, taking the
TTS install from 37 MB to 19 MB. Those files are pinned too, because espeak-ng
prints an error and produces **no audio** rather than raising when one is missing,
so the failure looks like a broken voice instead of a missing file.

## The path, per engine

```text
microphone
   │  getUserMedia({echoCancellation, noiseSuppression, autoGainControl})
   ▼
AudioWorklet  ── downsample to 16 kHz mono ──► PCM16 frames (~32 ms)
   │                                                 │
   │  loudness estimate (barge-in)                   │ binary frame 0x01
   ▼                                                 ▼
audio thread / main thread                        WebSocket
                                                     │
                     ┌───────────────────────────────┴────────────────────────┐
                     ▼                                                        ▼
       Deepgram /v2/listen (Flux)                        sherpa-onnx OnlineRecognizer
       interim ● final ● EndOfTurn ──┐                   (320 ms batches, one thread)
                                     │                   interim ● silence timer ──┐
                                     └──────────────────┬─────────────────────────┘
                                                        ▼
                                                 agent turn starts
                                                        │
                                            DeepSeek streams tokens
                                                        │
                                            SpeakParser → <say> sentences
                                                        │
                     ┌──────────────────────────────────┴─────────────────────┐
                     ▼                                                        ▼
       Deepgram /v1/speak (Aura)                        sherpa-onnx OfflineTts (VITS)
       linear16 PCM ──────────────┐                     22050 Hz → resampled to 24000
                                   └──────────────────┬───────────────────────────┘
                                                      ▼
                                       WebSocket binary frame → browser
                                                      │
                                                      ▼
                                    Web Audio schedules AudioBuffers
```

**The browser contract does not change with the engine.** Capture is 16 kHz mono
PCM16 either way, and playback is PCM16 either way. The local voice natively
synthesises at 22050 Hz, so the engine resamples to the configured
`SURTITLE_TTS_SAMPLE_RATE` (24000) rather than announcing a new rate —
`Playback.setServerRate()` refuses a change once the audio graph exists, so a
mid-session change would be silently ignored by the client.

## Barge-in with a local voice

The hosted engine discards audio for interrupted text by **closing the socket**:
Deepgram has already synthesised it, and leaving the connection open means the
backlog arrives later and sounds like the agent ignoring you.

A local engine has no socket, and an ONNX call already running cannot be
cancelled. The equivalent guarantee is enforced by *discarding the result*:
`barge_in()` increments the generation counter, and `_produce` re-checks it after
synthesis returns and raises `BargeIn`, so audio for cancelled text never reaches
the browser. Queued sentences are dropped before they are synthesised at all.
That is tested directly (`test_barge_in_discards_in_flight_synthesis`).

## Speech in (Deepgram)

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

### Barge-in windows are in milliseconds, not render quanta

`process()` runs once per render quantum, fixed by the specification at 128
sample-frames — about 2.7 ms at 48 kHz. The detector's thresholds were written as
if a quantum were a 32 ms audio frame, so the intended "130 ms of sustained speech"
was really 11 ms and the intended 400 ms grace window was 32 ms.

That mattered because the client silences playback on its own VAD **before** the
server validates the interruption: the server would log "ignoring an interruption
with no transcribed speech behind it" while the audio had already been cut. The
agent's own voice through the speakers was enough, so replies stopped and restarted
— reported as the audio breaking up. Measured in a browser against the shipped
processor:

| Burst | Interrupts? (before) | Interrupts? (now) |
|---|---|---|
| 50 ms loud | yes | no |
| 100 ms loud | yes | no |
| 400 ms loud | yes | yes |
| 400 ms quiet | no | no |

The level meter was also posting one message per quantum, roughly 375 a second,
each one a DOM write on the main thread — which delays playback scheduling and
shows up as gaps. It is throttled to ~20 a second.

A progress line ("still working on this") is deliberately **not** a final
utterance: marking it final makes the synthesiser report that speaking has
finished, which releases echo suppression mid-turn.


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
| No Deepgram key | If an engine is set to `deepgram`, that direction is disabled and the reason is reported in the `ready` payload and the Activity panel. A `local` direction is unaffected — the two are built independently. |
| Local extra not installed | The direction is disabled with the `uv sync --extra voice-local` instruction, at startup rather than on the first click. |
| Local model missing or corrupt | Reported by filename, with `surtitle models download <key>` as the fix. A partly installed model reads as *damaged*, not as "not installed". |
| Local model load fails at runtime | Reported once and the session continues text-only; the recogniser does not reconnect-loop, because a missing ONNX runtime is not a transient failure. |
| Local decode raises | Reported once, engine stops; the turn-based paths continue to work for typing. |
| Local audio queue backs up | Oldest batch dropped and counted; a stalled decoder is otherwise invisible. |
| Step budget exhausted | The turn stops, the reason is **spoken** as well as shown, and the log records it. |

### Failures are spoken, not just displayed

A voice-first user is listening, not reading. Every failure used to be displayed
silently, and a turn cut short by the step budget (`SURTITLE_MAX_STEPS`,
default 24) ended with the agent saying nothing at all — which is indistinguishable
from the agent having stopped. It was reported exactly that way: the agent worked
for eight minutes making 33 tool calls, exhausted its budget, wrote no assistant
message, and left no trace in the log either.

Each failure now gets one short spoken sentence saying what happened and what to
do next ("I ran out of steps before finishing that. Ask me to carry on, or give me
a smaller piece of it."), while the screen keeps the detail. The step limit is also
logged, and it is adjustable in Settings → Agent.

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

### Echo suppression must be released, or the mic looks broken

`_on_speaking_started` turns on echo suppression so the agent does not transcribe
its own voice. **Nothing turned it off.** `on_finished` was reachable only through
`TextToSpeech.stop()`, so after the agent's very first reply `is_speaking` stayed
true for the rest of the session.

`Session.handle_mic(open=True)` derives suppression from `is_speaking`, so every
later press of the microphone button **re-armed** suppression rather than clearing
it. Every transcript from then on was discarded as the agent's own voice — and
because the drop was a debug line, the log showed nothing at all.

Symptoms, all of which were reported:

- the first question works, and the microphone then appears dead;
- toggling the microphone does not help;
- audio is demonstrably arriving and loud — the log recorded `peak 0.996`;
- it happens in every browser, because none of it is browser-specific.

`Session._run_turn` now calls `TextToSpeech.end_of_turn()` when the model has
finished producing text. The synthesiser queues a turn-boundary marker and reports
`on_finished` once every queued sentence has been spoken, which releases
suppression. Both transitions are now logged (`speaking started; echo suppression
on` / `speaking finished; echo suppression released`), and a suppressed transcript
is logged rather than dropped in silence.

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

### A completed turn must show something

An answer can be produced and stored while its events never reach the page: a
dropped or half-dead socket loses them silently, and `_safe_send` suppresses every
send error, so the server carries on working.

Reported as "it finished and gave me no result and I had to prompt it again". The
database held a 7,072-character answer for that turn, written when the agent
finished — the work was done and only the delivery failed.

The client could not tell "the agent produced nothing" from "the events never
arrived", so it showed an empty turn. On `done`, a turn with nothing rendered is now
reconciled against the stored transcript and the reply is displayed, with a line in
the Activity panel saying why it appeared late. The recovery only accepts an answer
newer than the most recent question, so a stale reply is never presented as the
answer to a new one.

## Tuning

| Setting | Default | Effect |
|---|---|---|
| `SURTITLE_STT_BACKEND` | `deepgram` | `deepgram` or `local` |
| `SURTITLE_TTS_BACKEND` | `deepgram` | `deepgram` or `local` (independent) |
| `SURTITLE_STT_API` | `v2` | Deepgram only: `v2` = Flux, `v1` = Nova + endpointing |
| `DEEPGRAM_STT_MODEL` | `flux-general-en` | Deepgram STT model |
| `SURTITLE_ENDPOINTING_MS` | `300` | Silence before end-of-turn (Deepgram v1 only) |
| `DEEPGRAM_TTS_MODEL` | `aura-2-thalia-en` | Deepgram voice |
| `SURTITLE_TTS_SPEED` | `1.0` | Speaking rate, 0.5–2.0 (both engines) |
| `SURTITLE_TTS_SAMPLE_RATE` | `24000` | Output sample rate |
| `SURTITLE_LOCAL_STT_MODEL` | `streaming-zipformer-en-2023-06-26` | Local recogniser |
| `SURTITLE_LOCAL_TTS_MODEL` | `vits-piper-en_US-lessac-medium` | Local voice |
| `SURTITLE_LOCAL_EOT_SILENCE_MS` | `800` | Local: silence before a turn ends |
| `SURTITLE_LOCAL_EOT_EXTEND_MS` | `1200` | Local: longer wait after "and", "the", … |
| `SURTITLE_LOCAL_MAX_UTTERANCE_MS` | `20000` | Local: hard ceiling on one turn |
| `SURTITLE_MODELS_DIR` | `<data dir>/models` | Where local models are cached |

To verify the voice path end to end, run `./scripts/run.sh doctor` — it opens brief
sockets to both the Deepgram endpoints (when selected) and reports whether the key
is accepted, loads the local model and synthesises or decodes one frame (when
selected), and prints which configuration files were read.

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
