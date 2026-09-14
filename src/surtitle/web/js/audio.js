/**
 * Microphone capture and spoken-audio playback.
 *
 * Both live in the browser, which is what lets macOS and Windows share one code
 * path and keeps the Python process away from audio devices entirely.
 *
 * Playback uses Web Audio rather than an <audio> element: Deepgram streams raw
 * linear16 PCM with no container, so there is nothing for a media element to
 * decode. Scheduling AudioBuffers directly also gives gapless concatenation and
 * an instant stop() for barge-in, which an element cannot do mid-buffer.
 */

const WORKLET_URL = "/static/js/capture-worklet.js";

export class Capture {
  constructor({ onAudio, onLevel, onSpeechStart }) {
    this.onAudio = onAudio;
    this.onLevel = onLevel;
    this.onSpeechStart = onSpeechStart;
    this.context = null;
    this.stream = null;
    this.node = null;
    this.source = null;
    this.active = false;
  }

  get supported() {
    return Boolean(
      navigator.mediaDevices &&
        navigator.mediaDevices.getUserMedia &&
        window.AudioContext &&
      this.contextSupported
    );
  }

  get contextSupported() {
    return typeof AudioWorkletNode !== "undefined";
  }

  async start() {
    if (this.active) return true;

    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        // These three are the difference between a usable voice agent and one
        // that transcribes its own output.
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });

    this.context = new AudioContext();
    // Deepgram expects 16 kHz linear16; the worklet resamples to it.
    await this.context.audioWorklet.addModule(WORKLET_URL);

    this.source = this.context.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.context, "capture-processor", {
      numberOfInputs: 1,
      numberOfOutputs: 0,
    });

    this.node.port.onmessage = (event) => this._handle(event.data);
    this.source.connect(this.node);

    this.active = true;
    this.setMuted(false);
    return true;
  }

  _handle(message) {
    if (!message) return;
    if (message.type === "audio" && this.onAudio) {
      this.onAudio(new Uint8Array(message.buffer));
    } else if (message.type === "level" && this.onLevel) {
      this.onLevel(message.value);
    } else if (message.type === "speech-start" && this.onSpeechStart) {
      this.onSpeechStart();
    }
  }

  setMuted(muted) {
    if (this.node) this.node.port.postMessage({ type: "mute", value: muted });
  }

  /** Tell the worklet playback began, so it can ignore the speaker tail. */
  notifyPlayback(playing) {
    if (this.node) this.node.port.postMessage({ type: "playback", value: playing });
  }

  async stop() {
    this.active = false;
    if (this.node) {
      this.node.port.onmessage = null;
      this.node.disconnect();
      this.node = null;
    }
    if (this.source) {
      this.source.disconnect();
      this.source = null;
    }
    if (this.stream) {
      for (const track of this.stream.getTracks()) track.stop();
      this.stream = null;
    }
    if (this.context) {
      await this.context.close().catch(() => {});
      this.context = null;
    }
  }
}

export class Playback {
  constructor({ onStart, onIdle, sampleRate = 24000 }) {
    this.onStart = onStart;
    this.onIdle = onIdle;
    this.sampleRate = sampleRate;
    this.context = null;
    this.gain = null;
    this.sources = new Set();
    this.nextTime = 0;
    this.playing = false;
    // Small cushion so consecutive sentences join without a click.
    this.leadTime = 0.06;
  }

  _ensureContext() {
    if (!this.context) {
      this.context = new AudioContext({ sampleRate: this.sampleRate });
      this.gain = this.context.createGain();
      this.gain.connect(this.context.destination);
    }
    if (this.context.state === "suspended") {
      this.context.resume().catch(() => {});
    }
    return this.context;
  }

  /** Queue one chunk of PCM16 audio for immediate playback. */
  push(pcmBytes) {
    if (!pcmBytes || pcmBytes.length < 2) return;

    const context = this._ensureContext();
    const sampleCount = Math.floor(pcmBytes.length / 2);
    const view = new DataView(pcmBytes.buffer, pcmBytes.byteOffset, pcmBytes.byteLength);

    const buffer = context.createBuffer(1, sampleCount, this.sampleRate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < sampleCount; i += 1) {
      channel[i] = view.getInt16(i * 2, true) / 32768;
    }

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain);

    const now = context.currentTime;
    // If the queue has drained, start just ahead of now; otherwise append.
    const startAt = Math.max(now + this.leadTime, this.nextTime);
    source.start(startAt);
    this.nextTime = startAt + buffer.duration;

    if (!this.playing) {
      this.playing = true;
      if (this.onStart) this.onStart();
    }

    this.sources.add(source);
    source.onended = () => {
      this.sources.delete(source);
      if (this.sources.size === 0) this._finish();
    };
  }

  _finish() {
    if (!this.playing) return;
    this.playing = false;
    this.nextTime = 0;
    if (this.onIdle) this.onIdle();
  }

  /**
   * Stop everything immediately.
   *
   * This runs synchronously on barge-in: the user must hear silence the moment
   * they start talking, not after the current sentence finishes.
   */
  stop() {
    for (const source of this.sources) {
      try {
        source.onended = null;
        source.stop();
      } catch {
        /* already stopped */
      }
    }
    this.sources.clear();
    this.nextTime = 0;
    this._finish();
  }

  setVolume(value) {
    if (this.gain) this.gain.gain.value = Math.max(0, Math.min(1, value));
  }

  async close() {
    this.stop();
    if (this.context) {
      await this.context.close().catch(() => {});
      this.context = null;
      this.gain = null;
    }
  }
}
