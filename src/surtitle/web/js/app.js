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
  // Conversations the user filed away: kept, but out of the sidebar and out of
  // the agent's history search until restored.
  archive: [],
  showArchive: false,
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
  attachments: [],
};

const el = {
  frame: document.getElementById("frame"),
  projectList: document.getElementById("projectList"),
  sessionList: document.getElementById("sessionList"),
  archiveToggle: document.getElementById("archiveToggle"),
  archiveCount: document.getElementById("archiveCount"),
  archiveList: document.getElementById("archiveList"),
  archiveFoot: document.getElementById("archiveFoot"),
  archivePurge: document.getElementById("archivePurge"),
  confirmModal: document.getElementById("confirmModal"),
  confirmTitle: document.getElementById("confirmTitle"),
  confirmText: document.getElementById("confirmText"),
  confirmNote: document.getElementById("confirmNote"),
  confirmOk: document.getElementById("confirmOk"),
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
  attachments: document.getElementById("attachments"),
  fileInput: document.getElementById("fileInput"),
  attachButton: document.getElementById("attachButton"),
  dropzone: document.getElementById("dropzone"),
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

// ------------------------------------------------------------- attachments

/**
 * Files the user attached, held until the message is sent.
 *
 * Uploading happens at send time rather than on selection so a message is atomic:
 * the agent never receives a reference to a file that failed to store, and its
 * path is in the project before the text that mentions it.
 */
function renderAttachments() {
  el.attachments.replaceChildren();
  el.attachments.hidden = state.attachments.length === 0;

  for (const [index, file] of state.attachments.entries()) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.dataset.state = file.state || "pending";

    if (file.preview) {
      const thumb = document.createElement("img");
      thumb.className = "chip__thumb";
      thumb.src = file.preview;
      thumb.alt = "";
      chip.append(thumb);
    } else {
      // A glyph rather than nothing, so a non-image file is still visibly a file.
      chip.append(node("span", "chip__glyph", fileGlyph(file.name)));
    }

    chip.append(node("span", "chip__name", file.name));

    const remove = node("button", "chip__remove", "✕");
    remove.type = "button";
    remove.title = "Remove this attachment";
    remove.setAttribute("aria-label", `Remove ${file.name}`);
    remove.addEventListener("click", () => {
      const [removed] = state.attachments.splice(index, 1);
      if (removed && removed.preview) URL.revokeObjectURL(removed.preview);
      renderAttachments();
    });
    chip.append(remove);
    el.attachments.append(chip);
  }
}

function fileGlyph(name) {
  const extension = (name.split(".").pop() || "").toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(extension)) return "IMG";
  if (["pdf"].includes(extension)) return "PDF";
  if (["xlsx", "xls", "csv"].includes(extension)) return "XLS";
  if (["docx", "doc", "odt", "rtf"].includes(extension)) return "DOC";
  if (["pptx", "ppt", "odp"].includes(extension)) return "PPT";
  if (["py", "js", "ts", "json", "sh", "ps1", "rs", "go"].includes(extension)) return "<>";
  if (["zip", "gz", "tar"].includes(extension)) return "ZIP";
  return "FILE";
}

function addFiles(fileList) {
  const incoming = Array.from(fileList || []);
  if (incoming.length === 0) return;
  for (const file of incoming) {
    // Images get a local preview so the user can confirm the right one is attached.
    const preview = file.type && file.type.startsWith("image/") ? URL.createObjectURL(file) : "";
    state.attachments.push({ file, name: file.name, preview, state: "pending" });
  }
  renderAttachments();
}

/** Upload everything pending, returning the stored upload records. */
async function uploadAttachments() {
  const pending = state.attachments.filter((item) => item.file && item.state !== "stored");
  if (pending.length === 0) return [];

  const body = new FormData();
  for (const item of pending) {
    body.append("files", item.file, item.name);
    item.state = "uploading";
  }
  if (state.session) body.append("session_id", state.session.id);
  renderAttachments();

  const response = await fetch(`/api/projects/${state.project.id}/uploads`, {
    method: "POST",
    body,
  });
  const payload = await response.json();
  if (!response.ok) {
    for (const item of pending) item.state = "failed";
    renderAttachments();
    throw new Error(payload.error || "Upload failed");
  }

  for (const item of pending) {
    const stored = payload.files.find((entry) => entry.name === item.name);
    item.state = "stored";
    item.storedPath = stored ? stored.path : "";
  }
  renderAttachments();
  return payload.files;
}

function clearAttachments() {
  for (const item of state.attachments) {
    if (item.preview) URL.revokeObjectURL(item.preview);
  }
  state.attachments = [];
  renderAttachments();
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

/** A small icon button for the hover actions on a conversation row. */
function rowAction(glyph, title, handler, { danger = false } = {}) {
  const button = node("button", "button button--icon button--ghost row__action", glyph);
  button.type = "button";
  button.title = title;
  button.setAttribute("aria-label", title);
  if (danger) button.classList.add("row__action--danger");
  button.addEventListener("click", (event) => {
    // The row underneath is itself a button; without this the click would also
    // open the conversation we are about to archive.
    event.stopPropagation();
    event.preventDefault();
    handler();
  });
  return button;
}

function sessionRow(session, { archived }) {
  const wrap = node("div", "row-wrap");
  const button = node("button", "row");
  button.type = "button";
  button.setAttribute("aria-current", String(state.session?.id === session.id));
  button.append(node("span", "row__icon", archived ? "▤" : "□"));

  const main = node("div", "row__main");
  main.append(node("div", "row__title", session.title));
  const when = new Date(session.updated_at * 1000).toLocaleString();
  main.append(node("div", "row__meta", archived ? `Archived · ${when}` : when));
  button.append(main);
  button.addEventListener("click", () => selectSession(session.id));

  const actions = node("div", "row__actions");
  if (archived) {
    actions.append(
      rowAction("↩", "Restore this conversation", () => restoreSession(session)),
      rowAction("✕", "Delete this conversation for good", () => deleteSessionForever(session), {
        danger: true,
      }),
    );
  } else {
    actions.append(
      rowAction("▤", "Archive — keeps the chat, hides it from the list", () =>
        archiveSession(session),
      ),
    );
  }

  wrap.append(button, actions);
  return wrap;
}

function renderSessions() {
  el.sessionList.replaceChildren();
  el.archiveList.replaceChildren();

  const archived = state.archive.length;
  el.archiveToggle.hidden = !state.project;
  el.archiveCount.textContent = String(archived);
  el.archiveToggle.setAttribute("aria-expanded", String(state.showArchive));
  el.archiveToggle.querySelector(".row__icon").textContent = state.showArchive ? "▾" : "▸";
  const open = state.showArchive && archived > 0;
  el.archiveList.hidden = !open;
  el.archiveFoot.hidden = !open;
  el.archiveToggle.disabled = archived === 0;

  if (!state.project) {
    el.sessionList.append(node("p", "empty", "Open a project first."));
    return;
  }
  if (state.sessions.length === 0) {
    el.sessionList.append(node("p", "empty", "No conversations yet."));
  } else {
    for (const session of state.sessions) {
      el.sessionList.append(sessionRow(session, { archived: false }));
    }
  }
  if (open) {
    for (const session of state.archive) {
      el.archiveList.append(sessionRow(session, { archived: true }));
    }
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

// Reasoning streams as many small deltas. Keeping one row per delta turned the
// activity panel into a wall of single words and rebuilt the whole panel dozens
// of times a second, so deltas are coalesced into one row and the re-render is
// throttled.
const REASONING_MAX_CHARS = 2000;
let reasoningRenderTimer = null;

function pushReasoning(text) {
  if (!text) return;
  const last = state.activity[state.activity.length - 1];
  if (last && last.label === "Reasoning") {
    // Keep the tail: during a stream the newest reasoning is the useful part.
    last.detail = (last.detail + text).slice(-REASONING_MAX_CHARS);
  } else {
    state.activity.push({ label: "Reasoning", detail: text.slice(-REASONING_MAX_CHARS) });
  }
  if (reasoningRenderTimer) return;
  reasoningRenderTimer = setTimeout(() => {
    reasoningRenderTimer = null;
    renderRightbar();
  }, 120);
}

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
      // The server synthesises at this rate. Adopting it is what keeps a
      // configured rate from being decoded as if it were the default.
      if (data.sample_rate && !playback.setServerRate(data.sample_rate)) {
        state.activity.push({
          label: "Speech rate mismatch",
          detail:
            `synthesised at ${data.sample_rate} Hz but the audio output runs at ` +
            `${playback.status.negotiatedRate} Hz. Speech will sound too fast or too slow; ` +
            "reload the page to rebuild the audio output.",
        });
        renderRightbar();
      }
      // Only used when the server could not ask Deepgram for the speed itself.
      if (data.speech_speed) playback.setPlaybackRate(data.speech_speed);
      if (data.environment) {
        state.environment = data.environment;
      }
      if (Array.isArray(data.mcp_failures) && data.mcp_failures.length) {
        // A configured MCP server that did not start is worth telling the user
        // about, but it must not stop the session.
        toast(`MCP: ${data.mcp_failures[0]}`, "error");
      }
      // A resumed session restates its state, so a reconnect mid-turn does not
      // leave the status indicator stuck on whatever it showed before.
      if (data.resumed && data.state) setAgentState(data.state);
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
      // Deepgram refused the speed, so the server asked us to apply it here.
      if (data.speech_speed && data.kind_detail === "speed_fallback") {
        playback.setPlaybackRate(data.speech_speed);
        state.activity.push({
          label: "Speech speed",
          detail: `${data.speech_speed}× applied during playback (the voice rejected it)`,
        });
        renderRightbar();
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
      pushReasoning(data.text);
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

function savedMicrophone() {
  try {
    return localStorage.getItem("surtitle.microphone") || "";
  } catch {
    return "";
  }
}

const capture = new Capture({
  // Honour a previously chosen input, so the browser's default is only used
  // until the user says otherwise.
  deviceId: savedMicrophone(),
  onAudio: (frame) => connection.sendAudio(frame),
  onBackendChange: (backend) => {
    // A backend switch is a degraded mode, not a normal event: the user should
    // know their audio is being captured by the fallback path.
    if (backend === "script-processor") {
      state.activity.push({
        label: "Capture fallback in use",
        detail:
          "The AudioWorklet produced no audio in this browser, so the universal " +
          "ScriptProcessor path is handling capture. Audio still works.",
      });
      renderRightbar();
    }
  },
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

function savedSpeaker() {
  try {
    return localStorage.getItem("surtitle.speaker") || "";
  } catch {
    return "";
  }
}

const playback = new Playback({
  sampleRate: 24000,
  element: document.getElementById("speaker"),
  sinkId: savedSpeaker(),
  onStart: () => {
    setAgentState("speaking");
    // The grace window stops the speaker tail from triggering barge-in before
    // echo cancellation has converged.
    capture.notifyPlayback(true);
    // Record what the output path negotiated, once per spoken turn. Every field
    // here can silence or distort the voice on its own, and all of them can
    // change when audio devices come and go — so an intermittent fault leaves a
    // trail instead of a mystery.
    const status = playback.status;
    console.info("[surtitle] playback started", status);
    state.activity.push({ label: "Speaking", detail: playback.statusLine });
    if (!status.rateMatches) {
      state.activity.push({
        label: "Speech rate mismatch",
        detail: `synthesised at ${status.requestedRate} Hz, output at ${status.negotiatedRate} Hz`,
      });
    }
    renderRightbar();
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
    if (savedSpeaker() && playback.canSelectOutput) {
      const result = await playback.setOutputDevice(savedSpeaker());
      if (!result.ok && result.reason !== "unsupported") {
        console.warn("[surtitle] could not restore the saved output device", result.reason);
      }
    }
    if (!outputReady) {
      toast(
        "Your browser blocked audio playback. Click anywhere on the page, then try again.",
        "error",
      );
    }

    const captureState = await capture.start();  // { running, backend }
    state.micOpen = true;
    el.micButton.dataset.active = "true";
    el.micButton.setAttribute("aria-pressed", "true");
    el.micLabel.textContent = "Listening";
    connection.sendCommand("mic", { open: true });
    setAgentState("listening");
    el.captions.replaceChildren();
    el.captions.append(node("span", "captions__hint", "Listening…"));

    // Record what capture actually negotiated. This is the single most useful
    // diagnostic for a silent microphone, and it is readable in the UI.
    const started = capture.status;
    console.info("[surtitle] microphone opened", started);
    state.activity.push({
      label: "Microphone opened",
      detail:
        `device="${started.deviceLabel || "unknown"}" ` +
        `context=${started.running ? "running" : "NOT RUNNING"} ` +
        `backend=${started.backend} ` +
        `track=${started.trackState}${started.trackMuted ? " MUTED" : ""}`,
    });
    renderRightbar();

    // Distinguish "the microphone is open" from "audio is reaching the app".
    // Without this the two look identical, and a silently suspended audio context
    // is indistinguishable from a mute microphone.
    if (!captureState || captureState.running === false) {
      const problem =
        "The browser is not running audio capture. Click anywhere on the page, then toggle the mic again.";
      el.captions.replaceChildren(node("span", "captions__problem", problem));
      toast(problem, "error");
    }
    window.setTimeout(async () => {
      if (!state.micOpen) return;
      const status = capture.status;
      const device = status.deviceLabel ? ` (using "${status.deviceLabel}")` : "";
      let problem = null;
      if (status.silent) {
        // Name the device: a silent virtual input (BlackHole, Loopback, a
        // conferencing tool) is the most common cause of a microphone that
        // reports success and delivers nothing.
        problem =
          `No audio is reaching the app${device}. Check the browser's microphone ` +
          "permission and that the correct input device is selected.";
        const inputs = await capture.listInputDevices();
        if (inputs.length > 1) {
          const names = inputs.map((d) => d.label).join(" | ");
          state.activity.push({ label: "Audio inputs available", detail: names });
          renderRightbar();
        }
      } else if (status.maxLevel < 0.01) {
        problem = `The microphone is open${device} but the signal is silent. Raise the input level.`;
      }
      console.info("[surtitle] capture check", status);
      state.activity.push({
        label: "Capture check (3.5 s)",
        detail:
          `frames=${status.framesReceived} peak=${status.maxLevel.toFixed(4)} ` +
          `backend=${status.backend} context=${status.running ? "running" : "NOT RUNNING"}`,
      });
      renderRightbar();

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
  state.session = null;
  state.archive = [];
  state.showArchive = false;
  state.turns.clear();
  state.toolRows.clear();
  state.currentTurn = null;
  state.activity = [];
  el.turns.replaceChildren();

  await refreshSessions();
  renderProjects();
  renderHeader();
  await loadFiles(".");

  if (state.sessions.length === 0) {
    await createSession();
  } else {
    await selectSession(state.sessions[0].id);
  }
}

/** Reload both halves of the conversation list from the server. */
async function refreshSessions() {
  if (!state.project) return;
  const data = await api(`/api/projects/${state.project.id}/sessions?archived=all`);
  const all = data.sessions || [];
  state.sessions = all.filter((session) => !session.archived);
  state.archive = all.filter((session) => session.archived);
  renderSessions();
}

/**
 * Reset the open conversation after it was archived or deleted, opening another
 * one so the user is never left staring at a transcript that no longer exists.
 */
async function reopenAfterRemoval() {
  state.session = null;
  el.turns.replaceChildren();
  state.turns.clear();
  state.toolRows.clear();
  state.currentTurn = null;
  state.activity = [];
  if (state.sessions.length) {
    await selectSession(state.sessions[0].id);
  } else {
    await createSession();
  }
}

async function archiveSession(session) {
  try {
    await api(`/api/sessions/${session.id}/archive`, { method: "POST" });
    const wasOpen = state.session?.id === session.id;
    await refreshSessions();
    toast("Conversation archived. Your files are untouched.");
    if (wasOpen) await reopenAfterRemoval();
  } catch (cause) {
    toast(cause.message, "error");
  }
}

async function restoreSession(session) {
  try {
    await api(`/api/sessions/${session.id}/unarchive`, { method: "POST" });
    await refreshSessions();
    toast("Conversation restored.");
  } catch (cause) {
    toast(cause.message, "error");
  }
}

async function deleteSessionForever(session) {
  const ok = await confirmAction({
    title: "Delete this conversation?",
    text: `"${session.title}" and its transcript will be removed permanently.`,
    note: "Only the chat is deleted. Files the agent created stay in the project folder.",
    confirmLabel: "Delete",
  });
  if (!ok) return;
  try {
    const wasOpen = state.session?.id === session.id;
    await api(`/api/sessions/${session.id}`, { method: "DELETE" });
    await refreshSessions();
    toast("Conversation deleted.");
    if (wasOpen) await reopenAfterRemoval();
  } catch (cause) {
    toast(cause.message, "error");
  }
}

async function purgeArchive() {
  const count = state.archive.length;
  const ok = await confirmAction({
    title: `Delete all ${count} archived conversation${count === 1 ? "" : "s"}?`,
    text: "Their transcripts are removed permanently and cannot be recovered.",
    note: "Only chat history is deleted. Files in the project folder are never touched.",
    confirmLabel: "Delete all",
  });
  if (!ok) return;
  try {
    await api(`/api/projects/${state.project.id}/sessions/archived?confirm=true`, {
      method: "DELETE",
    });
    state.showArchive = false;
    await refreshSessions();
    toast(`Deleted ${count} archived conversation${count === 1 ? "" : "s"}.`);
  } catch (cause) {
    toast(cause.message, "error");
  }
}

async function createSession() {
  if (!state.project) return;
  const session = await api(`/api/projects/${state.project.id}/sessions`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  state.sessions.unshift(session);
  renderSessions();
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
      } else if (message.role === "system") {
        // Attachment records and similar notices: context, not something to answer.
        const turn = beginTurn("assistant");
        appendShown(turn, message.content);
      } else {
        const turn = beginTurn("assistant");
        // Show the spoken line first when there was one, then the full text.
        //
        // This previously rendered `spoken` *instead of* `content`, so reopening a
        // conversation showed only the short spoken summary and the real answer —
        // tables, paths, detail — silently vanished. Keeping the two channels
        // distinct across a reload is the reason they are stored separately.
        if (message.spoken && message.spoken.trim()) {
          appendSaid(turn, message.spoken);
        }
        if (message.content && message.content.trim() !== (message.spoken || "").trim()) {
          appendShown(turn, message.content);
        }
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

async function sendMessage() {
  const text = el.composer.value.trim();
  const hasAttachments = state.attachments.length > 0;
  if (!text && !hasAttachments) return;
  if (!state.session) {
    toast("Open a project and conversation first.", "error");
    return;
  }

  let message = text;
  if (hasAttachments) {
    try {
      const stored = await uploadAttachments();
      // Name the paths explicitly. The agent can only read inside the project, so
      // a stored path is the difference between "here is a file" and "here is a
      // file I can actually open".
      const listing = stored.map((entry) => `- ${entry.path}`).join("\n");
      message = `${text ? `${text}\n\n` : ""}Attached files (read them with read_file):\n${listing}`;
    } catch (error) {
      toast(`Could not upload: ${error.message}`, "error");
      return;
    }
  }

  connection.sendCommand("text", { text: message });
  el.composer.value = "";
  el.composer.style.height = "auto";
  clearAttachments();
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
  if (remember && allowed) {
    toast(`Allowed. ${pending.name} will not ask again in this project.`, "ok");
  } else if (!allowed) {
    toast("Rejected. The agent will be told and can try something else.", "ok");
  }
  setAgentState("thinking");
}

// -------------------------------------------------------------------- wiring

el.sendButton.addEventListener("click", () => sendMessage());

el.micButton.addEventListener("click", toggleMic);

el.attachButton.addEventListener("click", () => el.fileInput.click());
el.fileInput.addEventListener("change", () => {
  addFiles(el.fileInput.files);
  el.fileInput.value = "";
});

// Drag and drop over the whole window, with a counter because dragleave fires
// when moving between child elements and would otherwise flicker the overlay.
let dragDepth = 0;
window.addEventListener("dragenter", (event) => {
  if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes("Files")) return;
  event.preventDefault();
  dragDepth += 1;
  el.dropzone.hidden = false;
});
window.addEventListener("dragover", (event) => {
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  event.preventDefault();
});
window.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) el.dropzone.hidden = true;
});
window.addEventListener("drop", (event) => {
  if (!event.dataTransfer) return;
  event.preventDefault();
  dragDepth = 0;
  el.dropzone.hidden = true;
  addFiles(event.dataTransfer.files);
});

// Pasting a screenshot is the fastest way to share one, and a very common
// expectation for an agent UI.
el.composer.addEventListener("paste", (event) => {
  const items = Array.from((event.clipboardData && event.clipboardData.files) || []);
  if (items.length === 0) return;
  event.preventDefault();
  addFiles(items);
});

// Three explicit actions. Remembering a decision was previously a shift-click,
// which nothing advertised and no one would discover.
document.getElementById("approvalAllow").addEventListener("click", () => answerApproval(true, false));
document
  .getElementById("approvalAlways")
  .addEventListener("click", () => answerApproval(true, true));
document.getElementById("approvalDeny").addEventListener("click", () => answerApproval(false, false));

// Keyboard: Enter allows once, Escape rejects. The buttons hold focus after
// answerApproval, so a follow-up tool prompt can be answered without reaching for
// the mouse.
document.addEventListener("keydown", (event) => {
  // A confirmation is modal: it takes Escape first, and must not fall through to
  // the approval shortcuts behind it.
  if (!el.confirmModal.hidden) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeConfirm(false);
    }
    return;
  }
  if (!state.pendingApproval) return;
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    answerApproval(true, false);
  } else if (event.key === "Escape") {
    event.preventDefault();
    answerApproval(false, false);
  }
});

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

// A right-click on the mic opens the device picker directly: choosing an input is
// a frequent action when a machine has several, and digging through Settings for
// it is friction.
el.micButton.addEventListener("contextmenu", (event) => {
  event.preventDefault();
  settings.activeSection = "microphone";
  settings.open();
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

// ------------------------------------------------------------- confirmation

let confirmResolver = null;

/**
 * Ask before anything irreversible. Resolves true to go ahead, false to cancel.
 * The note line always explains what is *not* affected, because "delete the
 * conversation" should never read as "delete my work".
 */
function confirmAction({ title, text, note, confirmLabel = "Confirm" }) {
  el.confirmTitle.textContent = title;
  el.confirmText.textContent = text;
  el.confirmNote.textContent = note || "";
  el.confirmNote.hidden = !note;
  el.confirmOk.textContent = confirmLabel;
  el.confirmModal.hidden = false;
  el.confirmOk.focus();
  return new Promise((resolve) => {
    confirmResolver = resolve;
  });
}

function closeConfirm(result) {
  if (el.confirmModal.hidden && !confirmResolver) return;
  el.confirmModal.hidden = true;
  const resolve = confirmResolver;
  confirmResolver = null;
  if (resolve) resolve(result);
}

document.getElementById("confirmOk").addEventListener("click", () => closeConfirm(true));
document.getElementById("confirmCancel").addEventListener("click", () => closeConfirm(false));
document.getElementById("confirmMask").addEventListener("click", () => closeConfirm(false));

document.getElementById("archiveToggle").addEventListener("click", () => {
  state.showArchive = !state.showArchive;
  renderSessions();
});
el.archivePurge.addEventListener("click", purgeArchive);
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

/** Apply a microphone change without requiring a page reload. */
async function applyMicrophoneChange(deviceId) {
  capture.setDevice(deviceId);
  if (!state.micOpen) return;
  // Restart capture so the new device takes effect now rather than on next open.
  try {
    await capture.stop();
    await capture.start();
    el.captions.replaceChildren(
      node("span", "captions__hint", "Listening on the new device…"),
    );
  } catch (error) {
    toast(`Could not switch microphone: ${error.message}`, "error");
  }
}

const settings = new SettingsPanel({
  playback,
  onMicrophoneChange: (deviceId) => {
    applyMicrophoneChange(deviceId);
  },
  onSpeakerChange: (deviceId) => playback.setOutputDevice(deviceId),
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
