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
    if (this.active) return { running: true };

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

    // One context per page, reused across mic toggles. Creating a fresh context
    // each time eventually hits the browser's per-page AudioContext limit and
    // then fails outright.
    if (!this.context) {
      this.context = new AudioContext();
      this.moduleLoaded = this.context.audioWorklet.addModule(WORKLET_URL);
    }

    // An AudioContext created outside a user gesture starts suspended, and a
    // suspended context never runs the worklet — so nothing is captured and the
    // level meter never moves. That looks exactly like a muted microphone.
    // Resume first, and report the state so the caller can tell the user.
    if (this.context.state === "suspended") {
      await this.context.resume().catch(() => {});
    }
    // Deepgram expects 16 kHz linear16; the worklet resamples to it.
    await this.moduleLoaded;

    // The awaited module load can outlive the activation that started it, so
    // check again once the worklet is ready.
    if (this.context.state === "suspended") {
      await this.context.resume().catch(() => {});
    }

    this.source = this.context.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.context, "capture-processor", {
      numberOfInputs: 1,
      numberOfOutputs: 0,
    });

    this.node.port.onmessage = (event) => this._handle(event.data);
    this.source.connect(this.node);

    // Count what actually arrives, so "the microphone is open" can be told apart
    // from "audio is reaching the application".
    this.framesReceived = 0;
    this.maxLevel = 0;

    this.active = true;
    this.setMuted(false);
    return { running: this.context.state === "running" };
  }

  /** Diagnostics for the caller after opening the microphone. */
  get status() {
    return {
      running: Boolean(this.context && this.context.state === "running"),
      framesReceived: this.framesReceived,
      maxLevel: this.maxLevel,
      // True when the worklet has never produced a buffer, which means capture is
      // not running at all rather than the room merely being quiet.
      silent: this.framesReceived === 0,
    };
  }

  _handle(message) {
    if (!message) return;
    if (message.type === "audio") {
      this.framesReceived = (this.framesReceived || 0) + 1;
      if (this.onAudio) this.onAudio(new Uint8Array(message.buffer));
    } else if (message.type === "level") {
      this.maxLevel = Math.max(this.maxLevel || 0, message.value || 0);
      if (this.onLevel) this.onLevel(message.value);
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
    // The context is intentionally left open: it is reused on the next start, and
    // closing it here is what previously forced a new one per toggle.
  }

  /** Release the capture context entirely. Only for page teardown. */
  async dispose() {
    await this.stop();
    if (this.context) {
      await this.context.close().catch(() => {});
      this.context = null;
      this.moduleLoaded = null;
    }
  }
}

export class Playback {
  constructor({ onStart, onIdle, onBlocked, sampleRate = 24000 }) {
    this.onStart = onStart;
    this.onIdle = onIdle;
    // Called when audio arrives but the output context is not running, so the
    // user is told instead of hearing nothing.
    this.onBlocked = onBlocked;
    this.sampleRate = sampleRate;
    this.context = null;
    this.gain = null;
    this.sources = new Set();
    this.nextTime = 0;
    this.playing = false;
    // Set by unlock(); false means playback will be silent until a gesture.
    this.unlocked = false;
    this._blockedReported = false;
    // Small cushion so consecutive sentences join without a click.
    this.leadTime = 0.06;
  }

  /**
   * Create and start the audio context *inside a user gesture*.
   *
   * This matters more than it looks. An AudioContext created outside a user
   * gesture starts suspended, and `resume()` then rejects because activation has
   * been consumed — leaving playback silently doing nothing. Creating the context
   * on the mic click (a real gesture) means it is already running by the time the
   * first audio chunk arrives over the socket.
   */
  async unlock() {
    try {
      const context = this._ensureContext();
      if (context.state !== "running") {
        await context.resume();
      }
      // A near-silent buffer proves the graph really reaches the output device;
      // some audio stacks need something to have been played before they open.
      const primer = context.createBuffer(1, 1, context.sampleRate);
      const source = context.createBufferSource();
      source.buffer = primer;
      source.connect(context.destination);
      source.start();
      this.unlocked = context.state === "running";
      return this.unlocked;
    } catch {
      this.unlocked = false;
      return false;
    }
  }

  /** True once the output context is actually running. */
  get outputReady() {
    return Boolean(this.context && this.context.state === "running");
  }

  _ensureContext() {
    if (!this.context) {
      // The browser may ignore the requested rate; read back what it gave us and
      // decode at that rate, or every sample would be resampled and the voice
      // would play at the wrong pitch and speed.
      this.context = new AudioContext({ sampleRate: this.sampleRate });
      this.sampleRate = this.context.sampleRate;
      this.gain = this.context.createGain();
      this.gain.connect(this.context.destination);
    }
    if (this.context.state === "suspended") {
      // Best-effort recovery outside a gesture; `unlock()` is the reliable path.
      this.context.resume().catch(() => {});
    }
    return this.context;
  }

  /** Queue one chunk of PCM16 audio for immediate playback. */
  push(pcmBytes) {
    if (!pcmBytes || pcmBytes.length < 2) return;

    const context = this._ensureContext();
    if (context.state !== "running") {
      // Surface it once per turn rather than per chunk.
      if (!this._blockedReported && this.onBlocked) {
        this._blockedReported = true;
        this.onBlocked();
      }
    }

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
    this._blockedReported = false;
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
