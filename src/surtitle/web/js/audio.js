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
  /**
   * Microphone capture with two interchangeable backends.
   *
   * The AudioWorklet path is preferred, but it has a failure mode that produces
   * no error at all: `addModule()` resolves on a *suspended* AudioContext, the
   * node constructs, and `process()` is simply never driven — so nothing is ever
   * captured. Observed directly: context `suspended`, frames `0`.
   *
   * Two defences, because this is load-bearing:
   *
   * 1. The context is resumed and then **verified** running before the worklet is
   *    attached. A resume() promise resolving is not the same as the context
   *    running.
   * 2. If no frames arrive shortly after starting, capture automatically falls
   *    back to a ScriptProcessorNode, which is deprecated but universally
   *    supported and depends on no module loading at all. Falling back to silence
   *    is not acceptable, and the fallback is invisible to the caller.
   */
  constructor({ onAudio, onLevel, onSpeechStart, onBackendChange, deviceId = "" }) {
    this.onAudio = onAudio;
    this.onLevel = onLevel;
    this.onSpeechStart = onSpeechStart;
    this.onBackendChange = onBackendChange;
    // Preferred input device. Empty means "whatever the browser defaults to",
    // which on a machine with virtual audio devices is a coin toss.
    this.deviceId = deviceId || "";
    this.context = null;
    this.stream = null;
    this.node = null;
    this.source = null;
    this.active = false;
    this.backend = "none";
    this.framesReceived = 0;
    this.maxLevel = 0;
    this._upgradeTimer = null;
    this._speechFrames = 0;
    this._speaking = false;
    this._graceUntil = 0;
  }

  get supported() {
    return Boolean(
      navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.AudioContext,
    );
  }

  /**
   * Resume the context and wait until it is genuinely running.
   *
   * Returns true only when the context reports `running`. A resolved resume() is
   * not sufficient evidence: the context can still be suspended afterwards, which
   * is exactly how the worklet silently captured nothing.
   */
  async _ensureRunning() {
    if (!this.context) return false;
    for (let attempt = 0; attempt < 12; attempt += 1) {
      if (this.context.state === "running") return true;
      try {
        await this.context.resume();
      } catch {
        /* retry below */
      }
      if (this.context.state === "running") return true;
      await new Promise((resolve) => setTimeout(resolve, 60));
    }
    return this.context.state === "running";
  }

  /** Close the current context so the next start builds a clean one. */
  async _discardContext() {
    const context = this.context;
    this.context = null;
    if (context) {
      // Detach before closing: closing a context with live nodes attached can throw.
      try {
        if (this.node) this.node.disconnect();
        if (this.sink) this.sink.disconnect();
        if (this.source) this.source.disconnect();
      } catch {
        /* already detached */
      }
      await context.close().catch(() => {});
    }
  }

  async start() {
    // Every start is a clean start. Previously a leftover `active` flag made this
    // a silent no-op, so a second attempt did nothing at all — no stream, no
    // frames, no error — which is indistinguishable from a muted microphone and
    // survived toggling the mic (the toggle simply re-entered this early return).
    // Tearing down first removes that failure mode rather than diagnosing it.
    if (this.active || this.stream || this.node) {
      console.info("[surtitle] start requested while already running; restarting cleanly");
      await this.stop();
    }

    const constraints = {
      audio: {
        // These three are the difference between a usable voice agent and one
        // that transcribes its own output.
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    };
    // `exact` would reject outright when a device has been unplugged since it was
    // chosen, so prefer the requested device and fall back to the default rather
    // than refusing to start.
    if (this.deviceId) constraints.audio.deviceId = { ideal: this.deviceId };

    try {
      this.stream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (error) {
      if (this.deviceId && (error.name === "OverconstrainedError" || error.name === "NotFoundError")) {
        console.warn("[surtitle] chosen input unavailable; using the default", error.name);
        this.deviceId = "";
        delete constraints.audio.deviceId;
        this.stream = await navigator.mediaDevices.getUserMedia(constraints);
      } else {
        throw error;
      }
    }

    // Chromium suspends or closes a context once its input ends, and a suspended
    // context never drives the worklet — so the *second* listening session
    // captured nothing while the first worked. Firefox does not do this, which is
    // why identical code behaved differently between browsers.
    //
    // Reviving a stale context is unreliable, so one that is not running is
    // discarded and rebuilt. A running context is reused, which keeps the number
    // of live contexts well inside the browser's per-page limit during toggling.
    if (this.context && (this.context.state === "closed" || !(await this._ensureRunning()))) {
      await this._discardContext();
    }
    if (!this.context) {
      this.context = new AudioContext();
    }

    const running = await this._ensureRunning();
    this._micOpens = (this._micOpens || 0) + 1;
    console.info(
      `[surtitle] capture start #${this._micOpens}: context=${this.context.state} ` +
        `sampleRate=${this.context.sampleRate} device=${this.deviceId || "default"}`,
    );
    this.source = this.context.createMediaStreamSource(this.stream);

    this.framesReceived = 0;
    this.maxLevel = 0;
    this.active = true;

    // Try the worklet first, but only trust it once it has produced a frame.
    let workletAttached = false;
    if (running) {
      workletAttached = await this._attachWorklet();
    }

    if (workletAttached) {
      this.backend = "worklet";
      // If the worklet never delivers, switch to the fallback rather than staying
      // silent. This also covers a context that suspends immediately after start.
      this._upgradeTimer = setTimeout(async () => {
        if (!this.active || this.backend !== "worklet" || this.framesReceived > 0) return;
        // Re-check the context first: a suspended context explains zero frames and
        // is cheap to fix, whereas the fallback cannot run on a suspended context
        // either.
        const stillRunning = await this._ensureRunning();
        if (!this.active) return;
        if (this.framesReceived > 0) return;
        console.warn(
          `[surtitle] worklet produced no frames (context=${this.context.state}, ` +
            `resumed=${stillRunning}); switching to ScriptProcessor`,
        );
        this._attachScriptProcessor();
      }, 900);
    } else {
      this._attachScriptProcessor();
    }

    this.setMuted(false);
    return { running, backend: this.backend };
  }

  async _attachWorklet() {
    try {
      await this.context.audioWorklet.addModule(WORKLET_URL);
      // Re-verify: the awaited module load can outlive activation, and this is
      // the exact point where the worklet previously ended up inert.
      if (!(await this._ensureRunning())) return false;

      this.node = new AudioWorkletNode(this.context, "capture-processor", {
        numberOfInputs: 1,
        numberOfOutputs: 0,
      });
      this.node.port.onmessage = (event) => this._handleWorklet(event.data);
      this.source.connect(this.node);
      return true;
    } catch (error) {
      console.warn("[surtitle] AudioWorklet unavailable:", error && error.message);
      return false;
    }
  }

  /**
   * Universal fallback. ScriptProcessorNode is deprecated but works everywhere,
   * and it needs no module loading, so it cannot fail the way the worklet does.
   */
  _attachScriptProcessor() {
    try {
      if (this.node) {
        try {
          this.node.disconnect();
        } catch {
          /* already detached */
        }
        this.node = null;
      }
      const size = 4096;
      const node = this.context.createScriptProcessor(size, 1, 1);
      const ratio = this.context.sampleRate / 16000;
      let acc = 0;
      let accCount = 0;
      const frame = new Float32Array(512);
      let frameIndex = 0;

      node.onaudioprocess = (event) => {
        if (!this.active || this._muted) return;
        const input = event.inputBuffer.getChannelData(0);
        this._measure(input);
        for (let i = 0; i < input.length; i += 1) {
          acc += input[i];
          accCount += 1;
          if (accCount >= ratio) {
            frame[frameIndex] = acc / accCount;
            frameIndex += 1;
            acc = 0;
            accCount = 0;
            if (frameIndex >= frame.length) {
              this._emitFrame(frame);
              frameIndex = 0;
            }
          }
        }
      };
      // A ScriptProcessor only runs while connected to a destination, so route it
      // through a muted gain node to avoid feedback.
      const sink = this.context.createGain();
      sink.gain.value = 0;
      this.source.connect(node);
      node.connect(sink);
      sink.connect(this.context.destination);
      this.node = node;
      this.sink = sink;
      this.backend = "script-processor";
      if (this.onBackendChange) this.onBackendChange(this.backend);
    } catch (error) {
      console.error("[surtitle] no capture backend available:", error);
      this.backend = "failed";
    }
  }

  _emitFrame(samples) {
    const pcm = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) {
      const clamped = Math.max(-1, Math.min(1, samples[i]));
      pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    this.framesReceived += 1;
    if (this.onAudio) this.onAudio(new Uint8Array(pcm.buffer));
  }

  /** Speech detection, shared by both backends. */
  _measure(channel) {
    let sum = 0;
    for (let i = 0; i < channel.length; i += 1) sum += channel[i] * channel[i];
    const rms = Math.sqrt(sum / channel.length);
    this.maxLevel = Math.max(this.maxLevel, Math.min(1, rms * 6));
    if (this.onLevel) this.onLevel(Math.min(1, rms * 6));

    // Matching the worklet's conservative thresholds: a false interruption is
    // worse than a slightly late one.
    const withinGrace = performance.now() < this._graceUntil;
    if (rms > 0.05 && !withinGrace) {
      this._speechFrames += 1;
      if (this._speechFrames >= 4 && !this._speaking) {
        this._speaking = true;
        if (this.onSpeechStart) this.onSpeechStart();
      }
    } else {
      this._speechFrames = 0;
      if (this._speaking && rms < 0.03) this._speaking = false;
    }
  }

  _handleWorklet(message) {
    if (!message) return;
    if (message.type === "audio") {
      this.framesReceived += 1;
      if (this.onAudio) this.onAudio(new Uint8Array(message.buffer));
    } else if (message.type === "level") {
      this.maxLevel = Math.max(this.maxLevel, message.value || 0);
      if (this.onLevel) this.onLevel(message.value);
    } else if (message.type === "speech-start" && this.onSpeechStart) {
      this.onSpeechStart();
    }
  }

  /** Change the preferred input device. Takes effect on the next start. */
  setDevice(deviceId) {
    this.deviceId = deviceId || "";
  }

  setMuted(muted) {
    this._muted = Boolean(muted);
    if (this.node && this.backend === "worklet" && this.node.port) {
      this.node.port.postMessage({ type: "mute", value: muted });
    }
  }

  /** Tell capture that playback began, so the speaker tail is ignored. */
  notifyPlayback(playing) {
    // 400 ms, matching the worklet's grace window.
    if (playing) this._graceUntil = performance.now() + 400;
    if (this.node && this.backend === "worklet" && this.node.port) {
      this.node.port.postMessage({ type: "playback", value: playing });
    }
  }

  /** Diagnostics for the caller after opening the microphone. */
  get status() {
    const track = this.stream && this.stream.getAudioTracks()[0];
    let settings = {};
    try {
      settings = (track && track.getSettings && track.getSettings()) || {};
    } catch {
      settings = {};
    }
    return {
      running: Boolean(this.context && this.context.state === "running"),
      backend: this.backend,
      framesReceived: this.framesReceived,
      maxLevel: this.maxLevel,
      silent: this.framesReceived === 0,
      deviceLabel: (track && track.label) || "",
      deviceId: settings.deviceId || "",
      trackMuted: track ? Boolean(track.muted) : null,
      trackState: track ? track.readyState : "",
    };
  }

  /** List input devices, for diagnosing a silent capture. */
  async listInputDevices() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      return devices
        .filter((device) => device.kind === "audioinput")
        .map((device, index) => ({
          index,
          label: device.label || `Input ${index + 1} (label hidden until permission is granted)`,
          deviceId: device.deviceId,
        }));
    } catch {
      return [];
    }
  }

  async stop() {
    this.active = false;
    clearTimeout(this._upgradeTimer);
    this._upgradeTimer = null;
    // Reset per-session state. Leaving these set meant a restart inherited the
    // previous session's backend and frame counts, so the fallback watchdog could
    // not tell "no frames yet" from "frames from last time" — which breaks the
    // second attempt after toggling the microphone.
    this.backend = "none";
    this.framesReceived = 0;
    this.maxLevel = 0;
    this._speechFrames = 0;
    this._speaking = false;
    this._graceUntil = 0;
    if (this.node) {
      if (this.node.port) this.node.port.onmessage = null;
      if (this.node.onaudioprocess !== undefined) this.node.onaudioprocess = null;
      try {
        this.node.disconnect();
      } catch {
        /* already detached */
      }
      this.node = null;
    }
    if (this.sink) {
      try {
        this.sink.disconnect();
      } catch {
        /* already detached */
      }
      this.sink = null;
    }
    if (this.source) {
      try {
        this.source.disconnect();
      } catch {
        /* already detached */
      }
      this.source = null;
    }
    if (this.stream) {
      for (const track of this.stream.getTracks()) track.stop();
      this.stream = null;
    }
    // The context is intentionally left open: it is reused on the next start.
  }

  /** Release the capture context entirely. Only for page teardown. */
  async dispose() {
    await this.stop();
    if (this.context) {
      await this.context.close().catch(() => {});
      this.context = null;
    }
  }
}

export class Playback {
  constructor({ onStart, onIdle, onBlocked, sampleRate = 24000, sinkId = "", element = null }) {
    this.onStart = onStart;
    this.onIdle = onIdle;
    // Called when audio arrives but the output context is not running, so the
    // user is told instead of hearing nothing.
    this.onBlocked = onBlocked;
    // Preferred output device. Only honoured where setSinkId() exists.
    this.sinkId = sinkId || "";
    this.element = element;
    this.sampleRate = sampleRate;
    this.sink = null;
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

      // Route through an <audio> element when one is available, because that is
      // the only route that can select an output device. The element plays a live
      // MediaStream, so the audio graph still performs the scheduling; the
      // element is purely the sink.
      //
      // Exactly one path is connected. Connecting both would play every sentence
      // twice — once through the element and once through the context.
      let routed = false;
      if (this.element && this.context.createMediaStreamDestination) {
        try {
          this.sink = this.context.createMediaStreamDestination();
          this.gain.connect(this.sink);
          this.element.srcObject = this.sink.stream;
          this.element.play().catch(() => {});
          routed = true;
        } catch {
          this.sink = null;
        }
      }
      if (!routed) {
        // No element route: use the context destination, which is always audible
        // at the cost of not being able to choose the output device.
        this.gain.connect(this.context.destination);
      }
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

  /** True when this browser can select an output device at all. */
  get canSelectOutput() {
    const element = this.element;
    return Boolean(element && typeof element.setSinkId === "function");
  }

  /**
   * Choose the output device.
   *
   * Returns a result rather than throwing, because "unsupported" and "refused"
   * need different messages and neither should interrupt playback.
   */
  async setOutputDevice(deviceId) {
    this.sinkId = deviceId || "";
    if (!this.canSelectOutput) {
      return { ok: false, reason: "unsupported" };
    }
    try {
      await this.element.setSinkId(this.sinkId);
      return { ok: true };
    } catch (error) {
      // NotAllowedError means the page lacks permission to enumerate or select
      // outputs, which usually resolves after the microphone has been granted.
      return { ok: false, reason: error && error.name ? error.name : "failed" };
    }
  }

  /** List output devices. Empty where the browser does not expose them. */
  async listOutputDevices() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      return devices
        .filter((device) => device.kind === "audiooutput")
        .map((device, index) => ({
          deviceId: device.deviceId,
          label: device.label || `Output ${index + 1} (name hidden until permission is granted)`,
        }));
    } catch {
      return [];
    }
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
