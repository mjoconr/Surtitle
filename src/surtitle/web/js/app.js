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
import { markdownToHtml } from "./markdown.js";
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

/**
 * Where a stored answer stops being an answer and becomes the log of the work.
 *
 * The server appends the turn's tool activity to the message it stores, because
 * that listing is how the *model* remembers what it already did. It is not part of
 * what the person was told, and it is not written to be read: it is one line per
 * call, unbounded, and on a long turn it is far longer than the answer above it.
 * Rendering it inline put a wall of `run_shell(...)` under every reopened reply —
 * the transcripts that most needed reading were the ones it buried.
 */
const WORK_LOG_MARKER = "[work this turn]";

const state = {
  projects: [],
  sessions: [],
  // Conversations the user filed away: kept, but out of the sidebar and out of
  // the agent's history search until restored.
  archive: [],
  showArchive: false,
  project: null,
  session: null,
  // View state is per conversation. It used to be global and cleared on every
  // switch, which threw away the rows a background conversation was still
  // building — so its `tool_call` found no matching row and its progress was lost.
  views: new Map(),
  currentTurn: null,
  files: { path: ".", entries: [] },
  activity: [],
  // The agent's plan for the work in hand: [description, status] rows it keeps
  // current with the todo_write tool. Reloaded with the conversation, because a
  // plan that forgets itself on refresh is worse than none.
  todos: [],
  goal: null,
  micOpen: false,
  // True while the server is discarding transcripts as the agent's own voice.
  echoSuppressed: false,
  // What every open conversation is doing, keyed by session id, so the sidebar
  // can show progress in one that is not on screen.
  sessionActivity: new Map(),
  // Set from the server's ready event; false disables the mic button.
  voiceAvailable: true,
  pendingApproval: null,
  // A stop notice held back until the live conversation has been heard from.
  pendingStopNote: null,
  // The last thing the user asked for, so a step-limited turn can be resumed
  // without retyping it.
  lastUserText: "",
  settings: null,
  // Which panel is open, remembered per conversation: one conversation may be
  // about a plan while another is one you are watching the process of, and
  // switching between them should not re-open the wrong view. Plan leads.
  rightTab: "todo",
  sessionTabs: new Map(),
  // Which step's reasoning the Thinking tab is showing, when the reader has picked
  // one. Null means "follow the newest", which is where they want to be while the
  // agent is working; choosing an older step pins it until the next turn starts.
  thinkingStep: null,
  // Files this turn has read or written, so the Files panel can lead with what
  // the agent actually touched instead of an undifferentiated project tree.
  touched: new Map(),
  // The project notebook, fetched when its tab is first opened. Null means "not
  // read yet" rather than "empty", so an empty notebook can say so properly.
  notes: null,
  // The last completion's usage, and the two numbers it is judged against: the
  // budget we intend to send within, and the model's own window. Both are cleared
  // when the conversation changes — a budget meter showing another conversation's
  // numbers is a lie.
  usage: null,
  contextBudget: 0,
  contextWindow: 0,
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
  rightbarMeter: document.getElementById("rightbarMeter"),
  meterFill: document.getElementById("meterFill"),
  meterLabel: document.getElementById("meterLabel"),
  modelBadge: document.getElementById("modelBadge"),
  stopButton: document.getElementById("stopButton"),
  pushButton: document.getElementById("pushButton"),
  approvalStrip: document.getElementById("approvalStrip"),
  approvalText: document.getElementById("approvalText"),
  approvalActions: document.getElementById("approvalActions"),
  stopNote: document.getElementById("stopNote"),
  stopTitle: document.getElementById("stopTitle"),
  stopDetail: document.getElementById("stopDetail"),
  stopContinue: document.getElementById("stopContinue"),
  stopDismiss: document.getElementById("stopDismiss"),
  toast: document.getElementById("toast"),
  attachments: document.getElementById("attachments"),
  fileInput: document.getElementById("fileInput"),
  attachButton: document.getElementById("attachButton"),
  dropzone: document.getElementById("dropzone"),
  folderModal: document.getElementById("folderModal"),
  folderCrumbs: document.getElementById("folderCrumbs"),
  folderUp: document.getElementById("folderUp"),
  folderHidden: document.getElementById("folderHidden"),
  folderCount: document.getElementById("folderCount"),
  folderPath: document.getElementById("folderPath"),
  folderList: document.getElementById("folderList"),
  folderNote: document.getElementById("folderNote"),
  folderNewName: document.getElementById("folderNewName"),
  folderNew: document.getElementById("folderNew"),
  folderUse: document.getElementById("folderUse"),
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

/**
 * Durations, in the shortest form that is still readable.
 *
 * A turn that shells out to a build server runs for minutes, and "312.4s" makes
 * the reader do arithmetic. Sub-second work still shows milliseconds, because
 * that is the number that says a command was instant rather than slow.
 */
function formatDuration(ms) {
  const total = Math.max(0, Math.round(Number(ms) || 0));
  if (total < 1000) return `${total} ms`;
  const seconds = total / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const whole = Math.floor(seconds);
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/**
 * One ticker for every live timer on the page.
 *
 * Running per-element intervals would multiply with the number of open steps and
 * tool rows, and each would be its own timer to clear on a turn boundary. One
 * interval walks the live elements and writes the elapsed time in place, which
 * keeps the text changing without re-rendering any of the rows it belongs to.
 */
let timerTicker = null;

function startTimers() {
  if (timerTicker) return;
  timerTicker = setInterval(updateTimers, 500);
  updateTimers();
}

function stopTimers() {
  if (timerTicker) clearInterval(timerTicker);
  timerTicker = null;
}

function updateTimers() {
  const now = Date.now();
  let live = false;
  const open = view();
  if (!open) return;
  for (const turn of open.turns.values()) {
    for (const step of turn.stepRows.values()) {
      if (step.end) {
        step.timer.textContent = formatDuration(step.end - step.start);
        continue;
      }
      live = true;
      step.timer.textContent = formatDuration(now - step.start);
    }
  }
  for (const record of open.toolRows.values()) {
    if (!record.startedAt || !record.timer) continue;
    if (record.endedAt) continue;
    live = true;
    record.timer.textContent = formatDuration(now - record.startedAt);
  }
  updateWorkingLine(now, live);
  // The caller decides when to stop, not this function. `finishTimers` runs
  // before `done` clears `state.currentTurn`, so nothing here could tell that the
  // turn had ended — and a ticker left running writes to a transcript that is no
  // longer live, once every 500 ms, for the life of the page.
}

/**
 * "Reading the notes… 14m 38s" — the line that says what the turn is doing.
 *
 * A turn that thinks and calls tools for ten minutes in silence is
 * indistinguishable from one that has died, and the state pill alone says
 * "Thinking" whether that has been true for one second or twenty minutes. What
 * it is *doing* is the part that makes the wait legible: a generic verb plus a
 * clock is the same information as the spinner it replaced.
 */
function updateWorkingLine(now, anyLive) {
  const turn = state.currentTurn;
  const root = turn && turn.kind === "assistant" ? turn.root : null;
  // A turn that has ended is not working, whatever a leftover row says. Without
  // this the line came back: `finishTimers` takes it down and then calls
  // `updateTimers` to write the frozen durations, so an un-ended row from an
  // earlier turn (or one replayed from the store) put "Deep diving…" straight
  // back up — and the ticker was stopped immediately afterwards, so it sat there
  // frozen, claiming the agent was still working after it had finished.
  if (!root || !anyLive || turn.endedAt) {
    if (workingLine && workingLine.parentNode) workingLine.remove();
    workingLine = null;
    return;
  }
  if (!workingLine) {
    workingLine = node("div", "working");
    workingLine.append(node("span", "working__label", "Deep diving"));
    workingLine.append(node("span", "working__dots", ""));
    workingLine.append(node("span", "working__time", ""));
  }
  if (workingLine.parentNode !== root) root.append(workingLine);
  workingLine.querySelector(".working__label").textContent = describeWorking(turn);
  workingLine.querySelector(".working__time").textContent = formatDuration(now - turn.startedAt);
}

/**
 * What the turn is doing, in as few words as are honest.
 *
 * A call in flight is the most specific thing that can be said — it names the
 * tool and, through it, whether the agent is reading, searching or running
 * something. Failing that, the step's own summary of what it has called so far.
 * Only this turn's rows count: the tool map belongs to the conversation, so a
 * row left running by an earlier turn would otherwise be reported as current.
 */
function describeWorking(turn) {
  for (const record of turn.toolRows.values()) {
    if (record.startedAt && !record.endedAt && record.startedAt >= turn.startedAt) {
      return `Running ${record.name}`;
    }
  }
  const step = turn.stepRows.get(currentStepIndex());
  const summary = step && step.summary ? step.summary.textContent : "";
  if (summary && summary !== "thinking…") return summary;
  return "Deep diving";
}

let workingLine = null;

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

/**
 * The transcript is a turn → step → item tree.
 *
 * A *step* is one model round: it thinks, then calls zero or more tools. The
 * server numbers them and puts that number on every thinking delta, tool call
 * and tool result, so each event knows where it belongs without the client
 * having to infer boundaries.
 *
 * Grouping this way is what makes the process legible. Flattened, a turn that
 * ran fourteen commands was fourteen identical rows — which is exactly how a
 * long release turn read: a wall of `run_shell` with no indication of what was
 * being attempted or why. Grouped, each step carries the reasoning that produced
 * its commands, and the commands sit under it.
 *
 * A step is created lazily by the first event that has content for it: the
 * server announces a step before the model has produced anything, and creating
 * the row then would leave empty steps behind on a cancelled turn.
 */
/** The view state of one conversation, created on first use. */
function viewFor(sessionId) {
  let view = state.views.get(sessionId);
  if (!view) {
    view = { turns: new Map(), toolRows: new Map() };
    state.views.set(sessionId, view);
  }
  return view;
}

/** The view state of the open conversation, or null when nothing is open. */
function view() {
  return state.session ? viewFor(state.session.id) : null;
}

/** Start a conversation's view over, leaving every other one alone. */
function resetView(sessionId) {
  if (!sessionId) return;
  const existing = state.views.get(sessionId);
  if (existing) {
    existing.turns.clear();
    existing.toolRows.clear();
  } else {
    state.views.set(sessionId, { turns: new Map(), toolRows: new Map() });
  }
}

function beginTurn(kind) {
  const open = view();
  const turn = node("div", "turn");
  const spoken = node("div");
  const shown = node("div");
  shown.style.display = "flex";
  shown.style.flexDirection = "column";
  shown.style.gap = "8px";
  const steps = node("div", "turn__steps");

  if (kind === "user") {
    const bubble = node("div", "bubble bubble--user");
    turn.append(bubble);
    el.turns.append(turn);
    scrollToBottom();
    const record = {
      id: Symbol("turn"),
      kind,
      root: turn,
      toolRows: open.toolRows,
      bubble,
      spoken,
      shown,
      steps,
      stepRows: new Map(),
      startedAt: Date.now(),
      endedAt: null,
      saidText: "",
      shownText: "",
    };
    if (open) open.turns.set(record.id, record);
    return record;
  }

  // The work first, the answer last.
  //
  // This is the order the turn happened in and the order it reads in: the steps,
  // with the tool calls they contain, at the top; then whatever the agent chose to
  // *show* — a table, a list of options — and the spoken answer at the bottom,
  // where a reply belongs. It used to be the reverse: the answer was announced at
  // the top and the steps accumulated underneath it, so the line being spoken
  // drifted further from the reader's eye as the turn went on. `spoken` and `shown`
  // are containers built here and filled as events land, so their position is a
  // decision made once rather than a consequence of arrival order.
  turn.append(steps, shown, spoken);
  el.turns.append(turn);
  scrollToBottom();
  // A new turn's reasoning is what the Thinking tab should be showing, even if the
  // reader had picked an older step to read — the panel follows the work again.
  state.thinkingStep = null;
  const record = {
    id: Symbol("turn"),
    kind,
    root: turn,
    toolRows: open.toolRows,
    bubble: null,
    spoken,
    shown,
    steps,
    stepRows: new Map(),
    startedAt: Date.now(),
    endedAt: null,
    saidText: "",
    shownText: "",
  };
  if (open) open.turns.set(record.id, record);
  return record;
}

/**
 * The step a step-numbered event belongs to, created if it is new.
 *
 * Events without a number (an older server, or a tool row rebuilt from the
 * store's flat list) land in the most recent step, which is the only sensible
 * place for them.
 */
function stepFor(turn, stepNumber) {
  if (!turn || turn.kind !== "assistant") return null;
  let index = Number(stepNumber);
  if (!Number.isFinite(index) || index <= 0) {
    index = turn.stepRows.size ? Math.max(...turn.stepRows.keys()) : 1;
  }
  const existing = turn.stepRows.get(index);
  if (existing) return existing;

  const root = node("div", "step");
  root.dataset.state = "running";
  root.dataset.step = String(index);

  const head = node("button", "step__head");
  head.type = "button";
  head.setAttribute("aria-expanded", "true");
  head.append(node("span", "step__index", String(index)));
  const summary = node("span", "step__summary", "thinking…");
  head.append(summary);
  const timer = node("span", "step__timer", "");
  head.append(timer);

  const body = node("div", "step__body");
  const thinking = node("div", "step__thinking");
  const tools = node("div", "step__tools");

  body.append(thinking, tools);
  root.append(head, body);

  head.addEventListener("click", () => {
    const open = head.getAttribute("aria-expanded") === "true";
    head.setAttribute("aria-expanded", String(!open));
    body.hidden = open;
  });

  const record = {
    index,
    root,
    head,
    summary,
    timer,
    body,
    thinking,
    tools,
    toolRows: turn.toolRows,
    think: null,
    start: Date.now(),
    end: null,
    toolCount: 0,
    counts: new Map(),
  };

  // The step being worked on is the one to watch, so it starts open; earlier
  // steps collapse as the turn moves on, which keeps a long turn from becoming
  // a page of open panels.
  if (turn.stepRows.size > 0) {
    head.setAttribute("aria-expanded", "false");
    body.hidden = true;
  }
  for (const earlier of turn.stepRows.values()) collapseStep(earlier);
  turn.stepRows.set(index, record);
  turn.steps.append(root);
  if (turn.toolRows) turn.toolRows.set(`step:${index}`, record);
  scrollToBottom();
  return record;
}

function collapseStep(step) {
  if (!step || step.root.dataset.state === "running") return;
  step.head.setAttribute("aria-expanded", "false");
  step.body.hidden = true;
}

function noteStepTool(step, name) {
  if (!step) return;
  step.toolCount += 1;
  step.counts.set(name, (step.counts.get(name) || 0) + 1);
  step.summary.textContent = planSummary(step);
  // The first call is the moment the step stops considering and starts acting.
  // The reasoning that produced it is settled, so it folds down to its one-line
  // summary and the call takes the space: a long turn otherwise becomes a wall
  // of open thinking with the work buried underneath it.
  if (step.toolCount === 1) collapseThink(step.think);
}

/** Fold a Think block down to its one-line summary. */
function collapseThink(think) {
  if (!think) return;
  think.head.setAttribute("aria-expanded", "false");
  think.body.hidden = true;
}

/** "14 commands", "read_file ×3, run_shell ×2", etc. */
function planSummary(step) {
  if (!step || step.toolCount === 0) return "thinking…";
  if (step.counts.size === 1) {
    const [name, count] = [...step.counts.entries()][0];
    return count === 1 ? name : `${name} ×${count}`;
  }
  const parts = [...step.counts.entries()].map(([name, count]) =>
    count === 1 ? name : `${name} ×${count}`,
  );
  return `${step.toolCount} calls · ${parts.join(", ")}`;
}

/**
 * One Think block inside a step.
 *
 * Deltas are coalesced into the same block rather than one row each: a row per
 * delta turned this into a wall of single words and rebuilt the DOM dozens of
 * times a second. The full text is kept here because a Think block is only
 * rendered when someone opens it — the cap is only for what the server stores.
 */
function addThinking(turn, text, stepNumber) {
  if (!text) return;
  const step = stepFor(turn, stepNumber);
  if (!step) return;
  if (!step.think) {
    const wrap = node("div", "think");
    const head = node("button", "think__head");
    head.type = "button";
    head.setAttribute("aria-expanded", "true");
    head.append(node("span", "think__chevron", "▾"));
    head.append(node("span", "think__title", "Think"));
    const peek = node("span", "think__peek", "");
    head.append(peek);
    const body = node("div", "think__body");
    head.addEventListener("click", () => {
      const open = head.getAttribute("aria-expanded") === "true";
      head.setAttribute("aria-expanded", String(!open));
      body.hidden = open;
    });
    wrap.append(head, body);
    step.thinking.append(wrap);
    step.think = { wrap, head, peek, body, text: "" };
  }
  step.think.text += text;
  step.think.body.textContent = step.think.text;
  // A one-line gist, so a collapsed step still says what was being considered.
  // Written once so the panel does not churn on every token; `scheduleThinkPeek`
  // is what keeps it current from then on.
  if (!step.think.peek.textContent) {
    step.think.peek.textContent = gistOf(step.think.text);
    // The Thinking panel renders from `state.activity`, not from the transcript's
    // DOM, so the gist is recorded there too — otherwise its Think rows are
    // silently empty and the panel shows commands with no reasoning above them.
    pushActivity({
      kind: "think",
      step: step.index,
      at: step.start,
      label: "Think",
      detail: step.think.peek.textContent,
    });
  }
  scheduleThinkPeek(step.think);
}

let thinkPeekTimer = null;
let thinkPeekTarget = null;

/**
 * Keep a Think block's collapsed summary moving, without a write per token.
 *
 * The peek is what a folded block shows, and it used to be written exactly once
 * — so a block that had been open and then folded showed whatever its first line
 * had been, possibly minutes earlier. Only one step streams at a time, so one
 * throttled write is enough; the delay is what keeps this off the hot path that
 * reasoning deltas arrive on.
 */
function scheduleThinkPeek(think) {
  if (!think) return;
  thinkPeekTarget = think;
  if (thinkPeekTimer) return;
  thinkPeekTimer = setTimeout(() => {
    thinkPeekTimer = null;
    const target = thinkPeekTarget;
    thinkPeekTarget = null;
    if (target && target.peek) target.peek.textContent = latestLineOf(target.text);
  }, 250);
}

/** "24.3k", "384k", "1M" — a token count at a glance, where exact is not the point. */
function formatTokens(count) {
  const value = Number(count) || 0;
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 100_000 ? 1 : 0)}k`;
  const millions = value / 1_000_000;
  return `${millions >= 10 ? Math.round(millions) : millions.toFixed(1).replace(/\.0$/, "")}M`;
}

/**
 * How full the model's context window is.
 *
 * Nothing showed this, and the one number that was on screen answered a
 * different question: the model badge carried a running total of every token the
 * process had ever sent, which grows forever and says nothing about the
 * conversation in front of you. The window is what explains an agent that starts
 * forgetting its own work, and it is the number that tells you a conversation has
 * done its job and the next one should start fresh.
 */
function renderContextMeter() {
  const usage = state.usage;
  // Measured against the working budget, not the model's window: the window says
  // what is possible, the budget says what this app intends to send, and the
  // second is the number worth watching.
  const budget = Number(state.contextBudget) || 0;
  if (!usage || !budget) {
    el.rightbarMeter.hidden = true;
    return;
  }
  const sent = Number(usage.prompt_tokens || 0);
  const reply = Number(usage.completion_tokens || 0);
  const used = sent + reply;
  const share = Math.min(1, used / budget);
  // How much of what we sent the provider already had cached. It is the cost
  // story in one number: a cache hit is a fiftieth of a miss, so this falling
  // means something at the *front* of the request changed and invalidated the
  // prefix — which is exactly what the plan, the notebook and the file listing
  // used to do from inside the system prompt.
  const cached = Math.min(Number(usage.cached_tokens || 0), sent);
  const hitRate = sent > 0 ? cached / sent : 0;
  el.rightbarMeter.hidden = false;
  el.rightbarMeter.dataset.level = share >= 0.9 ? "high" : share >= 0.7 ? "warm" : "ok";
  el.meterFill.style.width = `${(share * 100).toFixed(1)}%`;
  el.meterLabel.textContent =
    `${formatTokens(used)} / ${formatTokens(budget)}` +
    (sent ? ` · cache ${Math.round(hitRate * 100)}%` : "");
  const window_ = Number(state.contextWindow) || 0;
  el.rightbarMeter.title =
    `This conversation is carrying about ${used.toLocaleString()} tokens of a ` +
    `${budget.toLocaleString()}-token working budget (${sent.toLocaleString()} sent, ` +
    `${reply.toLocaleString()} written).` +
    (sent
      ? ` ${cached.toLocaleString()} of the sent tokens were already cached ` +
        `(${Math.round(hitRate * 100)}%), which is what keeps a long conversation cheap.`
      : "") +
    (window_ ? ` The model itself accepts ${window_.toLocaleString()}.` : "") +
    " The oldest turns fall out of the model's view as it fills, so start a new" +
    " conversation when the work moves on.";
}

/** First line of some thinking, trimmed to something a row can show. */
function gistOf(text) {
  const line = String(text || "")
    .split("\n")
    .map((part) => part.trim())
    .find((part) => part.length > 0);
  if (!line) return "";
  return line.length > 120 ? `${line.slice(0, 120)}…` : line;
}

/**
 * The most recent line of some still-streaming reasoning.
 *
 * `gistOf` takes the *first* line, which is right for a step that has finished:
 * it says what the step set out to do. It is the wrong line while the step is
 * still running — the first line stops changing within a second, so a panel
 * built from it looks frozen even though the model is still thinking. The newest
 * line is the one that moves, and the newest words are at the end, so the
 * ellipsis goes in front.
 */
function latestLineOf(text) {
  const lines = String(text || "")
    .split("\n")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  const line = lines.length ? lines[lines.length - 1] : "";
  if (!line) return "";
  return line.length > 120 ? `…${line.slice(-120)}` : line;
}

function assistantTurn() {
  if (state.currentTurn && state.currentTurn.kind === "assistant") {
    return state.currentTurn;
  }
  // A new assistant turn means the message that was waiting behind the last one
  // has started, so its "queued" marker is no longer true.
  clearQueuedMarkers();
  const turn = beginTurn("assistant");
  state.currentTurn = turn;
  return turn;
}

/** Drop the queued marker from messages whose turn has now begun. */
function clearQueuedMarkers() {
  for (const bubble of el.turns.querySelectorAll('.bubble[data-queued="true"]')) {
    delete bubble.dataset.queued;
  }
}

/**
 * "Learned" — a note this turn wrote to the project notebook.
 *
 * The notebook is the app's real memory: it is injected at the start of every
 * later conversation, which makes it the most durable thing a turn can produce.
 * It was also entirely invisible, so the one lasting result never appeared in the
 * conversation that produced it. Shown as a conclusion rather than as a tool
 * call, because that is what it is — the `remember` call itself still appears
 * below with everything else the turn ran.
 */
function appendLearned(turn, text) {
  const body = String(text || "").trim();
  if (!body) return;
  const block = node("div", "learned");
  block.append(node("div", "learned__title", "Learned"));
  block.append(node("div", "learned__text", body));
  turn.root.append(block);
  scrollToBottom();
}

/**
 * "No result" — the closing section for a turn that ended without one.
 *
 * A turn that stops mid-work leaves the transcript looking merely unfinished:
 * the last thing on screen is a Think block, which reads as "still going". The
 * banner above the composer says what happened, but the transcript is what a
 * reopened conversation shows and where the eye already is, so the ending is
 * written into it as well. Without this, a conversation whose last turn produced
 * no answer simply stops — reported as "there is no closing section for a result".
 */
function appendNoAnswer(turn, detail) {
  const block = node("div", "noresult");
  block.append(node("div", "noresult__title", "No result — the turn ended here"));
  block.append(
    node(
      "div",
      "noresult__text",
      detail ||
        "It stopped without answering. The work it did is above; continue to let it carry on.",
    ),
  );
  turn.root.append(block);
  scrollToBottom();
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
  turn.shownText += text;
  const last = turn.shown.lastElementChild;
  let block;
  // Consecutive display chunks belong to one block unless a tool row or a
  // spoken line intervened, which is the visual promise of the speak layer.
  if (last && last.dataset.kind === "shown") {
    block = last;
    block.rawText = (block.rawText || "") + text;
  } else {
    block = node("div", "shown");
    block.dataset.kind = "shown";
    block.rawText = text;
    turn.shown.append(block);
  }
  // Markdown, re-rendered from the whole of what has arrived rather than appended
  // per delta: a table is only a table once its separator row is in, and half a
  // fence is not a fence. The display channel is a table or a listing written once
  // at the end of a turn, so this is a handful of small renders, not a hot path.
  //
  // The raw text is kept on the node rather than in `dataset` so it does not also
  // sit in the DOM as a copy of itself.
  block.innerHTML = markdownToHtml(block.rawText);
}

/** An answer as it was stored: what it showed, and the log of the work under it. */
function splitWorkLog(content) {
  const text = content || "";
  const at = text.indexOf(WORK_LOG_MARKER);
  if (at === -1) return { shown: text, log: "" };
  return {
    shown: text.slice(0, at).trim(),
    log: text.slice(at + WORK_LOG_MARKER.length).trim(),
  };
}

/**
 * Render a stored answer: what it showed, and the work log folded away.
 *
 * Used wherever a *stored* message is put on screen — reopening a conversation,
 * and recovering a reply whose events never arrived. The live path never needs it:
 * while a turn is running the work is on screen as steps and tool rows, which is
 * what the log summarises.
 */
function appendStoredAnswer(turn, content, spoken) {
  const { shown, log } = splitWorkLog(content);
  if (shown && shown !== (spoken || "").trim()) appendShown(turn, shown);
  appendWorkLog(turn, log);
}

/**
 * The turn's tool activity, folded shut.
 *
 * Folded because it is a record rather than a reply: eleven `run_shell(...)` lines
 * after every answer is the transcript burying itself. It goes inside the steps
 * rather than after the answer, so the closed version still reads top-down — the
 * work, then what was shown, then what was said.
 */
function appendWorkLog(turn, log) {
  if (!log) return;
  const lines = log.split("\n").filter((line) => line.trim());
  const details = node("details", "worklog");
  const summary = node("summary", "worklog__summary");
  const count = lines.length;
  summary.textContent = `Work this turn · ${count} action${count === 1 ? "" : "s"}`;
  const body = node("pre", "worklog__body", lines.join("\n"));
  details.append(summary, body);
  turn.steps.append(details);
}

function appendError(turn, message, kind) {
  const block = node("p", "error", message);
  // Where it came from, so a notice that stops being true can be taken down. A
  // voice problem is the one error here that reports a state rather than an event:
  // it stays true until something retracts it.
  if (kind) block.dataset.kind = kind;
  turn.root.append(block);
  return block;
}

/**
 * Retract the voice problem notices.
 *
 * A dropped recognition socket is reported and then reconnected, and the notice
 * used to stay on screen afterwards — "speech recognition is unavailable, check
 * your Deepgram key" — while transcripts were arriving normally. Only the ones this
 * event is about are removed: a turn that failed is still a turn that failed.
 */
function clearVoiceProblems() {
  for (const block of document.querySelectorAll('.error[data-kind="voice"]')) {
    block.remove();
  }
}

function addToolRow(step, callId, name) {
  const rows = (step && step.toolRows) || (view() && view().toolRows) || new Map();
  const existing = callId ? rows.get(callId) : null;
  if (existing) {
    // An approval prompt already opened a row for this call. Reuse it: appending
    // a second row leaves the first orphaned in the transcript, still reading
    // "awaiting approval" long after the question was answered.
    existing.name = name;
    existing.row.dataset.tool = name;
    existing.row.dataset.state = "running";
    existing.nameNode.textContent = name;
    existing.summary.textContent = "running…";
    existing.body.textContent = "";
    existing.body.hidden = true;
    existing.startedAt = Date.now();
    existing.endedAt = null;
    rows.set(callId, existing);
    return existing;
  }

  const row = node("div", "toolrow");
  row.dataset.state = "running";
  row.dataset.tool = name;

  const head = node("button", "toolrow__head");
  head.type = "button";
  head.append(node("span", "toolrow__dot"));
  const nameNode = node("span", "toolrow__name", name);
  head.append(nameNode);
  const summary = node("span", "toolrow__summary", "running…");
  head.append(summary);
  // A running call shows its own elapsed time, so a slow command is visibly
  // making progress rather than looking like the agent has stopped.
  const timer = node("span", "toolrow__timer", "");
  head.append(timer);

  const body = node("div", "toolrow__body");
  body.hidden = true;

  head.addEventListener("click", () => {
    body.hidden = !body.hidden;
  });

  row.append(head, body);
  if (step) step.tools.append(row);
  scrollToBottom();

  const record = {
    row,
    summary,
    body,
    name,
    nameNode,
    timer,
    startedAt: Date.now(),
    endedAt: null,
  };
  rows.set(callId, record);
  return record;
}

/** Settle a tool row with its outcome and how long it took. */
function settleToolRow(record, data) {
  const elapsed = data.duration_ms ?? (record.startedAt ? Date.now() - record.startedAt : 0);
  record.endedAt = Date.now();
  record.row.dataset.state = data.ok ? "ok" : "error";
  record.summary.textContent = data.display || data.error || (data.ok ? "done" : "failed");
  record.timer.textContent = formatDuration(elapsed);
  record.body.textContent = [
    data.display,
    data.error,
    elapsed ? formatDuration(elapsed) : "",
  ]
    .filter(Boolean)
    .join("\n");
  // Surface failures open, because a collapsed error is easy to miss.
  if (!data.ok) record.body.hidden = false;
}

/**
 * Settle a row whose approval never resolved.
 *
 * A prompt that is answered, declined or cancelled always ends in a tool event
 * that updates the row. A turn that dies while a prompt is open does not, so the
 * row is left claiming to be waiting for an answer that can no longer arrive.
 */
function settlePendingApproval(note) {
  const pending = state.pendingApproval;
  if (!pending) return;
  const record = view()?.toolRows.get(pending.call_id);
  if (record && record.row.dataset.state === "awaiting") {
    record.row.dataset.state = "stopped";
    record.summary.textContent = note;
  }
}

function scrollToBottom(force = false) {
  const nearBottom =
    el.transcript.scrollHeight - el.transcript.scrollTop - el.transcript.clientHeight < 160;
  if (force || nearBottom) {
    el.transcript.scrollTop = el.transcript.scrollHeight;
  }
}

/**
 * Land on the newest message, whatever the reader's position was.
 *
 * `scrollToBottom` deliberately refuses to move the view once someone has
 * scrolled up to read, so that streaming text does not yank the page. Replaying a
 * stored conversation is the opposite situation: the transcript has just been
 * replaced, so the view starts at the top and a long conversation opened showing
 * its *oldest* messages.
 *
 * Smooth scrolling is suspended for the whole replay, not just the final jump.
 * The stylesheet sets `scroll-behavior: smooth`, so building sixty turns started
 * sixty animations; one was still in flight afterwards and fought the jump, and
 * assigning `scrollTop` to move the view was itself animated rather than instant.
 * Measured on a 60-message conversation, that left the view ~1,600 px short.
 */
async function replayThenJumpToLatest(build) {
  const transcript = el.transcript;
  const previous = transcript.style.scrollBehavior;
  transcript.style.scrollBehavior = "auto";
  try {
    build();
  } finally {
    const toEnd = () => {
      transcript.scrollTop = transcript.scrollHeight;
    };
    toEnd();
    // Again after layout: wrapped text and web fonts change heights, and the
    // scrollbar appearing changes the viewport.
    await new Promise((resolve) => requestAnimationFrame(resolve));
    toEnd();
    await new Promise((resolve) => requestAnimationFrame(resolve));
    toEnd();
    transcript.style.scrollBehavior = previous;
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
    const wrap = node("div", "row-wrap");
    const button = node("button", "row");
    button.type = "button";
    button.setAttribute("aria-current", String(state.project?.id === project.id));
    button.append(node("span", "row__icon", "▸"));
    const main = node("div", "row__main");
    main.append(node("div", "row__title", project.name));
    main.append(node("div", "row__meta", project.exists ? project.root : "folder missing"));
    button.append(main);
    button.addEventListener("click", () => selectProject(project.id));

    // Removing a project is only ever about Surtitle's own records, so both
    // actions say what happens to the folder rather than leaving it implied.
    const actions = node("div", "row__actions");
    actions.append(
      rowAction("✎", "Rename this project", () => renameProject(project)),
      rowAction(
        "✕",
        "Delete this project and its conversations — the folder itself is kept",
        () => deleteProject(project),
        { danger: true },
      ),
    );
    wrap.append(button, actions);
    el.projectList.append(wrap);
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
  button.dataset.session = session.id;
  const activity = state.sessionActivity.get(session.id);
  if (activity) {
    button.dataset.working = String(Boolean(activity.working));
    button.dataset.approval = String(Boolean(activity.awaitingApproval));
    button.dataset.unread = String(Boolean(activity.unread));
  }
  button.append(node("span", "row__icon", archived ? "▤" : "□"));

  const main = node("div", "row__main");
  main.append(node("div", "row__title", session.title));
  const when = new Date(session.updated_at * 1000).toLocaleString();
  main.append(node("div", "row__meta", archived ? `Archived · ${when}` : when));
  const badge = node("span", "row__badge", "");
  badge.hidden = !activity?.awaitingApproval && !activity?.unread;
  if (activity?.awaitingApproval) badge.textContent = "needs you";
  else if (activity?.unread) badge.textContent = "reply";
  button.append(main, badge);
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
    // Both are offered on a live conversation: filing it away is the cautious
    // choice, and deleting one you have just finished with should not require
    // archiving it first.
    actions.append(
      rowAction("▤", "Archive — keeps the chat, hides it from the list", () =>
        archiveSession(session),
      ),
      rowAction("✕", "Delete this conversation for good", () => deleteSessionForever(session), {
        danger: true,
      }),
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

/**
 * A note in the Activity panel.
 *
 * Entries carry the step they belong to, so the panel is a running list of the
 * turn's process — the same steps the transcript shows — rather than a flat log
 * where "Called run_shell" and "Voice engines" are neighbours. `kind` lets the
 * panel group and icon them; a note without a step is installation or voice
 * diagnostics, which belong to no step and are listed separately.
 */
function pushActivity(entry) {
  state.activity.push({ at: Date.now(), ...entry });
  // Reasoning is coalesced into a step's Think block instead; nothing else
  // streams fast enough to need throttle-tying the re-render.
  scheduleActivityRender();
}

let activityRenderTimer = null;
// Whether the panel should follow the newest content on the next render.
let panelFollows = true;

function scheduleActivityRender() {
  if (activityRenderTimer) return;
  activityRenderTimer = setTimeout(() => {
    activityRenderTimer = null;
    renderRightbar();
  }, 120);
}

function renderRightbar() {
  // Whether the reader is sitting at the newest end of the panel, decided before
  // the rebuild resets it. Following only when they are is the same courtesy the
  // transcript extends.
  const box = el.rightbarBody;
  panelFollows = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  // Plan, Thinking, Notes, Files — in the order they answer a question. The plan
  // is what the agent intends, thinking is what it is doing, notes are what it
  // has decided to keep, files are what it has done to the project. Plan leads
  // because it is the only one that says how far the work has got, and it no
  // longer appears and disappears: a primary tab that is absent until the agent
  // writes a plan is one nobody looks for.
  for (const [tab, id] of [
    ["todo", "tabTodo"],
    ["thinking", "tabThinking"],
    ["notes", "tabNotes"],
    ["files", "tabFiles"],
  ]) {
    document.getElementById(id).setAttribute("aria-selected", String(state.rightTab === tab));
  }

  el.rightbarBody.replaceChildren();
  if (state.rightTab === "thinking") {
    renderThinking();
    return;
  }
  if (state.rightTab === "notes") {
    renderNotes();
    return;
  }
  if (state.rightTab === "files") {
    renderFiles();
    return;
  }
  renderTodo();
}

/**
 * The project notebook: what the agent has chosen to remember.
 *
 * It is read back at the start of every later conversation, which makes it the
 * app's actual memory of the project — and it was completely invisible, so the
 * one durable thing a turn produces was the one thing the user could not check.
 * It is a panel rather than a file in the tree because it belongs to the project
 * rather than to the working copy, and because "what does it know about this"
 * is a question worth one click.
 */
function renderNotes() {
  if (!state.project) {
    el.rightbarBody.append(node("p", "empty", "No project open."));
    return;
  }
  const notes = state.notes;
  if (!notes) {
    el.rightbarBody.append(node("p", "empty", "Loading the notebook…"));
    loadNotes();
    return;
  }
  if (notes.error) {
    // A failed read is not an empty notebook. Treating one as the other is how a
    // notebook with 3,700 characters in it sat behind "Nothing recorded yet".
    el.rightbarBody.append(
      node("p", "empty", `Could not read the notebook: ${notes.error}`),
    );
    return;
  }
  if (!notes.text) {
    el.rightbarBody.append(
      node(
        "p",
        "empty",
        "Nothing recorded yet. The agent writes here with its remember tool — which " +
          "machine is down, where a file or command lives, decisions already made — and " +
          "reads it back at the start of every conversation.",
      ),
    );
    return;
  }

  const head = node("div", "notes__head");
  head.append(node("span", "notes__path", notes.path || "notebook"));
  head.append(node("span", "notes__count", `${notes.chars} characters`));
  el.rightbarBody.append(head);

  if (notes.elided) {
    // The model is given a capped version; the panel shows everything, so the
    // difference has to be said rather than silently implied.
    el.rightbarBody.append(
      node("p", "notice", "Long. The model is shown only the newest part of this."),
    );
  }

  const body = node("div", "notes__body");
  body.textContent = notes.text;
  el.rightbarBody.append(body);
}

let notesLoading = false;

async function loadNotes() {
  if (!state.project || notesLoading) return;
  notesLoading = true;
  try {
    state.notes = await api(`/api/projects/${state.project.id}/notes`);
  } catch (error) {
    // Kept as an error, not cached as an empty notebook: the panel would
    // otherwise keep saying it is empty long after the reason has gone.
    state.notes = { error: error.message || "the request failed" };
  } finally {
    notesLoading = false;
  }
  if (state.rightTab === "notes") renderRightbar();
}

/** The tools whose `path` argument names a file the turn touched, and how. */
const TOUCHED_TOOLS = {
  read_file: "read",
  write_file: "wrote",
  edit_file: "edited",
};

/**
 * Note a file this turn touched, for the Files panel.
 *
 * The panel was the project tree and nothing else, which is why it read as
 * decoration: it said what exists, which the person working in the project
 * already knows, and never what the agent had done with it. A path is recorded
 * once per turn and the later action wins, so a file that was read and then
 * rewritten shows as written.
 */
function noteTouched(name, args) {
  const mode = TOUCHED_TOOLS[name];
  const path = args && args.path;
  if (!mode || !path) return;
  state.touched.set(String(path), mode);
}

/**
 * What this turn has touched, ahead of the tree below it.
 *
 * "Wrote out/report.csv" is a fact about the conversation; a directory listing
 * is a fact about the disk. The panel is worth a tab because of the first.
 */
function renderTouchedFiles() {
  if (state.touched.size === 0) return;
  const section = node("div", "touched");
  section.append(node("div", "sectionTitle", "Touched this turn"));
  // Newest first: the file being worked on now is the one being looked for.
  for (const [path, mode] of [...state.touched.entries()].reverse()) {
    const row = node("button", "touched__row");
    row.type = "button";
    row.dataset.mode = mode;
    row.title = path;
    row.append(node("span", "touched__mode", mode));
    row.append(node("span", "touched__path", path));
    row.addEventListener("click", () => window.open(projectFileUrl(path), "_blank", "noopener"));
    section.append(row);
  }
  el.rightbarBody.append(section);
}

/**
 * Files: what this turn touched, with the project tree folded away behind it.
 *
 * The tree was the whole panel, and it was useless — a wall of dot-directories
 * says what exists, which the person working in the project already knows. What is
 * worth the click is what the agent did with it. The tree is still here, because
 * "what is in this project" is a fair question, but it is closed by default and
 * costs one line until someone asks.
 */
function renderFiles() {
  if (!state.project) {
    el.rightbarBody.append(node("p", "empty", "No project open."));
    return;
  }

  renderTouchedFiles();
  if (state.touched.size === 0) {
    el.rightbarBody.append(
      node(
        "p",
        "empty",
        "Nothing touched yet. The files the agent reads and writes this turn appear here.",
      ),
    );
  }

  const browse = node("details", "browse");
  browse.append(node("summary", "browse__summary", "Browse project files"));
  el.rightbarBody.append(browse);

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
  browse.append(header);

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
  browse.append(tree);
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

/**
 * The Thinking tab: the conversation's reasoning, in full, as it is written.
 *
 * Reasoning is the one thing the transcript keeps folded away. Its steps each have
 * a Think block, but reading a turn's reasoning from there means opening them one
 * at a time, and the panel that was supposed to save you that was a column of
 * "Step 3" cards holding a one-line gist — which told you a step had thought
 * without showing anything it had thought.
 *
 * Two things make it useful, and both come from the same complaint:
 *
 * * It **shows the text**, whole, and follows the newest as it grows — so while the
 *   agent works this is the live view of what it is considering.
 * * You can **go back through the history** and read any earlier step. The history
 *   is the whole conversation, not the current turn: the turn with fifty-eight steps
 *   of reasoning in it is almost never the last one, because the last one is the
 *   short turn that answers. Scoping this to the newest turn put all of that out of
 *   reach, which is the panel being useless with extra steps.
 *
 * The text is read from the transcript's steps rather than from `state.activity`,
 * because the transcript is where the whole of it is: the activity log keeps the
 * gist, and after a reload it has *only* the gist, which is how this panel came to
 * show less than the transcript did on every conversation but the running one.
 */
function renderThinking() {
  const thoughts = conversationReasoning();
  if (!thoughts.length) {
    el.rightbarBody.append(node("p", "empty", "It has not had to think yet."));
    renderDiagnostics();
    renderEnvironment();
    return;
  }

  // Newest by default and pinned only while the reader has not chosen otherwise.
  // `state.thinkingStep` is released when a turn starts, so the panel returns to the
  // live end on its own rather than staying on an old step forever.
  const newest = thoughts.length - 1;
  const pinned = Number.isInteger(state.thinkingStep) && state.thinkingStep < thoughts.length;
  const selected = pinned ? state.thinkingStep : newest;
  const current = thoughts[selected];
  const live = selected === newest && Boolean(state.currentTurn);

  const card = node("div", "thinkread");
  const head = node("div", "thinkread__head");
  head.append(node("span", "thinkread__title", `Thinking ${selected + 1} of ${thoughts.length}`));
  head.append(
    node("span", "thinkread__where", `turn ${current.turnNumber} · step ${current.stepIndex}`),
  );
  head.append(
    node("span", "thinkread__time", `~${formatDuration((current.text.length / 40) * 1000)}`),
  );
  const jump = node("button", "thinkread__jump", "In transcript");
  jump.type = "button";
  jump.addEventListener("click", () => focusStepRecord(current.step));
  head.append(jump);
  card.append(head);

  const body = node("div", "thinkread__text", current.text);
  body.dataset.state = live ? "live" : "done";
  card.append(body);

  // The picker goes under the text: the reasoning is what the tab is for, and it is
  // read from the top, so a row of numbers above it pushes the words down for the
  // sake of navigation. One line, scrolled sideways rather than wrapped — a long
  // turn has fifty steps, and fifty chips wrapped into a block is furniture.
  if (thoughts.length > 1) card.append(thinkingPicker(thoughts, selected, newest));

  el.rightbarBody.append(card);
  renderDiagnostics();
  renderEnvironment();

  // Follow the words, not the panel: the reasoning is what grows, and it grows past
  // the bottom of the box it is in.
  if (live) body.scrollTop = body.scrollHeight;
  if (panelFollows) el.rightbarBody.scrollTop = el.rightbarBody.scrollHeight;
}

/** The row of steps you can jump to, newest last. */
function thinkingPicker(thoughts, selected, newest) {
  const picker = node("div", "thinkpick");
  let selectedChip = null;
  thoughts.forEach((thought, index) => {
    const chip = node("button", "thinkpick__item", String(index + 1));
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(index === selected));
    // The gist, so a step is identifiable before it is opened.
    chip.title = `Turn ${thought.turnNumber}, step ${thought.stepIndex}: ${gistOf(thought.text)}`;
    chip.addEventListener("click", () => {
      // Choosing the newest means "follow it again", not "stay here": the step is
      // still being written and a pinned reader would freeze mid-sentence.
      state.thinkingStep = index === newest ? null : index;
      // `renderRightbar`, not `renderThinking`: the panel is rebuilt from empty by
      // the former, so calling the tab's own renderer appends a second reader under
      // the first — two cards, two rows of sixty-two chips, and the click appearing
      // to do nothing because the reader above it is the one you can see.
      renderRightbar();
    });
    if (index === selected) selectedChip = chip;
    picker.append(chip);
  });
  // The selected chip is almost always the last one, which is off the end of a
  // sideways-scrolled strip.
  if (selectedChip) {
    requestAnimationFrame(() => {
      selectedChip.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  }
  return picker;
}

/**
 * Every step of every turn that reasoned, oldest first.
 *
 * A flat list rather than a per-turn one, because the interesting reasoning is
 * usually in the long working turn rather than the short turn that answers, and a
 * per-turn panel can only ever show one of them.
 */
function conversationReasoning() {
  const open = view();
  if (!open) return [];
  const thoughts = [];
  for (const turn of open.turns.values()) {
    if (turn.kind !== "assistant") continue;
    const steps = [...turn.stepRows.values()].sort((a, b) => a.index - b.index);
    for (const step of steps) {
      if (!step.think || !step.think.text) continue;
      thoughts.push({
        text: step.think.text,
        step,
        stepIndex: step.index,
        turnNumber: turnNumberFor(turn),
      });
    }
  }
  return thoughts;
}

/** Which user turn a reply belongs to, counted the way the transcript reads. */
function turnNumberFor(turn) {
  const open = view();
  if (!open) return 1;
  let count = 0;
  for (const item of open.turns.values()) {
    if (item.kind === "user") count += 1;
    if (item === turn) return Math.max(count, 1);
  }
  return Math.max(count, 1);
}

/**
 * Everything that happened outside a step: voice engine notices, installs, the
 * environment. Kept below the reasoning, where a diagnostic belongs.
 */
function renderDiagnostics() {
  const loose = state.activity.filter((item) => !item.step);
  if (!loose.length) return;
  const section = node("div", "actcard actcard--notes");
  section.append(node("div", "actcard__head", "Diagnostics"));
  for (const item of [...loose].reverse()) {
    const row = node("div", "actrow");
    const main = node("div", "actrow__main");
    main.append(node("div", "actrow__title", item.label));
    if (item.detail) main.append(node("div", "actrow__detail", item.detail));
    row.append(main);
    section.append(row);
  }
  el.rightbarBody.append(section);
}

/** The step currently being worked on, or 0 when nothing is running. */
function currentStepIndex() {
  const turn = state.currentTurn;
  if (!turn || turn.kind !== "assistant" || turn.stepRows.size === 0) return 0;
  return Math.max(...turn.stepRows.keys());
}

/** Scroll the transcript to a step and flash it, linking panel to process. */
function focusStep(index) {
  const open = view();
  const turn =
    state.currentTurn || [...(open ? open.turns.values() : [])].reverse().find((t) => t.kind === "assistant");
  focusStepRecord(turn && turn.stepRows.get(Number(index)));
}

/**
 * The same, for a step the caller already holds.
 *
 * The Thinking tab can show a step from any turn in the conversation, so it cannot
 * look one up by number in "the current turn" — that is how a jump lands on the
 * wrong step, or on nothing at all, for everything except the newest turn.
 */
function focusStepRecord(step) {
  if (!step) return;
  step.head.setAttribute("aria-expanded", "true");
  step.body.hidden = false;
  step.root.scrollIntoView({ block: "center", behavior: "smooth" });
  step.root.dataset.flash = "true";
  setTimeout(() => {
    delete step.root.dataset.flash;
  }, 1200);
}

/**
 * The agent's plan, as it last wrote it.
 *
 * Kept visible rather than tucked into the transcript because its whole value is
 * answering "what is it trying to do, and how far has it got" at a glance. The
 * status is the point: an agent that stopped with three items still pending has
 * not finished, and nothing else in the UI says so.
 */
function renderTodo() {
  // What the conversation is for, above what is being done about it. Shown first
  // because it is the broader statement: every item below can be ticked while the
  // thing the user actually asked for is still not done.
  if (state.goal && state.goal.text) {
    const goal = node("div", "goal");
    goal.dataset.achieved = state.goal.achieved ? "true" : "false";
    goal.append(node("span", "goal__mark", state.goal.achieved ? "☑" : "◇"));
    goal.append(node("span", "goal__text", state.goal.text));
    if (state.goal.achieved) goal.append(node("span", "goal__note", "reached"));
    el.rightbarBody.append(goal);
  }
  if (state.todos.length === 0) {
    el.rightbarBody.append(node("p", "empty", "The agent has not written a plan."));
    return;
  }
  const done = state.todos.filter((item) => item.status === "completed").length;
  const header = node("div", "todohead");
  header.append(node("span", "todohead__title", "Plan"));
  header.append(node("span", "todohead__count", `${done}/${state.todos.length}`));
  el.rightbarBody.append(header);

  const list = node("div", "todolist");
  for (const item of state.todos) {
    const row = node("div", "todo");
    row.dataset.status = item.status || "pending";
    row.append(node("span", "todo__box", item.status === "completed" ? "☑" : "☐"));
    const main = node("div", "todo__main");
    main.append(node("div", "todo__text", item.content || ""));
    if (item.activeForm && item.activeForm !== item.content) {
      main.append(node("div", "todo__active", item.activeForm));
    }
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
  syncComposerControls();
}

/** True while a turn is actually running, as opposed to speaking after one. */
function isWorking() {
  const state_ = el.agentState.dataset.state;
  return state_ === "thinking" || state_ === "tool";
}

/**
 * Show Stop and Push only while there is something to stop or push through.
 *
 * Without them there was no way to halt a running turn at all — the protocol had a
 * cancel command and the client never sent it — and no way to say "this next thing
 * matters more than what you are doing", which is the other half of that.
 */
function syncComposerControls() {
  const working = isWorking();
  el.stopButton.hidden = !working;
  el.pushButton.hidden = !working;
  el.sendButton.title = working
    ? "Send — it will run after this turn finishes (Enter)"
    : "Send (Enter)";
}

/**
 * Say whether what you say can currently reach the agent.
 *
 * Echo suppression silently discards transcripts while the agent's own voice is
 * playing. When it fails to lift, everything the user says vanishes with the
 * microphone looking open and audio arriving — the one voice failure that leaves
 * no trace on screen. Showing the state is what makes it diagnosable while it is
 * happening rather than afterwards from the log.
 */
function setEchoSuppressed(suppressed) {
  state.echoSuppressed = Boolean(suppressed);
  el.micButton.dataset.suppressed = String(Boolean(suppressed));
  renderMicLabel();
}

/**
 * The microphone label, in one place.
 *
 * "Listening" is a claim about whether what you say reaches the agent, and it
 * was written by three different call sites — so a turn ending, or capture
 * starting, could overwrite the suppressed state with a plain "Listening" while
 * transcripts were still being discarded.
 */
function renderMicLabel() {
  if (!state.micOpen) {
    el.micLabel.textContent = "Mic off";
    return;
  }
  el.micLabel.textContent = state.echoSuppressed
    ? "Listening (agent speaking)"
    : "Listening";
}

function setConnection(name, label) {
  el.connection.dataset.state = name === ConnectionState.OPEN ? "idle" : "error";
  el.connectionLabel.textContent = label;
}

// ------------------------------------------------------------- event handling

/**
 * Route one conversation's event, whether or not it is on screen.
 *
 * A conversation that is not being looked at still has a socket, so its events
 * still arrive. Its transcript is not rendered — a refresh replays it from the
 * store — but its *status* is tracked, because "working, needs approval, has an
 * answer waiting" is exactly what the sidebar has to show for the user to work
 * in one conversation while another carries on.
 */
function handleSessionEvent(sessionId, event) {
  const data = event.data || {};
  const open = sessionId === state.session?.id;

  if (event.kind === "user_text" && data.source === "voice") {
    noteActivity(sessionId, { working: true });
  } else if (event.kind === "state") {
    const name = data.state;
    if (name === "thinking" || name === "tool") {
      noteActivity(sessionId, { working: true, awaitingApproval: false });
    } else if (name === "awaiting_approval") {
      noteActivity(sessionId, { working: true, awaitingApproval: true });
    } else if (name === "idle") {
      noteActivity(sessionId, { working: false });
    }
  } else if (event.kind === "done" || event.kind === "error") {
    noteActivity(sessionId, { working: false, awaitingApproval: false });
  } else if (event.kind === "approval_request") {
    noteActivity(sessionId, { working: true, awaitingApproval: true });
  }

  if (!open) {
    // Only the badges change; the transcript is replayed when it is opened.
    if (event.kind === "agent_text" || event.kind === "say") {
      noteActivity(sessionId, { unread: true });
    }
    return;
  }
  handleEvent(event);
}

/** Update what a conversation is doing, and refresh its sidebar row. */
function noteActivity(sessionId, patch) {
  const current = state.sessionActivity.get(sessionId) || {
    working: false,
    awaitingApproval: false,
    unread: false,
  };
  const next = { ...current, ...patch };
  if (patch.working === true) next.unread = false;
  if (
    current.working === next.working &&
    current.awaitingApproval === next.awaitingApproval &&
    current.unread === next.unread
  ) {
    return;
  }
  state.sessionActivity.set(sessionId, next);
  updateSessionRow(sessionId, next);
}

/**
 * Patch one sidebar row rather than re-rendering the list.
 *
 * `renderSessions` rebuilds every row and re-attaches every handler; doing that
 * for each event of a long turn was work with no visible benefit.
 */
function updateSessionRow(sessionId, activity) {
  const button = el.sessionList.querySelector(`[data-session="${sessionId}"]`);
  if (!button) return;
  button.dataset.working = String(Boolean(activity.working));
  button.dataset.approval = String(Boolean(activity.awaitingApproval));
  button.dataset.unread = String(Boolean(activity.unread));
  const badge = button.querySelector(".row__badge");
  if (!badge) return;
  badge.textContent = activity.awaitingApproval ? "needs you" : activity.unread ? "reply" : "";
  badge.hidden = !activity.awaitingApproval && !activity.unread;
}

function handleEvent(event) {
  // Nothing here may run for a conversation that is not on screen: every case
  // below writes into the open transcript. `handleSessionEvent` decides.
  if (!state.session) return;
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
      // Two numbers from the server: the working budget the meter measures
      // against, and the model's own window, which is context rather than a
      // target. Sent rather than hardcoded because both move with the model.
      if (data.context_window) state.contextWindow = Number(data.context_window) || 0;
      if (data.context_budget) state.contextBudget = Number(data.context_budget) || 0;
      renderContextMeter();
      if (data.voice_backends) {
        // Which engine each direction uses belongs in the Activity panel: it is
        // the first thing a voice bug report needs, and it is never obvious from
        // the UI otherwise.
        pushActivity({
          label: "Voice engines",
          detail: `speech in: ${data.voice_backends.stt}; speech out: ${data.voice_backends.tts}`,
        });
      }
      if (data.voice_problem) {
        // A configured engine that did not start must say so, with the fix. The
        // alternative is a microphone button that looks fine and records nothing.
        pushActivity({
          label: "Voice unavailable",
          detail: data.voice_fix ? `${data.voice_problem} — ${data.voice_fix}` : data.voice_problem,
        });
        toast(data.voice_problem, "error");
      }
      // The server synthesises at this rate. Adopting it is what keeps a
      // configured rate from being decoded as if it were the default.
      if (data.sample_rate && !playback.setServerRate(data.sample_rate)) {
        pushActivity({
          label: "Speech rate mismatch",
          detail:
            `synthesised at ${data.sample_rate} Hz but the audio output runs at ` +
            `${playback.status.negotiatedRate} Hz. Speech will sound too fast or too slow; ` +
            "reload the page to rebuild the audio output.",
        });
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
      if (typeof data.echo_suppressed === "boolean") setEchoSuppressed(data.echo_suppressed);
      // A conversation whose stored transcript ends mid-process only *looks*
      // stopped: the answer is written when the turn finishes. If the live
      // session says it is still working, it has not stopped, and offering to
      // continue would start a second turn on top of the one already running.
      if (state.pendingStopNote) {
        const live = ["thinking", "tool", "awaiting_approval", "speaking"].includes(data.state);
        if (live) {
          state.pendingStopNote = null;
        } else {
          showStopNote(state.pendingStopNote, null);
          state.pendingStopNote = null;
        }
      }
      renderRightbar();
      break;
    }
    case "state": {
      if (data.state) {
        // An open approval outranks a voice transition. The agent narrates before
        // it calls a tool, so a prompt can arrive while the reply is still being
        // spoken; the `speaking -> idle` that follows used to clear it about half
        // a second later, leaving the user unable to answer a question the server
        // was still waiting on. Only a state that means the turn moved past the
        // decision may replace it.
        const voiceTransition = ["speaking", "listening", "idle"].includes(data.state);
        if (!(state.pendingApproval && voiceTransition)) setAgentState(data.state);
        // A cancellation reports `idle` without ever sending `done`, so the release
        // has to happen here too — but only for a real cancellation, never for an
        // ordinary state change.
        if (data.reason === "cancelled" || data.reason === "stopped") {
          clearApproval();
          if (state.currentTurn) finishTimers(state.currentTurn);
        }
        if (typeof data.echo_suppressed === "boolean") setEchoSuppressed(data.echo_suppressed);
        // A turn that is working needs its clock running from the moment it says
        // so, not from the first thing it happens to think or run. "Thinking" with
        // no counter is the state that reads as a hang.
        if (data.state === "thinking" || data.state === "tool") startTimers();
      }
      // Deepgram refused the speed, so the server asked us to apply it here.
      if (data.speech_speed && data.kind_detail === "speed_fallback") {
        playback.setPlaybackRate(data.speech_speed);
        pushActivity({
          label: "Speech speed",
          detail: `${data.speech_speed}× applied during playback (the voice rejected it)`,
        });
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
      // A message typed while a turn was running arrives here too. It is shown —
      // it is the user's message and hiding it reads as lost — but nothing is
      // reset, because the turn it is waiting behind is still streaming into its
      // own block above.
      const queued = Boolean(data.queued);
      if (!queued) {
        // A new turn supersedes anything outstanding: if an approval was waiting,
        // the turn that asked for it is gone.
        clearApproval();
      }
      const turn = beginTurn("user");
      turn.bubble.textContent = data.text || "";
      // Kept so a stopped turn can be resumed: the server continues from its own
      // stored conversation, and this is only what the composer re-offers.
      state.lastUserText = data.text || "";
      if (queued) {
        turn.bubble.dataset.queued = "true";
        toast("Queued — it will run as soon as this request finishes.");
        break;
      }
      state.currentTurn = null;
      state.pendingStopNote = null;
      // A new turn touches its own files; the last turn's list is history, and
      // the Thinking tab is where history lives.
      state.touched.clear();
      renderRightbar();
      el.captions.replaceChildren();
      hideStopNote();
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
      const step = stepFor(turn, data.step);
      const record = addToolRow(step, data.call_id, data.name || "tool");
      noteStepTool(step, data.name || "tool");
      startTimers();
      record.arguments = data.arguments || {};
      clearApprovalFor(data.call_id);
      noteTouched(data.name, data.arguments);
      pushActivity({
        kind: "tool",
        step: step ? step.index : 0,
        label: data.name || "tool",
        detail: formatArgs(data.arguments),
      });
      break;
    }
    case "tool_result": {
      // Also covers a call that was answered without the user: an auto-approved
      // retry, a remembered tool, or one the user just declined.
      clearApprovalFor(data.call_id);
      const record = view()?.toolRows.get(data.call_id);
      if (record) settleToolRow(record, data);
      // A notebook write is the turn learning something durable. The note is the
      // only part of it worth showing — the tool's own display is a character
      // count — and it is already on the wire as the call's argument.
      if (data.name === "remember" && data.ok) {
        appendLearned(
          assistantTurn(),
          (record && record.arguments && record.arguments.note) || data.display,
        );
        // The notebook just changed under the panel that shows it.
        state.notes = null;
        if (state.rightTab === "notes") loadNotes();
      }
      const step = stepFor(assistantTurn(), data.step);
      if (step) {
        step.end = Date.now();
        step.root.dataset.state = data.ok ? "ok" : "error";
        step.summary.textContent = planSummary(step);
      }
      pushActivity({
        kind: "result",
        step: step ? step.index : 0,
        label: `${data.name} ${data.ok ? "succeeded" : "failed"}`,
        detail: data.display || data.error || "",
        duration_ms: data.duration_ms,
      });
      break;
    }
    case "artifact": {
      const turn = assistantTurn();
      const link = node("a", "artifact", `Open ${data.path}`);
      link.href = projectFileUrl(data.path);
      link.target = "_blank";
      link.rel = "noopener";
      turn.root.append(link);
      // A file the agent produced is the most interesting thing the Files panel
      // can show, and this is the event that names it.
      state.touched.set(String(data.path), "created");
      pushActivity({
        kind: "artifact",
        step: currentStepIndex(),
        label: "Created file",
        detail: data.path,
      });
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
      const step = stepFor(turn, data.step);
      const record = addToolRow(step, data.call_id, data.name || "tool");
      noteStepTool(step, data.name || "tool");
      record.row.dataset.state = "awaiting";
      record.summary.textContent = "awaiting approval";
      setAgentState("awaiting_approval");
      break;
    }
    case "usage": {
      // The model badge keeps the model; the numbers belong to the meter, which
      // has the room to say what they mean. `prompt_tokens` is the size of the
      // conversation the model was just sent, and that is what the window bounds.
      state.usage = data;
      renderContextMeter();
      break;
    }
    case "thinking": {
      // Reasoning sits in the step that produced it, in the transcript, so the
      // process reads in order: what was considered, then what was run. It is
      // never mixed into the answer — the step's Think block is collapsed unless
      // someone opens it.
      const turn = assistantTurn();
      addThinking(turn, data.text || "", data.step);
      startTimers();
      const step = stepFor(turn, data.step);
      if (step) {
        // After the first delta the row already exists, so every later one
        // updates it in place — and an in-place update has to schedule its own
        // repaint. Only `pushActivity` did that, so the panel showed the step's
        // first line and then sat still for the rest of the step, which reads as
        // a stalled agent. Reported as "the activity does not seem to update".
        const existing = state.activity.find((item) => item.kind === "think" && item.step === step.index);
        const live = latestLineOf(step.think ? step.think.text : "");
        if (existing) {
          existing.detail = live;
          // The full text too: the Thinking panel shows the running step's
          // reasoning in full, and a one-line summary is not something to watch.
          existing.full = step.think ? step.think.text : live;
          scheduleActivityRender();
        } else {
          pushActivity({
            kind: "think",
            step: step.index,
            label: "Think",
            detail: live,
            full: step.think ? step.think.text : live,
          });
        }
      }
      break;
    }
    case "voice": {
      // The voice layer's health, not its output. A problem that has ended is taken
      // off the screen; the activity panel keeps the record either way.
      if (!data.problem) {
        clearVoiceProblems();
        pushActivity({ label: "Speech recognition recovered", detail: "" });
        setAgentState(state.micOpen ? "listening" : "idle");
      }
      break;
    }
    case "goal": {
      // Same contract as the plan below: the store is the source of truth, so this
      // replaces rather than merges and cannot drift from the record.
      state.goal = data.goal ? { text: data.goal, achieved: !!data.achieved } : null;
      renderRightbar();
      break;
    }
    case "todos": {
      // The agent rewrote its plan. Replacing wholesale rather than merging keeps
      // the panel a faithful picture of what the agent believes, including items
      // it decided to drop.
      state.todos = Array.isArray(data.todos) ? data.todos : [];
      // Writing a plan no longer steals the panel. The plan has its own tab,
      // which is already in front of the user the first time, and switching
      // under them would take away whatever they were reading.
      renderRightbar();
      break;
    }
    case "error": {
      // An errored turn cannot still be waiting for approval, and a stuck prompt
      // is worse than no prompt: it blocks the composer indefinitely.
      settlePendingApproval("not run — the turn failed");
      clearApproval();
      const turn = state.currentTurn || beginTurn("assistant");
      appendError(turn, data.message || "Something went wrong.", data.kind_detail);
      setAgentState("error");
      if (data.recoverable) toast(data.message, "error");
      break;
    }
    case "done": {
      settlePendingApproval("not run");
      clearApproval();
      if (!data.failed && !data.truncated) setAgentState(state.micOpen ? "listening" : "idle");
      // A turn that produced no rendered output has no current turn at all: the
      // turn object is created by the first event that renders something. So make
      // one rather than concluding there is nothing to recover.
      const finished = state.currentTurn || beginTurn("assistant");
      finishTimers(finished);
      // Why it ended, when that was not simply "it finished". A turn that stops
      // because it ran out of steps is not a failure and not an answer, and
      // leaving the indicator to slide back to "Idle" made the two look alike.
      showStopNote(data, finished);
      state.currentTurn = null;
      // A completed turn must show something. If nothing was rendered, the events
      // never arrived: a dropped or half-dead socket loses them silently, and the
      // server carries on working and stores the answer. The user was then left
      // with an empty turn, no error, and no way to know an answer existed, so
      // they asked again. The answer is in the store, so fetch it and show it.
      if (!data.failed && !turnHasVisibleText(finished)) {
        recoverMissingAnswer(finished).then((found) => {
          // Nothing was recovered either, so the transcript would simply stop —
          // which reads as "still working" to anyone who came back to it.
          if (!found) appendNoAnswer(finished, data.detail);
        });
      } else if (!turnHasVisibleText(finished)) {
        // A turn that failed or ran out of steps has no answer to recover. The
        // banner above the composer says why; this is the same ending written
        // where the conversation actually is.
        appendNoAnswer(finished, data.reason === "failed" ? "" : data.detail);
      }
      break;
    }
    default:
      break;
  }
  if (event.seq) scrollToBottom();
}

/** True when a turn ended up showing the user anything at all. */
function turnHasVisibleText(turn) {
  return Boolean((turn.saidText && turn.saidText.trim()) || (turn.shownText && turn.shownText.trim()));
}

/**
 * Freeze a turn's clocks and clear the "still working" line.
 *
 * Called on the turn boundary rather than by each row: a row that stops its own
 * timer keeps ticking if its completion event is the one that got lost, and a
 * timer that runs forever after the turn ended is its own kind of lie.
 */
function finishTimers(turn) {
  const now = Date.now();
  if (turn && turn.kind === "assistant") {
    turn.endedAt = now;
    for (const step of turn.stepRows.values()) {
      if (!step.end) step.end = now;
      step.root.dataset.state = step.root.dataset.state === "running" ? "ok" : step.root.dataset.state;
    }
  }
  for (const record of (view()?.toolRows ?? new Map()).values()) {
    if (record.startedAt && !record.endedAt && record.row) {
      // A call whose result never arrived is not silently "ok": it is unknown,
      // and saying so is better than a green dot that means nothing.
      if (record.row.dataset.state === "running") {
        record.row.dataset.state = "stopped";
        record.summary.textContent = "no result reported";
      }
      record.endedAt = now;
    }
  }
  if (workingLine && workingLine.parentNode) workingLine.remove();
  workingLine = null;
  updateTimers();
  stopTimers();
}

/**
 * Say why a turn ended, when it did not simply finish.
 *
 * The agent speaks this itself (see `Session._speak_problem`), so the same
 * sentence appears here as text. The reason and the wording are kept aligned by
 * the server sending `reason`/`detail` on the done event; a user who heard the
 * voice should find the same explanation on screen.
 *
 * Only the endings that need explaining get a banner. This used to fall through
 * to a default of `{ title: "Stopped", detail: "" }`, which meant every ordinary
 * completion — `reason: "complete"`, the common case by far — put a bare
 * "Stopped" on screen with nothing under it. A user reading that reasonably
 * concludes the turn was abandoned, however well it actually went.
 */
function showStopNote(data, turn) {
  const reason = data.reason || (data.failed ? "failed" : data.truncated ? "step_limit" : "");
  const copy = {
    step_limit: {
      title: "Stopped — the step limit was reached",
      detail:
        data.detail ||
        "It worked through as many steps as it is allowed in one turn and stopped part-way. " +
          "The work so far is above. Continue to let it carry on, or ask for a smaller piece.",
    },
    no_answer: {
      title: "Stopped — it finished without answering",
      detail:
        data.detail ||
        "The turn did its work but produced no reply. The commands it ran are recorded above. " +
          "Continue to ask for the answer.",
    },
    failed: {
      title: "Stopped — something went wrong",
      detail: data.detail || "That turn ended with an error. The detail is in the transcript above.",
    },
    interrupted: {
      title: "Stopped — this turn was interrupted",
      detail:
        data.detail ||
        "The conversation ends part-way through, with no answer after the last step. " +
          "Continue to let the agent carry on from here.",
    },
  }[reason];
  if (!copy) {
    // `complete` is the ordinary finish and needs no explanation; `cancelled`
    // and `stopped` are the user's own doing, and telling them why is noise.
    hideStopNote();
    return;
  }

  el.stopTitle.textContent = copy.title;
  el.stopDetail.textContent = copy.detail;
  el.stopNote.hidden = false;
  el.stopNote.dataset.reason = reason;
  // Continue is only offered when continuing can actually work: a step-limited
  // turn resumes from its stored conversation, an internal failure may not.
  // Continue is only offered for the two endings that resuming actually fixes: a turn
  // that ran out of steps, and one that did the work but never answered. An
  // internal failure may not repeat the same way, so it gets no button.
  const resumable =
    reason === "step_limit" || reason === "no_answer" || reason === "interrupted";
  el.stopContinue.hidden = !(resumable && Boolean(state.lastUserText));
  scrollToBottom();
}

function hideStopNote() {
  el.stopNote.hidden = true;
}

/**
 * Ask the agent to carry on with the turn that stopped.
 *
 * The server keeps the conversation, so the nudge is deliberately short — the
 * work in flight is already in its history. Saying "continue" as a fresh turn
 * also means it starts with a step budget of its own, which is the whole point:
 * the previous turn's limit is what stopped it.
 */
function continueLastTurn() {
  if (!state.session) return;
  const nudge = "Continue from where you stopped.";
  hideStopNote();
  const turn = beginTurn("user");
  turn.bubble.textContent = "Continue";
  state.currentTurn = null;
  state.lastUserText = nudge;
  activeConnection().sendCommand("text", { text: nudge });
  setAgentState("thinking");
  startTimers();
}

/**
 * Show the answer for a turn whose events never arrived.
 *
 * The transcript on the server is the source of truth, so re-read it rather than
 * asking the model again: the work is already done and only the delivery failed.
 * Reported in the Activity panel, because a reply that appears a moment late for
 * no visible reason is as confusing as one that never appears.
 */
async function recoverMissingAnswer(turn) {
  if (!state.session) return false;
  try {
    const session = await api(`/api/sessions/${state.session.id}`);
    const messages = session.messages || [];
    const last = [...messages]
      .reverse()
      .find((message) => message.role === "assistant" && (message.content || message.spoken));
    if (!last) return false;
    // Only recover an answer to the question that was just asked. Showing a stale
    // reply from an earlier turn would be worse than showing nothing, because it
    // reads as a real answer.
    const lastUser = [...messages].reverse().find((message) => message.role === "user");
    if (lastUser && last.id < lastUser.id) return false;

    if (last.spoken && last.spoken.trim()) appendSaid(turn, last.spoken);
    if (last.content && last.content.trim() !== (last.spoken || "").trim()) {
      appendStoredAnswer(turn, last.content, last.spoken);
    }
    pushActivity({
      label: "Recovered a missing reply",
      detail: "The answer was produced and stored, but its events did not reach this page.",
    });
    renderRightbar();
    scrollToBottom(true);
    toast("Recovered the reply that did not come through.", "error");
    return true;
  } catch {
    // A failed recovery must not add noise; the caller reports the ending.
    return false;
  }
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

/**
 * Release the prompt once the call it belongs to actually proceeds.
 *
 * The decision is not always a click: a tool that was trusted, one whose
 * approval arrived from elsewhere, or a declined call all end in a tool event.
 * Matching on the call id keeps an unrelated tool's traffic from dismissing a
 * question that is still open.
 */
function clearApprovalFor(callId) {
  if (state.pendingApproval && state.pendingApproval.call_id === callId) clearApproval();
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
  // Resolved per frame: the microphone follows the conversation on screen, and
  // there is no longer a single socket to hold. This used to call sendAudio on
  // the singleton connection that this file stopped defining when each
  // conversation got its own socket. A bare `connection` still resolves in a
  // browser — to the element with `id="connection"` — so every captured frame
  // threw `sendAudio is not a function` and was dropped. Capture looked
  // perfectly healthy: the level meter moved, the device was named, and the
  // server received nothing at all.
  onAudio: (frame) => sendAudioFrame(frame),
  onBackendChange: (backend) => {
    // A backend switch is a degraded mode, not a normal event: the user should
    // know their audio is being captured by the fallback path.
    if (backend === "script-processor") {
      pushActivity({
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
      activeConnection().sendCommand("barge_in");
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
    pushActivity({ label: "Speaking", detail: playback.statusLine });
    if (!status.rateMatches) {
      pushActivity({
        label: "Speech rate mismatch",
        detail: `synthesised at ${status.requestedRate} Hz, output at ${status.negotiatedRate} Hz`,
      });
    }
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

/**
 * One socket per open conversation, not one socket for the app.
 *
 * A single socket was closed and reopened on every switch, so moving to another
 * conversation stopped the one you left: it kept working on the server, but
 * nothing was listening for its events, so its progress and its answer had no
 * way back. Keeping a socket per conversation is what lets one chat carry on
 * while you work in another — which the server has always supported; only the
 * browser was holding it back.
 *
 * A conversation's socket is closed when the conversation goes away (archived or
 * deleted), not when you look away from it.
 */
const connections = new Map();

/**
 * Send one captured frame to the conversation on screen.
 *
 * Capture is a single global stream that follows the conversation, so the socket
 * is looked up per frame rather than captured once.
 */
function sendAudioFrame(frame) {
  const session = state.session;
  if (!session) return;
  const socket = connections.get(session.id);
  if (socket) socket.sendAudio(frame);
}

function connectionFor(sessionId) {
  const existing = connections.get(sessionId);
  if (existing) return existing;
  const socket = new Connection({
    onEvent: (event) => handleSessionEvent(sessionId, event),
    onAudio: (pcm) => {
      // Only the conversation on screen may speak: audio is played, not shown,
      // and a background reply talking over the one being read would be worse
      // than silent.
      if (sessionId === state.session?.id) playback.push(pcm);
    },
    onState: (connectionState) => {
      if (sessionId !== state.session?.id) return;
      if (connectionState === ConnectionState.OPEN) setConnection(connectionState, "Connected");
      else if (connectionState === ConnectionState.CONNECTING)
        setConnection(connectionState, "Connecting");
      else setConnection(connectionState, "Disconnected");
    },
    onError: (message) => {
      if (sessionId === state.session?.id) toast(message, "error");
    },
  });
  connections.set(sessionId, socket);
  return socket;
}

/** The socket for the open conversation. Throws if there is none. */
function activeConnection() {
  if (!state.session) throw new Error("no conversation open");
  return connectionFor(state.session.id);
}

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
      renderMicLabel();
      activeConnection().sendCommand("mic", { open: false });
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
    renderMicLabel();
    activeConnection().sendCommand("mic", { open: true });
    setAgentState("listening");
    el.captions.replaceChildren();
    el.captions.append(node("span", "captions__hint", "Listening…"));

    // Record what capture actually negotiated. This is the single most useful
    // diagnostic for a silent microphone, and it is readable in the UI.
    const started = capture.status;
    console.info("[surtitle] microphone opened", started);
    pushActivity({
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
          pushActivity({ label: "Audio inputs available", detail: names });
          renderRightbar();
        }
      } else if (status.maxLevel < 0.01) {
        problem = `The microphone is open${device} but the signal is silent. Raise the input level.`;
      }
      console.info("[surtitle] capture check", status);
      pushActivity({
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
  leaveSession();
  state.project = project;
  state.session = null;
  state.archive = [];
  state.showArchive = false;
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

async function renameProject(project) {
  const name = await promptAction({
    title: "Rename project",
    label: "Project name",
    value: project.name,
    confirmLabel: "Rename",
    note: "Only the name in Surtitle changes. The folder on disk keeps its name.",
  });
  if (!name || name === project.name) return;
  try {
    const updated = await api(`/api/projects/${project.id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    });
    if (state.project?.id === project.id) {
      state.project = { ...state.project, name: updated.name };
      renderHeader();
    }
    await loadProjects();
    toast("Project renamed.");
  } catch (cause) {
    toast(cause.message, "error");
  }
}

/**
 * Remove a project and every conversation in it, keeping the folder.
 *
 * "Delete the project" is the one action here that could reasonably be read as
 * "delete my work", so the dialog names the folder and says it is untouched
 * before anything happens, and the confirmation afterwards repeats it. Nothing
 * on disk is ever removed: this forgets the project in Surtitle's own database.
 */
async function deleteProject(project) {
  let total = 0;
  try {
    const full = await api(`/api/projects/${project.id}`);
    const counts = full.session_counts || {};
    total = (counts.active || 0) + (counts.archived || 0);
  } catch {
    // The count is a courtesy; the confirmation does not depend on it.
  }
  const conversations =
    total === 1 ? "its one conversation" : `its ${total} conversations`;
  const ok = await confirmAction({
    title: `Delete "${project.name}"?`,
    text: total
      ? `The project and ${conversations} will be removed from Surtitle permanently.`
      : "The project will be removed from Surtitle permanently.",
    note: `Your files are not touched: ${project.root} and everything in it stays exactly as it is.`,
    confirmLabel: "Delete project",
  });
  if (!ok) return;
  const wasOpen = state.project?.id === project.id;
  try {
    await api(`/api/projects/${project.id}`, { method: "DELETE" });
    await loadProjects();
    toast(`Project deleted. ${project.root} was left as it is.`);
    if (wasOpen) await forgetOpenProject();
  } catch (cause) {
    toast(cause.message, "error");
  }
}

/** Clear the view after the open project went away, then open another. */
async function forgetOpenProject() {
  leaveSession();
  state.project = null;
  state.session = null;
  state.sessions = [];
  state.archive = [];
  state.showArchive = false;
  state.currentTurn = null;
  state.activity = [];
  state.files = { path: ".", entries: [] };
  el.turns.replaceChildren();
  renderProjects();
  renderSessions();
  renderHeader();
  renderRightbar();
  if (state.projects.length) {
    await selectProject(state.projects[0].id);
  } else {
    openProjectModal();
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
  leaveSession();
  state.session = null;
  el.turns.replaceChildren();
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
    closeConnection(session.id);
    state.sessionActivity.delete(session.id);
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
    closeConnection(session.id);
    state.sessionActivity.delete(session.id);
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
  // The conversation may have changed while that request was in flight — a click
  // is enough. Rendering the reply into whatever is open now would put one
  // conversation's transcript in another's window.
  if (state.session?.id === sessionId) {
    // Re-selecting the same conversation: keep its rows and just reconnect.
    state.session = session;
    openConnection();
    return;
  }
  leaveSession();
  viewFor(sessionId);
  state.session = session;
  el.turns.replaceChildren();
  resetView(sessionId);
  state.currentTurn = null;
  state.activity = [];
  state.todos = Array.isArray(session.todos) ? session.todos : [];
  state.goal = session.goal ? { text: session.goal, achieved: !!session.goal_achieved } : null;
  // The panel this conversation was last left on, if it has been open before.
  state.rightTab = state.sessionTabs.get(sessionId) || "todo";
  state.touched.clear();
  // The notebook belongs to the project rather than the conversation, but the
  // project can change, so it is re-read rather than trusted.
  state.notes = null;
  // The window is a fact about this conversation; another one's usage is not a
  // smaller version of it, it is a different number entirely.
  state.usage = null;
  renderContextMeter();
  state.lastUserText = "";
  state.pendingStopNote = null;
  hideStopNote();

  // Replay the stored transcript so reopening a conversation shows its history,
  // then land on the newest message rather than the oldest.
  //
  // Assistant turns are rebuilt as steps, not as one flat block: the server
  // stores each step's thinking as a `reasoning` message and each call's step
  // number, so a reopened conversation shows the same reasoning-then-commands
  // process the live view showed. Without this a reopened turn was a bare answer
  // with no sign of the fourteen commands behind it.
  await replayThenJumpToLatest(() => {
    const calls = session.tool_calls || [];
    // One chronological stream: the store keeps thinking as messages and calls
    // as their own rows, and their order relative to each other is what the
    // process view is. `id` is unique across both tables, so the sort is stable
    // where timestamps land in the same millisecond.
    const stream = [
      ...(session.messages || []).map((message) => ({ at: message.created_at, message })),
      ...calls.map((call) => ({ at: call.created_at, call })),
    ].sort((a, b) => (a.at || 0) - (b.at || 0));

    // Thinking is keyed by the step it belongs to, and calls place it. The stored
    // reasoning row carries no step number of its own, so the step is taken from
    // the calls that follow it — which is exactly how it was written: a step's
    // thinking is stored when the step ends, immediately before its calls.
    //
    // Holding it back until the answer instead put a step's Think block in the
    // *next* turn, and dropped it entirely when the turn had no answer of its own.
    // That is the shape a stopped conversation has, so the one case where the
    // reasoning matters most was the one that lost it.
    const thinking = new Map();
    const tookStep = new Set();
    let looseThinking = [];
    // Where the conversation was last spoken to, and where it was last answered.
    // A stopped turn is one that was never answered — not merely one that did
    // work, which is every turn.
    let lastAskedAt = -1;
    let lastAnsweredAt = -1;

    const flushThinking = (turn, step) => {
      const index = Number(step) || 1;
      if (!thinking.has(index)) return;
      addThinking(turn, thinking.get(index), index);
      thinking.delete(index);
    };

    const attachLoose = (turn) => {
      for (const text of looseThinking) addThinking(turn, text);
      looseThinking = [];
    };

    for (const item of stream) {
      if (item.message) {
        const { message } = item;
        if (message.role === "user") {
          const turn = beginTurn("user");
          turn.bubble.textContent = message.content;
          state.lastUserText = message.content || state.lastUserText;
          lastAskedAt = item.at || 0;
        } else if (message.role === "reasoning") {
          looseThinking.push(message.content || "");
        } else if (message.role === "system") {
          // Attachment records and similar notices: context, not something to answer.
          const turn = beginTurn("assistant");
          appendShown(turn, message.content);
        } else {
          const turn = beginTurn("assistant");
          // Any thinking still unclaimed belongs above this answer.
          for (const [index, text] of thinking) {
            addThinking(turn, text, index);
          }
          thinking.clear();
          attachLoose(turn);
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
            // The stored content is the display text with the turn's work log
            // appended to it: `appendStoredAnswer` splits them, renders the first as
            // Markdown and folds the second away.
            appendStoredAnswer(turn, message.content, message.spoken);
          }
          lastAnsweredAt = item.at || 0;
        }
      } else {
        const call = item.call;
        const turn = assistantTurn();
        if (looseThinking.length) {
          // Thinking that precedes the first call of a step is that step's.
          thinking.set(Number(call.step) || 1, looseThinking.join(""));
          looseThinking = [];
        }
        tookStep.add(Number(call.step) || 1);
        replayToolCall(turn, call);
        flushThinking(turn, call.step);
      }
    }

    // Work with no answer after it: the stored conversation ends mid-process.
    // Show the work rather than dropping what the user watched happen, but do not
    // announce a stop yet — a turn that is still running looks exactly like this
    // from the store, because its answer is not written until it finishes. The
    // live conversation is asked first (`ready`), and only a quiet one is told it
    // stopped. Showing it here made a refresh during a long turn claim the agent
    // had stopped and offer to continue work that was still in progress.
    if (thinking.size || looseThinking.length || tookStep.size) {
      const turn = beginTurn("assistant");
      for (const [index, text] of thinking) addThinking(turn, text, index);
      attachLoose(turn);
    }

    // A stopped turn is one the agent never answered. That is not the same as a
    // turn that did work: every turn does work. This used to fire whenever the
    // conversation contained any thinking or tool call at all, so reopening a
    // conversation that had finished normally announced "Stopped — the step limit
    // was reached" over a complete answer — and named a cause the browser has no
    // way to know. It may have been a restart, a cancellation, or a crash.
    if (lastAskedAt > lastAnsweredAt) {
      // The server records how the last turn ended, so a reopened conversation
      // states the real reason rather than one the browser inferred. "No answer
      // after the work" is not the same as "why": a spent step budget, an empty
      // model round, a failure and a restart all looked identical from here, and
      // this used to name the wrong one. The guess is kept only for
      // conversations whose last turn ended before the record existed.
      state.pendingStopNote = {
        reason: session.last_end_reason || "interrupted",
        detail: session.last_end_detail || STOPPED_WITHOUT_ANSWER,
      };
      // And written into the transcript, not only into the banner: a reopened
      // conversation is read from the top, and one whose last turn produced no
      // answer must not simply stop at a Think block.
      const replayed = view() ? [...view().turns.values()] : [];
      const lastTurn = replayed.reverse().find((item) => item.kind === "assistant");
      appendNoAnswer(lastTurn || beginTurn("assistant"), session.last_end_detail || "");
    }
    state.currentTurn = null;
  });

  renderSessions();
  renderRightbar();
  openConnection();
}

/** The stored line shown when a conversation ends mid-process. */
const STOPPED_WITHOUT_ANSWER =
  "This conversation was interrupted before the agent answered — the process may have been " +
  "restarted, or the turn cancelled. Continue to let the agent carry on from here.";

/**
 * Rebuild one stored tool call as a settled row in its step.
 *
 * The store keeps the outcome, the duration and the step, so a reopened row is
 * the same row the live turn produced rather than a placeholder: a command that
 * failed is still red, and one that took thirty seconds still says so. No timer
 * is started — the call is over.
 */
function replayToolCall(turn, call) {
  const step = stepFor(turn, call.step);
  const record = addToolRow(step, `stored_${call.id}`, call.name || "tool");
  noteStepTool(step, call.name || "tool");
  record.arguments = call.arguments || {};
  const ok = call.ok !== false;
  settleToolRow(record, {
    ok,
    display: call.result || (ok ? "done" : ""),
    error: ok ? "" : call.result || "failed",
    duration_ms: call.duration_ms,
  });
  if (call.approved === false) {
    record.row.dataset.state = "stopped";
    record.summary.textContent = "declined";
  }
  // What a previous conversation learned belongs in the reopened transcript too,
  // or the notebook looks like it filled itself.
  if (call.name === "remember" && ok) {
    appendLearned(turn, (call.arguments && call.arguments.note) || call.result || "");
  }
  step.end = step.end || (call.created_at ? call.created_at * 1000 : Date.now());
  step.root.dataset.state = ok ? "ok" : "error";
  step.summary.textContent = planSummary(step);
  pushActivity({
    kind: "result",
    step: step.index,
    label: call.name || "tool",
    detail: formatArgs(call.arguments),
    duration_ms: call.duration_ms,
    ok,
    at: call.created_at ? call.created_at * 1000 : Date.now(),
  });
}

/**
 * Make sure the open conversation has a socket, and keep the others open.
 *
 * Reconnecting is deliberate rather than reusing a live socket: the server
 * rebinds an existing session to the new connection and restates its state, so
 * anything that happened while this conversation was in the background — where
 * its events were not being rendered — is reconciled from the store.
 */
function openConnection() {
  if (!state.project || !state.session) return;
  const socket = connectionFor(state.session.id);
  socket.close();
  socket.connect({ project_id: state.project.id, session_id: state.session.id });
}

/**
 * Stop the microphone belonging to the conversation being left.
 *
 * There is one microphone, and the policy is that it follows the conversation on
 * screen — a chat you have navigated away from must not keep listening to the
 * room, and its transcripts would land in a conversation nobody is reading. The
 * conversation itself keeps working; only its ears close.
 */
function leaveSession() {
  if (!state.session) return;
  if (state.micOpen) {
    try {
      activeConnection().sendCommand("mic", { open: false });
    } catch {
      // No socket to tell; the server closes the stream with the session anyway.
    }
  }
  capture.setMuted(true);
  state.micOpen = false;
  el.micButton.dataset.active = "false";
  el.micButton.setAttribute("aria-pressed", "false");
  renderMicLabel();
}

/** Drop a conversation's socket. Its conversation is gone, or being replaced. */
function closeConnection(sessionId) {
  const socket = connections.get(sessionId);
  if (!socket) return;
  socket.close();
  connections.delete(sessionId);
}

async function sendMessage(options = {}) {
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

  activeConnection().sendCommand("text", {
    text: message,
    // Push, from the button beside send: stop the running turn and take its place.
    interrupt: Boolean(options && options.interrupt),
  });
  el.composer.value = "";
  el.composer.style.height = "auto";
  clearAttachments();
  setAgentState("thinking");
}

function answerApproval(allowed, remember) {
  const pending = state.pendingApproval;
  clearApproval();
  if (!pending) return;
  activeConnection().sendCommand("approval", {
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

// Stop: halt the turn and drop what was waiting behind it.
el.stopButton.addEventListener("click", () => {
  activeConnection().sendCommand("cancel", {});
});

// Push: stop the turn and send this through it, rather than behind it.
el.pushButton.addEventListener("click", () => sendMessage({ interrupt: true }));

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
    // Enter sends; with a turn running that means it waits behind it. Modifier+Enter
    // is the keyboard form of Push, for when this message cannot wait.
    sendMessage({ interrupt: event.metaKey || event.ctrlKey });
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

/** Show one panel, and remember it for this conversation. */
function showRightTab(tab) {
  state.rightTab = tab;
  if (state.session) state.sessionTabs.set(state.session.id, tab);
  if (tab === "notes") {
    // Re-read whenever it is opened. The notebook belongs to the project, so
    // another conversation may have added to it since this panel last looked.
    state.notes = null;
  }
  renderRightbar();
}

document.getElementById("tabFiles").addEventListener("click", () => showRightTab("files"));
document.getElementById("tabNotes").addEventListener("click", () => showRightTab("notes"));
document.getElementById("tabThinking").addEventListener("click", () => showRightTab("thinking"));
document.getElementById("tabTodo").addEventListener("click", () => showRightTab("todo"));
el.stopContinue.addEventListener("click", () => continueLastTurn());
el.stopDismiss.addEventListener("click", () => hideStopNote());
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

// ------------------------------------------------------- in-app folder picker

/**
 * Choosing a project folder without a desktop dialog.
 *
 * The server lists one directory level at a time and returns every jump target
 * with it, so this works from any browser on any machine — including the Windows
 * session where the native chooser will not come to the front, and a headless
 * host that has no chooser at all. Nothing here joins path segments: every row
 * already carries an absolute path the server produced, which is what keeps the
 * displayed location and the chosen one from drifting apart.
 */
const folderModal = document.getElementById("folderModal");
const folderState = {
  path: "",
  crumbs: [],
  entries: [],
  parent: null,
  roots: [],
  truncated: false,
  showHidden: false,
  resolve: null,
};

/** Open the picker and resolve with the chosen absolute path, or null. */
function openFolderPicker(startPath) {
  folderState.showHidden = false;
  el.folderHidden.setAttribute("aria-pressed", "false");
  el.folderNewName.value = "";
  el.folderNote.hidden = true;
  folderModal.hidden = false;
  el.folderUse.focus();
  return new Promise((resolve) => {
    folderState.resolve = resolve;
    loadFolder(startPath || undefined);
  });
}

function closeFolderPicker(path) {
  if (folderModal.hidden && !folderState.resolve) return;
  folderModal.hidden = true;
  const resolve = folderState.resolve;
  folderState.resolve = null;
  if (resolve) resolve(path || null);
}

async function loadFolder(path) {
  try {
    // No path asks the server for its own home directory, which is the right
    // place to start when nothing has been chosen yet.
    const level = path
      ? await api(`/api/dialog/browse?path=${encodeURIComponent(path)}`)
      : await api("/api/dialog/browse");
    applyFolder(level);
  } catch (cause) {
    // The current level stays on screen: a typo in the path box should cost the
    // message, not the place the user had navigated to.
    el.folderNote.textContent = cause.message;
    el.folderNote.hidden = false;
  }
}

function applyFolder(level) {
  folderState.path = level.path;
  folderState.crumbs = level.crumbs || [];
  folderState.entries = level.entries || [];
  folderState.parent = level.parent || null;
  folderState.roots = level.roots || [];
  folderState.truncated = Boolean(level.truncated);
  el.folderPath.value = level.path;
  el.folderNote.hidden = !folderState.truncated;
  if (folderState.truncated) {
    el.folderNote.textContent =
      "This folder has more subfolders than can be listed. Type a path to go straight to one.";
  }
  renderFolder();
}

/** True when `inside` is `root` itself or somewhere below it. */
function within(inside, root) {
  if (!inside || !root) return false;
  const a = inside.toLowerCase();
  const b = root.toLowerCase();
  return a === b || a.startsWith(b.endsWith("\\") || b.endsWith("/") ? b : `${b}/`) ||
    a.startsWith(`${b}\\`);
}

function renderFolder() {
  el.folderCrumbs.replaceChildren();
  folderState.crumbs.forEach((crumb, index) => {
    if (index > 0) el.folderCrumbs.append(node("span", "folder__sep", "›"));
    const crumbButton = node("button", "folder__crumb", crumb.name);
    crumbButton.type = "button";
    crumbButton.title = crumb.path;
    crumbButton.setAttribute("aria-current", String(crumb.path === folderState.path));
    crumbButton.addEventListener("click", () => loadFolder(crumb.path));
    el.folderCrumbs.append(crumbButton);
  });

  const visible = folderState.entries.filter(
    (entry) => folderState.showHidden || !entry.hidden,
  );
  const hiddenCount = folderState.entries.length - visible.length;

  // Entries first, drives last. At a drive root the other volumes used to come
  // first, which pushed the folder's own contents below the fold: ten rows of
  // "D:\ E:\ F:\" filled the panel and the answer to "what is in here?" was
  // nowhere on screen. The folder being looked at leads; the jump targets follow.
  const rows = [];
  if (folderState.parent) rows.push({ name: "..", path: folderState.parent, up: true });
  rows.push(...visible);
  const drives = folderState.roots.filter((root) => !within(folderState.path, root));
  if (drives.length) {
    rows.push({ group: "Other drives" });
    for (const root of drives) rows.push({ name: root, path: root, drive: true });
  }

  el.folderList.replaceChildren();
  if (visible.length === 0) {
    // Say which silence this is. "Nothing here" and "everything here is hidden
    // and the toggle is off" look identical otherwise, which is how a level full
    // of folders came to read as a broken picker.
    el.folderList.append(
      node(
        "p",
        "folder__empty",
        hiddenCount
          ? `No subfolders shown. ${hiddenCount} hidden — choose Hidden to show them.`
          : "No subfolders here.",
      ),
    );
  }
  for (const row of rows) {
    if (row.group) {
      el.folderList.append(node("p", "folder__group", row.group));
      continue;
    }
    const button = node("button", "folder__entry");
    button.type = "button";
    button.setAttribute("role", "option");
    button.append(node("span", "folder__entryIcon", row.up ? "↰" : row.drive ? "▣" : "▸"));
    button.append(node("span", "folder__entryName", row.name + (row.hidden ? "  (hidden)" : "")));
    button.addEventListener("click", () => loadFolder(row.path));
    el.folderList.append(button);
  }

  // A count is the quickest answer to "did this load, or is it empty?".
  el.folderCount.textContent = visible.length === 1 ? "1 folder" : `${visible.length} folders`;
  el.folderCount.hidden = visible.length === 0;
  el.folderUp.disabled = !folderState.parent;
  // A new level starts at its first folder, not where the last one was scrolled.
  el.folderList.scrollTop = 0;
}

el.folderUp.addEventListener("click", () => {
  if (folderState.parent) loadFolder(folderState.parent);
});
el.folderUse.addEventListener("click", () => closeFolderPicker(folderState.path));
el.folderHidden.addEventListener("click", () => {
  folderState.showHidden = !folderState.showHidden;
  el.folderHidden.setAttribute("aria-pressed", String(folderState.showHidden));
  renderFolder();
});
el.folderPath.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  loadFolder(el.folderPath.value.trim() || undefined);
});
el.folderNew.addEventListener("click", async () => {
  const name = el.folderNewName.value.trim();
  if (!name) {
    el.folderNewName.focus();
    return;
  }
  try {
    const created = await api("/api/dialog/browse", {
      method: "POST",
      body: JSON.stringify({ path: folderState.path, name }),
    });
    el.folderNewName.value = "";
    // Land inside the folder that was just made: it is almost always the one
    // the user means to choose.
    await loadFolder(created.path);
  } catch (cause) {
    toast(cause.message || "Could not create that folder.", "error");
  }
});
el.folderNewName.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    el.folderNew.click();
  }
});
document.getElementById("folderCancel").addEventListener("click", () => closeFolderPicker(null));
document.getElementById("folderMask").addEventListener("click", () => closeFolderPicker(null));

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

// ----------------------------------------------------------- text prompt

let promptResolver = null;

/**
 * Ask for one line of text. Resolves the trimmed value, or null on cancel.
 *
 * Enter submits, which is what anyone types into a name field expects; Escape
 * cancels for the same reason.
 */
function promptAction({ title, label, value = "", confirmLabel = "Save", note = "" }) {
  document.getElementById("promptTitle").textContent = title;
  document.getElementById("promptLabel").textContent = label;
  document.getElementById("promptInput").value = value;
  document.getElementById("promptNote").textContent = note;
  document.getElementById("promptNote").hidden = !note;
  document.getElementById("promptOk").textContent = confirmLabel;
  const modal = document.getElementById("promptModal");
  modal.hidden = false;
  const input = document.getElementById("promptInput");
  input.focus();
  input.select();
  return new Promise((resolve) => {
    promptResolver = resolve;
  });
}

function closePrompt(result) {
  const modal = document.getElementById("promptModal");
  if (modal.hidden && !promptResolver) return;
  modal.hidden = true;
  const resolve = promptResolver;
  promptResolver = null;
  if (resolve) resolve(result);
}

document.getElementById("promptOk").addEventListener("click", () => {
  closePrompt(document.getElementById("promptInput").value.trim() || null);
});
document.getElementById("promptCancel").addEventListener("click", () => closePrompt(null));
document.getElementById("promptMask").addEventListener("click", () => closePrompt(null));
document.getElementById("promptInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    closePrompt(event.currentTarget.value.trim() || null);
  } else if (event.key === "Escape") {
    event.preventDefault();
    closePrompt(null);
  }
});

document.getElementById("archiveToggle").addEventListener("click", () => {
  state.showArchive = !state.showArchive;
  renderSessions();
});
el.archivePurge.addEventListener("click", purgeArchive);
document.getElementById("newProject").addEventListener("click", openProjectModal);
document.getElementById("projectClose").addEventListener("click", closeProjectModal);
document.getElementById("projectCancel").addEventListener("click", closeProjectModal);
document.getElementById("projectMask").addEventListener("click", closeProjectModal);

// Browse opens the in-app picker, which needs nothing from the machine's desktop
// and therefore works everywhere. "System…" additionally offers the native
// chooser, where /api/health reported that this machine can show one.
function usePickedFolder(path) {
  document.getElementById("projectRoot").value = path;
  // The folder's own name is nearly always the project name the user wants.
  const name = document.getElementById("projectName");
  if (!name.value.trim()) {
    name.value = path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
  }
}

document.getElementById("projectBrowse").addEventListener("click", async () => {
  const current = document.getElementById("projectRoot").value.trim();
  const picked = await openFolderPicker(current || undefined);
  if (picked) usePickedFolder(picked);
});

// The native chooser opens on the machine running the server: that is where the
// agent reads and writes, and a browser file handle is not a path it could be
// confined to. Cancelling returns no path, which is not an error.
document.getElementById("projectSystem").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const picked = await api("/api/dialog/folder", {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (picked.path) usePickedFolder(picked.path);
  } catch (cause) {
    toast(cause.message || "Could not open a folder chooser.", "error");
  } finally {
    button.disabled = false;
  }
});

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

/**
 * Tell the user once that a newer Surtitle exists.
 *
 * Nothing is recorded unless the message was actually shown, and a failure here
 * is deliberately silent: an update notice that cannot be fetched must not look
 * like the app is broken.
 */
async function noticeRelease() {
  try {
    const release = await api("/api/release");
    if (!release.available || !release.can_announce) return;
    const version = (release.latest || {}).version;
    if (!version) return;
    toast(`Surtitle ${version} is available — update from the tray menu or the releases page.`);
    await api("/api/release/noticed", { method: "POST", body: JSON.stringify({}) });
  } catch {
    // Offline, or the endpoint is unhappy: neither is the user's problem now.
  }
}

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
    // The native chooser opens on the server's desktop, so that button is only
    // offered where the machine actually has one. The in-app picker next to it
    // needs no such permission and is always available.
    document.getElementById("projectSystem").hidden = !health.folder_dialog;
    if (!health.deepseek_configured) {
      toast("Add your DeepSeek API key in Settings to start.", "error");
      settings.open();
    } else if (!health.deepgram_configured) {
      toast("Voice is unavailable without a Deepgram key; you can still type.", "error");
    }
  } catch (error) {
    toast(`Cannot reach the server: ${error.message}`, "error");
  }

  // A newer release is worth one sentence, and only a few times. The server keeps
  // the count, so a browser and the tray share one budget instead of each
  // deciding for itself that the user has not heard yet.
  noticeRelease();

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
  for (const socket of connections.values()) socket.close();
  capture.dispose().catch(() => {});
  playback.close().catch(() => {});
});

main();
