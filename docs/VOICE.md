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

The receive loop parses defensively. An exact `type` match is tried first, and any
payload carrying turn-shaped fields (`transcript`, `words`, `end_of_turn`) is routed
to the turn handler as well. A renamed server event therefore degrades to "captions
still work" instead of silently dropping every transcript.

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
sockets to both the STT and TTS endpoints and reports whether the key is accepted.
