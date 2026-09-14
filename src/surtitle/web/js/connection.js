/**
 * WebSocket transport.
 *
 * Binary framing with a one-byte opcode, matching the server:
 *   0x01 + PCM16 bytes  -> microphone audio
 *   0x02 + JSON bytes   -> control command
 *
 * Audio is sent as binary rather than base64-in-JSON because the microphone
 * stream is continuous; base64 would add a third to the traffic for nothing.
 */

const OP_AUDIO = 0x01;
const OP_JSON = 0x02;

export const ConnectionState = {
  CONNECTING: "connecting",
  OPEN: "open",
  CLOSED: "closed",
};

export class Connection {
  constructor({ onEvent, onAudio, onState, onError }) {
    this.onEvent = onEvent;
    this.onAudio = onAudio;
    this.onState = onState;
    this.onError = onError;
    this.socket = null;
    this.state = ConnectionState.CLOSED;
    this._hello = null;
    this._queue = [];
    this._retries = 0;
    this._closedByUs = false;
    this._reconnectTimer = null;
  }

  connect(hello) {
    this._hello = hello;
    this._closedByUs = false;
    this._open();
  }

  _open() {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${scheme}://${window.location.host}/ws`;

    this._setState(ConnectionState.CONNECTING);
    const socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    this.socket = socket;

    socket.onopen = () => {
      this._retries = 0;
      this._setState(ConnectionState.OPEN);
      // The server accepts immediately and expects hello as the first message.
      this._sendJson({ kind: "hello", data: this._hello });
      for (const payload of this._queue.splice(0)) {
        this._sendJson(payload);
      }
    };

    socket.onmessage = (event) => this._onMessage(event);

    socket.onclose = () => {
      this._setState(ConnectionState.CLOSED);
      if (!this._closedByUs) this._scheduleReconnect();
    };

    socket.onerror = () => {
      if (this.onError) this.onError("Connection error");
    };
  }

  _scheduleReconnect() {
    // Back off so a restarted server is picked up without hammering it.
    const delay = Math.min(8000, 500 * 2 ** this._retries);
    this._retries += 1;
    clearTimeout(this._reconnectTimer);
    this._reconnectTimer = setTimeout(() => {
      if (!this._closedByUs) this._open();
    }, delay);
  }

  _onMessage(event) {
    if (typeof event.data === "string") {
      try {
        if (this.onEvent) this.onEvent(JSON.parse(event.data));
      } catch {
        /* ignore malformed frame */
      }
      return;
    }

    const bytes = new Uint8Array(event.data);
    if (bytes.length === 0) return;

    if (bytes[0] === OP_AUDIO) {
      if (this.onAudio) this.onAudio(bytes.subarray(1));
      return;
    }
    if (bytes[0] === OP_JSON) {
      try {
        if (this.onEvent) {
          this.onEvent(JSON.parse(new TextDecoder().decode(bytes.subarray(1))));
        }
      } catch {
        /* ignore malformed frame */
      }
    }
  }

  _setState(state) {
    this.state = state;
    if (this.onState) this.onState(state);
  }

  get ready() {
    return this.state === ConnectionState.OPEN;
  }

  sendAudio(pcmBytes) {
    if (!this.ready || !pcmBytes || pcmBytes.length === 0) return;
    const frame = new Uint8Array(pcmBytes.length + 1);
    frame[0] = OP_AUDIO;
    frame.set(pcmBytes, 1);
    // Drop audio rather than queue it: stale speech is worse than a small gap.
    try {
      this.socket.send(frame);
    } catch {
      /* socket went away mid-send */
    }
  }

  sendCommand(kind, data = {}) {
    this._sendJson({ kind, data });
  }

  _sendJson(payload) {
    if (!this.ready) {
      // Queue control commands so an approval answered during a reconnect is
      // not lost, but keep the queue bounded.
      if (this._queue.length < 50) this._queue.push(payload);
      return;
    }
    try {
      this.socket.send(JSON.stringify(payload));
    } catch {
      /* socket went away mid-send */
    }
  }

  close() {
    this._closedByUs = true;
    clearTimeout(this._reconnectTimer);
    if (this.socket) {
      try {
        this.socket.close();
      } catch {
        /* already closed */
      }
      this.socket = null;
    }
    this._setState(ConnectionState.CLOSED);
  }
}
