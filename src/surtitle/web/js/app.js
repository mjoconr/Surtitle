/**
 * Application shell.
 *
 * Holds the state the UI needs, wires the transport and audio layers together,
 * and renders the transcript. Rendering is intentionally piecemeal rather than a
 * full re-render: agent text arrives incrementally, so each turn keeps its own
 * DOM nodes and appends as events land.
 */

import { Capture, Playback } from "./audio.js";
import { Connection, ConnectionState } from "./connection.js";
import { SettingsPanel } from "./settings.js";

const AGENT_STATE_LABELS = {
  idle: "Idle",
  listening: "Listening",
  thinking: "Thinking",
  tool: "Working",
  awaiting_approval: "Needs approval",
  speaking: "Speaking",
  error: "Error",
};

const state = {
  projects: [],
  sessions: [],
  project: null,
  session: null,
  turns: new Map(),
  currentTurn: null,
  toolRows: new Map(),
  files: { path: ".", entries: [] },
  activity: [],
  micOpen: false,
  // Set from the server's ready event; false disables the mic button.
  voiceAvailable: true,
  pendingApproval: null,
  settings: null,
  rightTab: "files",
  environment: null,
};

const el = {
  frame: document.getElementById("frame"),
  projectList: document.getElementById("projectList"),
  sessionList: document.getElementById("sessionList"),
  turns: document.getElementById("turns"),
  transcript: document.getElementById("transcript"),
  captions: document.getElementById("captions"),
  composer: document.getElementById("composer"),
  composerCard: document.getElementById("composerCard"),
  sendButton: document.getElementById("sendButton"),
  micButton: document.getElementById("micButton"),
  micLabel: document.getElementById("micLabel"),
  micLevel: document.getElementById("micLevel"),
  agentState: document.getElementById("agentState"),
  agentStateLabel: document.getElementById("agentStateLabel"),
  connection: document.getElementById("connection"),
  connectionLabel: document.getElementById("connectionLabel"),
  headerTitle: document.getElementById("headerTitle"),
  headerSubtitle: document.getElementById("headerSubtitle"),
  rightbarBody: document.getElementById("rightbarBody"),
  modelBadge: document.getElementById("modelBadge"),
  approvalStrip: document.getElementById("approvalStrip"),
  approvalText: document.getElementById("approvalText"),
  approvalActions: document.getElementById("approvalActions"),
  toast: document.getElementById("toast"),
};

// --------------------------------------------------------------- utilities

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  // textContent everywhere, never innerHTML: model output is untrusted.
  if (text !== undefined) element.textContent = text;
  return element;
}

let toastTimer = null;
function toast(message, tone = "ok") {
  el.toast.textContent = message;
  el.toast.dataset.tone = tone;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.toast.hidden = true;
  }, tone === "error" ? 6500 : 2600);
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: options?.body ? { "Content-Type": "application/json" } : undefined,
    ...options,
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = { error: text };
  }
  if (!response.ok) {
    const error = new Error(payload?.error || `Request failed (${response.status})`);
    error.field = payload?.field;
    error.status = response.status;
    throw error;
  }
  return payload;
}

// ---------------------------------------------------------------- transcript

function beginTurn(kind) {
  const turn = node("div", "turn");
  const spoken = node("div");
  const shown = node("div");
  shown.style.display = "flex";
  shown.style.flexDirection = "column";
  shown.style.gap = "8px";
  const tools = node("div");
  tools.style.display = "flex";
  tools.style.flexDirection = "column";
  tools.style.gap = "6px";

  if (kind === "user") {
    const bubble = node("div", "bubble bubble--user");
    turn.append(bubble);
    el.turns.append(turn);
    scrollToBottom();
    return { id: Symbol("turn"), kind, root: turn, bubble, spoken, shown, tools, saidText: "" };
  }

  turn.append(spoken, shown, tools);
  el.turns.append(turn);
  scrollToBottom();
  return { id: Symbol("turn"), kind, root: turn, bubble: null, spoken, shown, tools, saidText: "" };
}

function assistantTurn() {
  if (state.currentTurn && state.currentTurn.kind === "assistant") {
    return state.currentTurn;
  }
  const turn = beginTurn("assistant");
  state.currentTurn = turn;
  return turn;
}

function appendSaid(turn, text) {
  if (!text) return;
  turn.saidText += (turn.saidText ? " " : "") + text;
  turn.spoken.replaceChildren();
  const block = node("div", "said");
  block.append(node("span", "said__glyph"));
  block.append(node("span", null, turn.saidText));
  turn.spoken.append(block);
}

function appendShown(turn, text) {
  if (!text) return;
  const last = turn.shown.lastElementChild;
  // Consecutive display chunks belong to one block unless a tool row or a
  // spoken line intervened, which is the visual promise of the speak layer.
  if (last && last.dataset.kind === "shown") {
    last.textContent += text;
  } else {
    const block = node("div", "shown", text);
    block.dataset.kind = "shown";
    turn.shown.append(block);
  }
}

function appendError(turn, message) {
  const block = node("p", "error", message);
  turn.root.append(block);
}

function addToolRow(turn, callId, name) {
  const row = node("div", "toolrow");
  row.dataset.state = "running";
  row.dataset.tool = name;

  const head = node("button", "toolrow__head");
  head.type = "button";
  head.append(node("span", "toolrow__dot"));
  head.append(node("span", "toolrow__name", name));
  const summary = node("span", "toolrow__summary", "running…");
  head.append(summary);

  const body = node("div", "toolrow__body");
  body.hidden = true;

  head.addEventListener("click", () => {
    body.hidden = !body.hidden;
  });

  row.append(head, body);
  turn.tools.append(row);
  scrollToBottom();

  const record = { row, summary, body, name };
  state.toolRows.set(callId, record);
  return record;
}

function scrollToBottom() {
  const nearBottom =
    el.transcript.scrollHeight - el.transcript.scrollTop - el.transcript.clientHeight < 160;
  if (nearBottom) {
    el.transcript.scrollTop = el.transcript.scrollHeight;
  }
}

// ------------------------------------------------------------------ sidebar

function renderProjects() {
  el.projectList.replaceChildren();
  if (state.projects.length === 0) {
    el.projectList.append(node("p", "empty", "No projects yet. Create one to begin."));
    return;
  }
  for (const project of state.projects) {
    const button = node("button", "row");
    button.type = "button";
    button.setAttribute("aria-current", String(state.project?.id === project.id));
    button.append(node("span", "row__icon", "▸"));
    const main = node("div", "row__main");
    main.append(node("div", "row__title", project.name));
    main.append(node("div", "row__meta", project.exists ? project.root : "folder missing"));
    button.append(main);
    button.addEventListener("click", () => selectProject(project.id));
    el.projectList.append(button);
  }
}

function renderSessions() {
  el.sessionList.replaceChildren();
  if (!state.project) {
    el.sessionList.append(node("p", "empty", "Open a project first."));
    return;
  }
  if (state.sessions.length === 0) {
    el.sessionList.append(node("p", "empty", "No conversations yet."));
    return;
  }
  for (const session of state.sessions) {
    const button = node("button", "row");
    button.type = "button";
    button.setAttribute("aria-current", String(state.session?.id === session.id));
    button.append(node("span", "row__icon", "□"));
    const main = node("div", "row__main");
    main.append(node("div", "row__title", session.title));
    main.append(node("div", "row__meta", new Date(session.updated_at * 1000).toLocaleString()));
    button.append(main);
    button.addEventListener("click", () => selectSession(session.id));
    el.sessionList.append(button);
  }
}

function renderHeader() {
  if (!state.project) {
    el.headerTitle.textContent = "No project open";
    el.headerSubtitle.textContent = "Create a project to begin";
    return;
  }
  el.headerTitle.textContent = state.project.name;
  el.headerSubtitle.textContent = state.project.root;
}

// ------------------------------------------------------------- right panel

function renderRightbar() {
  document.getElementById("tabFiles").setAttribute("aria-selected", String(state.rightTab === "files"));
  document
    .getElementById("tabActivity")
    .setAttribute("aria-selected", String(state.rightTab === "activity"));

  el.rightbarBody.replaceChildren();
  if (state.rightTab === "activity") {
    renderActivity();
    return;
  }
  renderFiles();
}

function renderFiles() {
  if (!state.project) {
    el.rightbarBody.append(node("p", "empty", "No project open."));
    return;
  }

  const header = node("div", "row");
  header.style.cursor = "default";
  const up = node("button", "button button--ghost", "↑");
  up.type = "button";
  up.disabled = state.files.path === ".";
  up.addEventListener("click", () => {
    const parts = state.files.path.split("/");
    parts.pop();
    loadFiles(parts.length ? parts.join("/") : ".");
  });
  const path = node("div", "row__main row__meta", state.files.path);
  header.append(up, path);
  el.rightbarBody.append(header);

  const tree = node("div", "filetree");
  if (state.files.entries.length === 0) {
    tree.append(node("p", "empty", "This folder is empty."));
  }
  for (const entry of state.files.entries) {
    const item = node("button", "filetree__item");
    item.type = "button";
    const isDir = entry.type === "dir";
    item.append(node("span", null, isDir ? "▸" : "·"));
    item.append(node("span", null, entry.name));
    item.addEventListener("click", () => {
      const path = state.files.path === "." ? entry.name : `${state.files.path}/${entry.name}`;
      if (isDir) loadFiles(path);
      else window.open(projectFileUrl(path), "_blank", "noopener");
    });
    tree.append(item);
  }
  el.rightbarBody.append(tree);
}

function renderEnvironment() {
  const info = state.environment;
  if (!info) return;

  const section = node("div");
  section.append(node("div", "sectionTitle", "Python environment"));

  const detail = node("div", "detail");
  detail.append(
    keyValue("Isolated", info.isolated ? "yes (project-local)" : "no (shared with the app)"),
    keyValue("Packages installed", String(info.package_count ?? 0)),
  );
  section.append(detail);

  if (Array.isArray(info.approved) && info.approved.length) {
    const list = node("div", "detail");
    list.append(node("span", "detail__key", "Approved requirements"));
    for (const requirement of info.approved) {
      list.append(node("div", "detail__value", requirement));
    }
    section.append(list);
  }

  const note = node(
    "p",
    "notice",
    info.isolated
      ? "The agent installs packages here when it needs a library it does not have. This never changes the application itself."
      : "The agent is using the application environment. It creates its own isolated environment the first time it installs a package.",
  );
  note.style.marginTop = "8px";
  section.append(note);
  el.rightbarBody.append(section);
}

function renderActivity() {
  renderEnvironment();
  if (state.activity.length === 0) {
    el.rightbarBody.append(node("p", "empty", "Nothing has happened yet."));
    return;
  }
  const list = node("div", "filetree");
  for (const item of [...state.activity].reverse()) {
    const row = node("div", "row");
    row.style.cursor = "default";
    const main = node("div", "row__main");
    main.append(node("div", "row__title", item.label));
    if (item.detail) main.append(node("div", "row__meta", item.detail));
    row.append(main);
    list.append(row);
  }
  el.rightbarBody.append(list);
}

function projectFileUrl(path) {
  return `/api/projects/${state.project.id}/file?path=${encodeURIComponent(path)}`;
}

async function loadFiles(path) {
  if (!state.project) return;
  try {
    const data = await api(
      `/api/projects/${state.project.id}/files?path=${encodeURIComponent(path)}`,
    );
    state.files = { path: data.path || ".", entries: data.entries || [] };
  } catch (error) {
    state.files = { path, entries: [] };
    toast(error.message, "error");
  }
  renderRightbar();
}

// --------------------------------------------------------------- agent state

function setAgentState(name) {
  const label = AGENT_STATE_LABELS[name] || name;
  el.agentState.dataset.state = name;
  el.agentStateLabel.textContent = label;
}

function setConnection(name, label) {
  el.connection.dataset.state = name === ConnectionState.OPEN ? "idle" : "error";
  el.connectionLabel.textContent = label;
}

// ------------------------------------------------------------- event handling

function handleEvent(event) {
  const data = event.data || {};
  switch (event.kind) {
    case "ready": {
      if (data.pong) return;
      if (typeof data.voice_enabled === "boolean") {
        state.voiceAvailable = data.voice_enabled;
        if (!data.voice_enabled) {
          el.micButton.disabled = true;
          el.micLabel.textContent = "Voice off";
        }
      }
      if (data.model) el.modelBadge.textContent = data.model;
      if (data.environment) {
        state.environment = data.environment;
      }
      if (Array.isArray(data.mcp_failures) && data.mcp_failures.length) {
        // A configured MCP server that did not start is worth telling the user
        // about, but it must not stop the session.
        toast(`MCP: ${data.mcp_failures[0]}`, "error");
      }
      renderRightbar();
      break;
    }
    case "state": {
      if (data.state) {
        setAgentState(data.state);
        // A cancellation reports `idle` without ever sending `done`, so the
        // release has to happen here too.
        if (data.state !== "awaiting_approval") clearApproval();
      }
      break;
    }
    case "interim": {
      const text = (data.text || "").trim();
      el.captions.replaceChildren();
      if (text) {
        // Labelled, because unlabelled text floating above the composer reads as
        // a stray marker rather than something you said.
        el.captions.append(node("span", "captions__speaker", "You said"));
        el.captions.append(node("span", data.final ? null : "captions__pending", text));
      } else if (state.micOpen) {
        // Capture is running but nothing has been recognised yet.
        el.captions.append(node("span", "captions__hint", "Listening…"));
      }
      break;
    }
    case "user_text": {
      // A new turn supersedes anything outstanding: if an approval was waiting,
      // the turn that asked for it is gone.
      clearApproval();
      const turn = beginTurn("user");
      turn.bubble.textContent = data.text || "";
      state.currentTurn = null;
      el.captions.replaceChildren();
      break;
    }
    case "say": {
      const turn = assistantTurn();
      appendSaid(turn, data.text || "");
      if (playback.playing) setAgentState("speaking");
      scrollToBottom();
      break;
    }
    case "agent_text": {
      const turn = assistantTurn();
      appendShown(turn, data.text || "");
      scrollToBottom();
      break;
    }
    case "tool_call": {
      const turn = assistantTurn();
      addToolRow(turn, data.call_id, data.name || "tool");
      state.activity.push({ label: `Called ${data.name}`, detail: formatArgs(data.arguments) });
      renderRightbar();
      break;
    }
    case "tool_result": {
      const record = state.toolRows.get(data.call_id);
      if (record) {
        record.row.dataset.state = data.ok ? "ok" : "error";
        record.summary.textContent = data.display || data.error || (data.ok ? "done" : "failed");
        record.body.textContent = [data.display, data.error, data.duration_ms ? `${data.duration_ms} ms` : ""]
          .filter(Boolean)
          .join("\n");
        // Surface failures open, because a collapsed error is easy to miss.
        if (!data.ok) record.body.hidden = false;
      }
      state.activity.push({
        label: `${data.name} ${data.ok ? "succeeded" : "failed"}`,
        detail: data.display || data.error || "",
      });
      renderRightbar();
      break;
    }
    case "artifact": {
      const turn = assistantTurn();
      const link = node("a", "artifact", `Open ${data.path}`);
      link.href = projectFileUrl(data.path);
      link.target = "_blank";
      link.rel = "noopener";
      turn.root.append(link);
      state.activity.push({ label: "Created file", detail: data.path });
      renderRightbar();
      break;
    }
    case "approval_request": {
      // The composer becomes the approval surface, so the decision cannot be
      // missed and the normal send action cannot race it.
      state.pendingApproval = data;
      el.composerCard.dataset.approval = "true";
      el.approvalStrip.hidden = false;
      el.approvalActions.hidden = false;
      el.sendButton.hidden = true;
      el.approvalText.textContent = describeApproval(data);
      const turn = assistantTurn();
      addToolRow(turn, data.call_id, `${data.name} (awaiting approval)`);
      setAgentState("awaiting_approval");
      break;
    }
    case "usage": {
      if (data.total_tokens) {
        el.modelBadge.textContent = `${state.settings?.model || ""} · ${data.total_tokens} tok`.trim();
      }
      break;
    }
    case "thinking": {
      // Reasoning is shown in the activity panel only: it is useful, but it is
      // not the answer and must not be confused with it.
      if (data.text) {
        state.activity.push({ label: "Reasoning", detail: data.text.slice(0, 240) });
        renderRightbar();
      }
      break;
    }
    case "error": {
      // An errored turn cannot still be waiting for approval, and a stuck prompt
      // is worse than no prompt: it blocks the composer indefinitely.
      clearApproval();
      const turn = state.currentTurn || beginTurn("assistant");
      appendError(turn, data.message || "Something went wrong.");
      setAgentState("error");
      if (data.recoverable) toast(data.message, "error");
      break;
    }
    case "done": {
      clearApproval();
      if (!data.failed && !data.truncated) setAgentState(state.micOpen ? "listening" : "idle");
      state.currentTurn = null;
      break;
    }
    default:
      break;
  }
  if (event.seq) scrollToBottom();
}

function describeApproval(data) {
  const args = data.arguments || {};
  if (data.name === "install_packages" && Array.isArray(args.packages)) {
    // Naming the packages is the point: the user is approving third-party code.
    return `Install ${args.packages.length} Python package(s): ${args.packages.join(", ")}?`;
  }
  if (data.name === "run_shell" && args.command) {
    return `Run this command: ${args.command}`;
  }
  if (data.name === "run_python") {
    return "Run Python code in this project?";
  }
  if (typeof args.path === "string") {
    return `${data.summary || data.name} — ${args.path}?`;
  }
  return `${data.summary || data.name} — allow this?`;
}

function keyValue(key, value) {
  const row = node("div");
  const label = node("span", "detail__key", key + ": ");
  const content = node("span", "detail__value", value);
  row.append(label, content);
  return row;
}

function formatArgs(args) {
  if (!args || typeof args !== "object") return "";
  return Object.entries(args)
    .map(([key, value]) => {
      const text = typeof value === "string" ? value : JSON.stringify(value);
      return `${key}=${text.length > 60 ? `${text.slice(0, 60)}…` : text}`;
    })
    .join(" ");
}

function clearApproval() {
  state.pendingApproval = null;
  el.composerCard.dataset.approval = "false";
  el.approvalStrip.hidden = true;
  el.approvalActions.hidden = true;
  el.sendButton.hidden = false;
}

// -------------------------------------------------------------------- voice

const capture = new Capture({
  onAudio: (frame) => connection.sendAudio(frame),
  onLevel: (value) => {
    el.micLevel.style.width = `${Math.round(value * 100)}%`;
  },
  onSpeechStart: () => {
    // Instant barge-in: silence the speaker locally before the server has even
    // seen the transcript, then tell the server to stop generating.
    if (playback.playing) {
      playback.stop();
      connection.sendCommand("barge_in");
      setAgentState("listening");
    }
  },
});

const playback = new Playback({
  sampleRate: 24000,
  onStart: () => {
    setAgentState("speaking");
    // The grace window stops the speaker tail from triggering barge-in before
    // echo cancellation has converged.
    capture.notifyPlayback(true);
  },
  onIdle: () => {
    capture.notifyPlayback(false);
    setAgentState(state.micOpen ? "listening" : "idle");
  },
  onBlocked: () => {
    // The agent produced speech but the browser will not play it. Say so, with
    // the fix, rather than leaving the user staring at a silent page.
    toast(
      "Audio is blocked by the browser. Click anywhere on the page and try again.",
      "error",
    );
  },
});

const connection = new Connection({
  onEvent: handleEvent,
  onAudio: (pcm) => playback.push(pcm),
  onState: (connectionState) => {
    if (connectionState === ConnectionState.OPEN) setConnection(connectionState, "Connected");
    else if (connectionState === ConnectionState.CONNECTING)
      setConnection(connectionState, "Connecting");
    else setConnection(connectionState, "Disconnected");
  },
  onError: (message) => toast(message, "error"),
});

async function toggleMic() {
  if (!state.session) {
    toast("Open a conversation first.", "error");
    return;
  }
  try {
    if (state.micOpen) {
      capture.setMuted(true);
      state.micOpen = false;
      el.micButton.dataset.active = "false";
      el.micButton.setAttribute("aria-pressed", "false");
      el.micLabel.textContent = "Mic off";
      connection.sendCommand("mic", { open: false });
      setAgentState("idle");
      el.captions.replaceChildren();
      return;
    }

    // Unlock audio output here, inside the click. An AudioContext created later —
    // when the first audio chunk arrives over the socket — is created outside a
    // user gesture, so it starts suspended and can then never be resumed. The
    // result is a reply that silently never plays, with no error anywhere.
    const outputReady = await playback.unlock();
    if (!outputReady) {
      toast(
        "Your browser blocked audio playback. Click anywhere on the page, then try again.",
        "error",
      );
    }

    const captureState = await capture.start();
    state.micOpen = true;
    el.micButton.dataset.active = "true";
    el.micButton.setAttribute("aria-pressed", "true");
    el.micLabel.textContent = "Listening";
    connection.sendCommand("mic", { open: true });
    setAgentState("listening");
    el.captions.replaceChildren();
    el.captions.append(node("span", "captions__hint", "Listening…"));

    // Distinguish "the microphone is open" from "audio is reaching the app".
    // Without this the two look identical, and a silently suspended audio context
    // is indistinguishable from a mute microphone.
    if (!captureState || captureState.running === false) {
      toast(
        "The browser is not running audio capture. Click anywhere on the page, then toggle the mic again.",
        "error",
      );
    }
    window.setTimeout(() => {
      if (!state.micOpen) return;
      const status = capture.status;
      let problem = null;
      if (status.silent) {
        problem =
          "No audio is reaching the app. Check the browser's microphone permission, " +
          "then the input device in macOS System Settings → Sound.";
      } else if (status.maxLevel < 0.01) {
        problem = "Your microphone is connected but the signal is silent. Raise the input level.";
      }
      if (problem) {
        // Shown in the caption band as well as a toast: that is where the user is
        // already looking, and it persists instead of disappearing.
        el.captions.replaceChildren(node("span", "captions__problem", problem));
        toast(problem, "error");
      }
    }, 3500);
  } catch (error) {
    // A denied microphone must not block the text-only path.
    toast(
      `Microphone unavailable: ${error.message}. You can still type your requests.`,
      "error",
    );
  }
}

// ------------------------------------------------------------------- actions

async function loadProjects() {
  const data = await api("/api/projects");
  state.projects = data.projects || [];
  renderProjects();
}

async function selectProject(projectId) {
  const project = await api(`/api/projects/${projectId}`);
  state.project = project;
  state.sessions = project.sessions || [];
  state.session = null;
  state.turns.clear();
  state.toolRows.clear();
  state.currentTurn = null;
  state.activity = [];
  el.turns.replaceChildren();

  renderProjects();
  renderSessions();
  renderHeader();
  await loadFiles(".");

  if (state.sessions.length === 0) {
    await createSession();
  } else {
    await selectSession(state.sessions[0].id);
  }
}

async function createSession() {
  if (!state.project) return;
  const session = await api(`/api/projects/${state.project.id}/sessions`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  state.sessions.unshift(session);
  await selectSession(session.id);
}

let sessionRetry = 0;

async function selectSession(sessionId) {
  const session = await api(`/api/sessions/${sessionId}`);
  state.session = session;
  el.turns.replaceChildren();
  state.turns.clear();
  state.toolRows.clear();
  state.currentTurn = null;
  state.activity = [];

  // Replay the stored transcript so reopening a conversation shows its history.
  if (session.messages) {
    for (const message of session.messages) {
      if (message.role === "user") {
        const turn = beginTurn("user");
        turn.bubble.textContent = message.content;
      } else {
        const turn = beginTurn("assistant");
        if (message.spoken) appendSaid(turn, message.spoken);
        else appendShown(turn, message.content);
      }
    }
    state.currentTurn = null;
  }

  renderSessions();
  openConnection();
}

function openConnection() {
  if (!state.project || !state.session) return;
  connection.close();
  connection.connect({ project_id: state.project.id, session_id: state.session.id });
}

function sendMessage() {
  const text = el.composer.value.trim();
  if (!text) return;
  if (!state.session) {
    toast("Open a project and conversation first.", "error");
    return;
  }
  connection.sendCommand("text", { text });
  el.composer.value = "";
  el.composer.style.height = "auto";
  setAgentState("thinking");
}

function answerApproval(allowed, remember) {
  const pending = state.pendingApproval;
  clearApproval();
  if (!pending) return;
  connection.sendCommand("approval", {
    call_id: pending.call_id,
    allowed,
    remember,
  });
  setAgentState("thinking");
}

// -------------------------------------------------------------------- wiring

el.sendButton.addEventListener("click", () => sendMessage());

el.micButton.addEventListener("click", toggleMic);

document.getElementById("approvalAllow").addEventListener("click", (event) => {
  // Shift-click means "and don't ask again for this tool".
  answerApproval(true, event.shiftKey);
});
document.getElementById("approvalDeny").addEventListener("click", () => answerApproval(false, false));

el.composer.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});

el.composer.addEventListener("input", () => {
  el.composer.style.height = "auto";
  el.composer.style.height = `${Math.min(160, el.composer.scrollHeight)}px`;
});

document.getElementById("newSession").addEventListener("click", () => createSession());

document.getElementById("tabFiles").addEventListener("click", () => {
  state.rightTab = "files";
  renderRightbar();
});
document.getElementById("tabActivity").addEventListener("click", () => {
  state.rightTab = "activity";
  renderRightbar();
});
document.getElementById("rightbarToggle").addEventListener("click", () => {
  const collapsed = el.frame.dataset.rightbarCollapsed === "true";
  el.frame.dataset.rightbarCollapsed = String(!collapsed);
});
document.getElementById("sidebarToggle").addEventListener("click", () => {
  const collapsed = el.frame.dataset.sidebarCollapsed === "true";
  el.frame.dataset.sidebarCollapsed = String(!collapsed);
});

// Theme: a body attribute, matching how the design platform switches schemes.
const THEME_KEY = "surtitle.theme";
function applyTheme(preference) {
  const dark =
    preference === "dark" ||
    (preference === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.body.toggleAttribute("data-ds-dark-theme", dark);
}
document.getElementById("themeToggle").addEventListener("click", () => {
  const dark = document.body.hasAttribute("data-ds-dark-theme");
  const next = dark ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
});

// ------------------------------------------------------------- project modal

const projectModal = document.getElementById("projectModal");
function openProjectModal() {
  projectModal.hidden = false;
  document.getElementById("projectName").value = "";
  document.getElementById("projectRoot").value = "";
  document.getElementById("projectError").hidden = true;
  document.getElementById("projectName").focus();
}
function closeProjectModal() {
  projectModal.hidden = true;
}
document.getElementById("newProject").addEventListener("click", openProjectModal);
document.getElementById("projectClose").addEventListener("click", closeProjectModal);
document.getElementById("projectCancel").addEventListener("click", closeProjectModal);
document.getElementById("projectMask").addEventListener("click", closeProjectModal);
document.getElementById("projectCreate").addEventListener("click", async () => {
  const name = document.getElementById("projectName").value.trim();
  const root = document.getElementById("projectRoot").value.trim();
  const error = document.getElementById("projectError");
  error.hidden = true;

  if (!name) {
    error.textContent = "Give the project a name.";
    error.hidden = false;
    return;
  }

  try {
    const project = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, root: root || undefined }),
    });
    closeProjectModal();
    await loadProjects();
    await selectProject(project.id);
    toast(`Project "${project.name}" ready.`, "ok");
  } catch (cause) {
    error.textContent = cause.message;
    error.hidden = false;
  }
});

// ---------------------------------------------------------------- settings

const settings = new SettingsPanel({
  onSaved: (described) => {
    state.settings = described;
    const model = Object.values(described.sections || {})
      .flat()
      .find((field) => field.name === "deepseek_model");
    if (model) el.modelBadge.textContent = model.value;
  },
  onToast: toast,
});
document.getElementById("settingsOpen").addEventListener("click", () => settings.open());

// ------------------------------------------------------------------ startup

async function main() {
  applyTheme(localStorage.getItem(THEME_KEY) || "system");
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if ((localStorage.getItem(THEME_KEY) || "system") === "system") applyTheme("system");
  });

  setAgentState("idle");
  setConnection(ConnectionState.CLOSED, "Starting");

  try {
    const health = await api("/api/health");
    el.modelBadge.textContent = health.model;
    if (!health.deepseek_configured) {
      toast("Add your DeepSeek API key in Settings to start.", "error");
      settings.open();
    } else if (!health.deepgram_configured) {
      toast("Voice is unavailable without a Deepgram key; you can still type.", "error");
    }
  } catch (error) {
    toast(`Cannot reach the server: ${error.message}`, "error");
  }

  try {
    await loadProjects();
    if (state.projects.length > 0) {
      await selectProject(state.projects[0].id);
    } else {
      renderSessions();
      renderRightbar();
      openProjectModal();
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

window.addEventListener("beforeunload", () => {
  connection.close();
  capture.dispose().catch(() => {});
  playback.close().catch(() => {});
});

main();
