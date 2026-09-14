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
const SPEECH_RMS_THRESHOLD = 0.02;
const CONSECUTIVE_SPEECH_FRAMES = 3;
// Ignore input right after playback starts: the speaker tail can trip the
// threshold before echo cancellation has converged.
const SPEECH_GRACE_FRAMES = 6;

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
  }

  handleMessage(event) {
    const data = event.data || {};
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
      if (this._speaking && rms < SPEECH_RMS_THRESHOLD * 0.5) {
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
