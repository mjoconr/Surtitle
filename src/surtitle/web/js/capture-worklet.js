/**
 * Microphone capture with built-in barge-in detection.
 *
 * Runs on the audio thread so capture is never blocked by rendering. Two jobs:
 *
 * 1. Downsample the input to 16 kHz mono PCM16, which is what Deepgram's
 *    linear16 streaming expects, and post it in ~32 ms frames. Frames are
 *    batched rather than posted per render quantum (128 samples) because
 *    thousands of tiny postMessage calls per second starve the main thread.
 *
 * 2. Detect likely user speech and report it immediately. Waiting for the
 *    server to confirm a transcript would make interruption feel sluggish, so
 *    loudness is measured here. It is deliberately conservative — a sustained
 *    threshold, not a single spike — because a false positive truncates the
 *    agent mid-sentence, which is far more annoying than a slightly late
 *    interruption.
 */

const TARGET_SAMPLE_RATE = 16000;
const FRAME_SAMPLES = 512; // ~32 ms at 16 kHz

// Speech must exceed this RMS and then persist for this many frames before it
// counts as the user taking the floor.
//
// These are deliberately conservative. A false positive truncates the agent
// mid-sentence, which is far worse than an interruption arriving a fraction of a
// second late: in a real session the agent's own voice through the speakers
// tripped a 0.02 threshold and cut replies off. 0.05 sustained for four frames is
// roughly 130 ms of speech, which a person talking over the agent easily reaches
// while speaker bleed does not.
const SPEECH_RMS_THRESHOLD = 0.05;
const CONSECUTIVE_SPEECH_FRAMES = 4;
// Ignore input right after playback starts: the speaker tail can trip the
// threshold before echo cancellation has converged.
const SPEECH_GRACE_FRAMES = 12;

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._ratio = sampleRate / TARGET_SAMPLE_RATE;
    this._buffer = new Float32Array(FRAME_SAMPLES);
    this._bufferIndex = 0;
    this._acc = 0;
    this._accCount = 0;
    this._speechFrames = 0;
    this._graceFrames = 0;
    this._speaking = false;
    this._muted = true;

    // Browsers disagree about how a processor receives messages, and getting it
    // wrong fails completely silently: `process()` keeps running, the graph is
    // fine, and not one frame is ever posted.
    //
    // The specification delivers messages to `port.onmessage`. Chrome does not
    // call a `handleMessage` method at all, and Firefox has historically called
    // *only* that. This processor originally defined `handleMessage` alone, so
    // Chrome never delivered the `mute: false` that arms capture, `_muted` stayed
    // at its constructor default, and capture produced zero frames in **every**
    // browser. The ScriptProcessor fallback then quietly took over, which is why
    // voice appeared to work while this path never did.
    //
    // Both entry points are wired, and both are idempotent, so a browser that
    // calls either (or, harmlessly, both) behaves identically.
    this.port.onmessage = (event) => this._onMessage(event.data);
  }

  /** Legacy entry point, still used by some engines. */
  handleMessage(event) {
    this._onMessage(event.data);
  }

  _onMessage(data) {
    data = data || {};
    if (data.type === "mute") {
      this._muted = Boolean(data.value);
      this._speechFrames = 0;
      this._speaking = false;
    } else if (data.type === "playback") {
      // The main thread tells us playback started, to open a grace window.
      if (data.value) this._graceFrames = SPEECH_GRACE_FRAMES;
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;

    const channel = input[0];
    if (!channel) return true;

    if (this._graceFrames > 0) this._graceFrames -= 1;

    if (!this._muted) {
      this._measure(channel);

      for (let i = 0; i < channel.length; i += 1) {
        this._acc += channel[i];
        this._accCount += 1;
        if (this._accCount >= this._ratio) {
          // Mean over the window: cheap anti-aliasing before decimation.
          this._buffer[this._bufferIndex] = this._acc / this._accCount;
          this._bufferIndex += 1;
          this._acc = 0;
          this._accCount = 0;

          if (this._bufferIndex >= FRAME_SAMPLES) {
            this._flush();
          }
        }
      }
    }

    return true;
  }

  _measure(channel) {
    let sum = 0;
    for (let i = 0; i < channel.length; i += 1) sum += channel[i] * channel[i];
    const rms = Math.sqrt(sum / channel.length);

    if (rms > SPEECH_RMS_THRESHOLD && this._graceFrames === 0) {
      this._speechFrames += 1;
      if (this._speechFrames >= CONSECUTIVE_SPEECH_FRAMES && !this._speaking) {
        this._speaking = true;
        this.port.postMessage({ type: "speech-start" });
      }
    } else {
      this._speechFrames = 0;
      // Require a real pause before re-arming, so one utterance cannot fire
      // several barge-ins in a row.
      if (this._speaking && rms < SPEECH_RMS_THRESHOLD * 0.6) {
        this._speaking = false;
      }
    }

    this.port.postMessage({ type: "level", value: Math.min(1, rms * 6) });
  }

  _flush() {
    const pcm = new Int16Array(FRAME_SAMPLES);
    for (let i = 0; i < FRAME_SAMPLES; i += 1) {
      const clamped = Math.max(-1, Math.min(1, this._buffer[i]));
      // Asymmetric scaling: -1 maps to -32768, +1 to 32767.
      pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    this.port.postMessage({ type: "audio", buffer: pcm.buffer }, [pcm.buffer]);
    this._bufferIndex = 0;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
