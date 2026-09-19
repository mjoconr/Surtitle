# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **There is a macOS build for Intel Macs, and the updater will no longer install
  an archive built for a different architecture.** The macOS release was arm64
  only, and the updater chose an asset by *preferring* a matching architecture
  rather than requiring one — so on an Intel Mac nothing matched, and it fell back
  to the arm64 archive. That does not run slowly or badly: the archive carries its
  own interpreter, so it fails to start at all, after replacing a working install.
  Intel Macs now get `surtitle-<version>-darwin-x86_64.tar.gz`, built on the Intel
  runner (`macos-13` was retired; `macos-15-intel` is what replaced it), and an
  architecture the release does not cover is told so instead of being handed
  something that cannot run. Apple silicon may still fall back to the Intel build
  through Rosetta, because that direction works.

## [0.10.0] - 2026-09-19

### Added

- **An eval harness, and the first tasks for it.** `python -m evals` runs real
  questions — the work recorded in an agent session, and this project's own
  history — through the shipped turn path, and scores the result on objective
  checks: which tools were called, whether the turn finished, and what the answer
  had to contain. Until now every change to the prompt, the model settings or the
  tools was a coin flip, because there was nothing to compare against. Results are
  written to `evals/runs/` for exactly that comparison. Tasks of your own live in
  the gitignored `evals/tasks/`, because a task names a real project; what is
  committed is the harness and a sample project it can run against.
  `evals/README.md` says how to add a task; `tests/test_eval_harness.py` covers the
  harness itself offline.

### Changed

- **A recent turn is replayed to the model as it happened, not as a summary of
  itself.** On later turns every tool call used to arrive as one line of prose
  keeping 320 characters of its outcome, while the stored result holds up to four
  thousand — so a turn could read a file and the next turn could not remember what
  it said, and read it again. The two most recent turns now replay as the
  exchanges that produced them: the call, its result, then the answer, with a
  failed call still saying so. Older turns keep the summary, which names the tool
  and its target, and a result too large to replay whole says where it was cut.
  An existing database gains what it needs on open.

- **The agent thinks harder before it acts: reasoning effort is now `high`, and a
  single response may run to 32,768 tokens.** The defaults were `low` and 4,096,
  and that pair is the largest single reason a Surtitle turn looked less capable
  than the same model in a harness that left it alone: at low effort the work was
  never attempted rather than attempted badly, and a long plan or a large edit
  could be cut off mid-thought. Nothing about the spoken channel changes — the
  extra thinking is not extra talking — so lower `SURTITLE_REASONING_EFFORT` only
  if a turn feels slow, not because the default is reckless.

## [0.9.2] - 2026-09-19

### Fixed

- **A turn that pauses with the plan still open is no longer recorded as the
  answer.** The loop ends a turn on the first round that calls no tool, so a model
  that stopped mid-work in prose — "let me now check the parser", "the report is
  not written yet", "say go and I'll start at change 1" — ended the turn as
  `complete`: no banner, no Continue button, and a Plan tab still claiming work was
  outstanding. In one real session that happened eight times in a day, each one
  answered by the user typing "continue". When a working turn ends with items still
  open, the agent is now asked once to carry on with the next one — or to tick what
  is done, or to say plainly that it is stopping — and it is told which items the
  user can see. A turn that ran no tools at all is untouched: answering a question
  is not a pause in the work.

## [0.9.1] - 2026-09-19

### Fixed

- **A turn whose model round came back empty is no longer stored as a success with
  nothing in it.** The wrap-up added in 0.9.0 was asked only of a turn that had
  already done work, so a model that answered the *first* round with nothing at all
  fell through and was stored as `complete` with a zero-character assistant
  message: silence, no work log, no stop banner and no Continue button — the one
  shape of "it stopped" with nothing on screen to act on. Two real turns on
  2026-09-19 were stored that way. The wrap-up is asked whenever the closing round
  is empty, and a turn that is still empty after being asked is reported as having
  no answer, spoken and banner'd, in the form that matches whether it had a work
  log.
- **A spoken utterance no longer runs a second turn beside the one it replaced.**
  Speaking over the agent cancels the running turn, and a cancelled turn's exit
  drains the queue — so a request already waiting behind it was started, and then
  the utterance was started on top of that. Two turns ran at once, one of them
  untracked and unreachable by Stop, with both replies and both reasoning streams
  interleaved into one conversation. The real 2026-09-19 session shows the shape:
  two `turn ended` lines seven seconds apart and two assistant messages written into
  one turn. The drain is now held for exactly the replacement, and a request that
  arrives as a turn starts is no longer able to race the turn already starting.

## [0.9.0] - 2026-09-19

### Added

- **The right-hand panel now answers four questions instead of offering three
  unlabelled lists.** **Plan** leads and is always there — it was the last tab and
  hidden until the agent happened to write a plan, so the panel opened on a file
  browser and the useful view was the one nobody had seen. **Thinking** is what the
  tab formerly called Activity always was: the step, the reasoning behind it, the
  tool it called, and the result, now reading oldest-first and following the newest
  so it visibly moves, with the running step's reasoning shown in full.
  **Notes** is the project notebook — what `remember` has written and what the
  agent is given at the start of every conversation. It was completely invisible:
  the one durable thing a turn produces was the one thing you could not check, and
  the only route to it was finding `.surtitle/notes.md` on disk. **Files** leads
  with what the turn read and wrote, and the project tree is folded behind one
  line — it was a wall of dot-directories saying what exists, which you already
  knew. Which tab is open is remembered per conversation.
- **Stop and Push, and a request that no longer has to wait.** A message typed
  while the agent works used to be refused with "Still working on the previous
  request. Stop it first or wait", and an utterance captured in the same window was
  dropped with no message at all. It is held and run when the turn ends, shown in
  the transcript where you typed it. **Push** stops the turn and takes its place;
  **Stop** halts the work and drops what was waiting behind it. Both appear only
  while a turn is running, and modifier+Enter is the keyboard form of Push.
- **A context meter, above every tab.** It reports how much of a 128k working
  budget the conversation is carrying, how much of that the provider served from
  its cache, and the model's own window beside them. The one number previously on
  screen was a running total of every token the process had ever sent, which grows
  forever and says nothing about the conversation in front of you.
- The agent's notebook writes appear in the transcript as **Learned** blocks, and
  the line that says the turn is still working now names what it is doing
  ("Running run_shell") rather than only that time is passing.

### Changed

- **The conversation's changing context moved out of the system prompt.** The plan,
  the notebook and the project's top-level listing were sections of the system
  message — the head of every request — and all three change during a session, so
  nearly every turn began by invalidating the provider's prefix cache. DeepSeek
  serves a cache hit for a fiftieth of a miss. They are delivered as a message
  after the history now; the agent still receives all of it every turn.
- **A tool result is pruned head-and-tail instead of being cut short**, and to
  6,000 characters rather than 24,000. Every result of the current turn stays in
  the request until the turn ends, so a long turn could previously add a quarter of
  a million characters to a single request. The middle is what goes, the count of
  what went is stated, and the opening and the conclusion survive.
- **An old turn's work log is shortened, once.** Past the newest six turns, each
  action keeps its tool and target and loses the tail of its outcome, and beyond
  the twelfth action the count of what went is stated. Written once into the
  model's copy so the replayed history stays byte-stable, which is what keeps the
  cache working; the transcript you read keeps every action.
- **The context window is taken from the model rather than assumed.** A 128k
  default against `deepseek-flash`'s published 1M read "55% full" at 7%. The
  window comes from the same table as the prices, `SURTITLE_CONTEXT_LIMIT`
  overrides it, and `SURTITLE_CONTEXT_BUDGET` sets the working budget the meter
  measures against. An unknown model gets no meter rather than a wrong one.

### Fixed

- **A turn that finished with nothing to say is no longer stored as a success.**
  Reported on 0.8.1 as the agent stopping again: a turn opened with a spoken
  preamble, worked four rounds and nine tool calls, and finished on a round that
  produced nothing at all — no speech, no display, no call. Whether the closing
  round said anything was asked of the *turn*, and the opening preamble satisfied
  it, so the turn completed quietly and the user heard a preamble and then silence.
  It is asked of the closing round now: one wrap-up request, and if that also comes
  back empty the turn is reported as having no answer — stored, banner'd and
  spoken. The transcript also ends with a closing section of its own when a turn
  produced no answer, because a turn that stops mid-work otherwise ends on a Think
  block, which reads as "still going".
- **A session that has been closed no longer keeps its socket.** Reported as
  "I just restarted but it seems broken, text and voice", cured only by a page
  refresh. The session underneath had been torn down — engines stopped, events
  dropped — while its connection kept dispatching into it, so a turn ran, stored
  its work, and reached neither the screen nor the speaker. The connection now ends
  and the browser reconnects into a working session, and both close paths are
  logged.
- **The microphone can no longer be switched off while audio is still being
  recognised.** Capture posted its mute message to the audio worklet only when the
  backend was labelled `worklet`, so a worklet attached under another label kept
  sending frames; and the server never checked whether the microphone was open, so
  it fed them to the recogniser regardless. The log showed a microphone closed at
  15:04:42 and an utterance committed at 15:04:55 from 236 seconds of audio. The
  worklet is told whenever there is a node to tell, and frames for a closed
  microphone are refused and counted.
- **The Notes panel no longer reports an empty notebook when the read failed.** A
  failed fetch was cached as "nothing recorded yet", so a notebook with 3,700
  characters in it sat behind an empty panel long after the reason had gone. It is
  re-read whenever the tab is opened.
- **Closing a turn no longer discards the turn the queue has just started.**
  `cancel_turn` cleared the session's turn after awaiting the cancelled task, and
  that task's exit drains the queue — so Stop or Push could leave a live turn
  untracked, unreachable by a later stop.

## [0.8.1] - 2026-09-19

### Added

- **The agent can see its own plan, and knows what is on your screen.** The plan
  was write-only: `todo_write` recorded it and nothing ever replayed it, so an
  agent whose context had moved on could not say which item was still unticked —
  and when asked exactly that, it could not. The plan now reaches the agent at the
  start of every turn, with the state of each item and a plain statement of what
  is unfinished, so "which item is left?" is a lookup rather than a guess. The
  prompt also describes the interface it is speaking into: the conversation
  column, the Files / Activity / Plan tabs, the fact that the Plan tab appears
  when a plan is written and then stays — after the turn ends and across a reload
  — and that speaking over it or stopping it ends the turn where it stands.
- **The agent no longer announces that it is about to check something.** Checking
  is the standing expectation, so "I'm not sure, I'll need to look at the details"
  is the same sentence every turn and tells the user nothing they did not already
  know. It looks, and reports what it found. Offering to check something
  checkable is out too: an assumption worth stating is one that looking cannot
  settle.
- `search_history` now searches the conversation in progress as well as earlier
  ones, and labels each hit with where it came from. Excluding the current session
  removed the only way to recover work that had aged out of the replayed context,
  which is precisely when an agent concludes that somebody else must have done it.

### Fixed

- **The Activity panel moves while the agent is thinking.** The panel builds one
  row per step from that step's first reasoning delta, and only the create path
  scheduled a repaint — so every delta after the first updated the row in state
  and left it frozen on screen, and a step that thought for a minute looked
  exactly like a step that had died. The row also gisted the *first* line, which
  suits a finished step but stops changing within a second of a running one; a
  live row now follows the newest line and repaints itself.
- **A finished conversation is no longer labelled "Stopped" when you reopen it.**
  The reload path raised the banner whenever the replayed transcript contained any
  thinking or tool call — which is every turn that ever did anything — and blamed
  the step limit, a cause the browser has no way to know. Reopening a conversation
  that had completed normally put "Stopped — the step limit was reached" above a
  complete answer. A stopped turn is now one that was *never answered*, and the
  banner says only what is known: the turn was interrupted, whether by a restart,
  a cancellation, or a crash.
- **"Deep diving…" no longer sits on screen after the turn has ended.** Clearing
  the working line and writing the frozen step durations happen in the same pass,
  and the line was removed before that pass rather than after — so any row left
  un-ended put it straight back, and the timer was stopped immediately afterwards.
  The result was a frozen "Deep diving… 1m 11s" beside a finished answer, which
  reads as the agent still working. A turn that has ended is now never shown as
  working, whatever a leftover row says.
- **A turn no longer ends with its answer only on screen.** The "must not be
  silent" guarantee was checked across the whole turn, so a turn that opened with
  a spoken "Let me find the push route before I write anything" and then finished
  with the entire result — the note, the revision, the question about committing —
  in the display channel passed it. A listening user heard the intention and then
  silence. The round that ends the turn is now checked on its own: if it produces
  nothing to say, its conclusion is spoken. The prompt also asks for a closing
  line carrying the outcome, and says plainly that `<display>` is for the
  evidence behind the answer rather than a diary of the search.
- **The microphone works again.** Letting several conversations run at once
  replaced the single socket with one per conversation, and rebound every call
  site but one: the capture callback still sent its audio frames to the old
  single-socket name. A bare `connection` does not fail loudly in a browser — it
  resolves to the element with `id="connection"` — so every captured frame threw
  `sendAudio is not a function` and was dropped. Capture reported success and the
  level meter moved while the server received nothing at all, logging "no audio
  arrived for this listening session -- the problem is in the browser's capture",
  which points at the wrong end of the wire. Frames now go to the conversation on
  screen, which is the one the microphone follows.
- **A long conversation no longer erases what the agent has just done.** The
  context replayed to the model was taken from the *beginning* of the transcript
  rather than the end (`ORDER BY id ASC LIMIT 40`), and reasoning is stored as one
  row per step — so a session consumed its 40 rows within the first turn or two
  and the model saw only its opening exchange for the rest of it. In the session
  this was found in, the replayed window held three messages from the first ninety
  seconds while 190 tool calls, two commits and an entire feature were built
  outside it. The agent then re-proposed work it had already finished and reported
  that "somebody" had already done it, and could not tell that the somebody was
  itself. The window is now the **newest** messages, counted over the conversation
  rather than the audit trail beside it.
- **An ordinary finished turn is no longer labelled "Stopped".** The stop
  banner's reason lookup ended in a default of `{ title: "Stopped", detail: "" }`,
  so `reason: "complete"` — every turn that goes well — put a bare "Stopped" on
  screen with nothing underneath it. Only the endings that need explaining now get
  a banner: the step limit, a turn that did work but produced no answer, and a
  failure.
- **Echo suppression is no longer lifted about 0.2 seconds into every reply.** The
  watchdog measures silence against the last audio frame actually sent, and during
  a think — or simply between turns — that timestamp is minutes old, so its next
  tick judged live suppression stale and released it, leaving the microphone open
  for the whole answer. The silence clock now starts when speech does. The log said
  it plainly: `echo suppression had outlived the agent's audio by 115.6s (0
  transcript(s) were discarded while it was on)` — the counter was zero because
  suppression had not been doing anything.
- **The transcript and its command list keep their most recent entries when they
  are capped.** `list_messages` and `list_tool_calls` took the first N rows as
  well, so a long conversation showed its opening rather than what had just
  happened — in the model's case with the consequences described above.

## [0.8.0] - 2026-09-19

### Added

- **Several conversations can work at once, in one project or across projects.**
  The app held a single connection and closed it on every switch, so moving to
  another conversation stopped the one you left: it carried on running on the
  server with nothing listening for its events, and its progress and its answer had
  no way back. Each open conversation now keeps its own connection, so a long job
  in one can run while you read or work in another — the server has always allowed
  this; only the browser was holding it back. A conversation that is working shows
  it in the sidebar with a pulse, and marks itself **needs you** for an approval or
  **reply** when an answer is waiting, so you can see what needs attention without
  opening each one.
- Conversations keep working when you switch project, not only when you switch
  chat. Their transcripts are replayed from the store when you return, so nothing
  that happened while you were away is lost.
- The microphone follows the conversation on screen: there is one microphone, and a
  chat you have navigated away from must not keep listening to the room. The
  conversation itself is unaffected — only its ears close.

### Fixed

- **Your words are no longer thrown away while the agent is speaking.** Echo
  suppression exists to stop the agent transcribing its own voice, and it is
  released when the synthesiser reports that it has finished. When that release
  did not come, every transcript was silently discarded for the rest of the
  session: in the session this was found in, a complete sentence — "The question
  is, uh, is the slower speed losing more than we…" — was transcribed and then
  dropped, forty seconds after playback had stopped, with the microphone
  apparently open and loud audio arriving. Suppression is now bounded by the audio
  actually sent: a silence longer than that audio could still account for lifts it
  and writes a warning naming how many transcripts were lost. The UI also shows
  when speech is being suppressed ("Listening (agent speaking)"), so the one voice
  failure that used to leave no trace on screen is visible while it is happening.
- **A sentence is no longer cut in half and answered a piece at a time.** A
  recogniser's end of turn is not always the end of what you were saying: "So we
  could work out a simulation" and "of this." were reported 1.5 seconds apart as
  two turns, so the agent began answering half a sentence and the fragments that
  followed cancelled the turn before it. An utterance is now held briefly
  (`SURTITLE_STT_MERGE_HOLD_MS`, default 1200 ms) and anything that continues it
  is merged into one message before the agent sees it, bounded by
  `SURTITLE_STT_MERGE_MAX_MS` so a speaker who never pauses still gets an answer.

## [0.7.0] - 2026-09-19

### Fixed

- **A turn that did the work no longer ends without an answer.** A model round can
  come back with no text and no tool calls, and the loop treated that as a finished
  turn: the reply was stored as its `[work this turn]` log alone, with nothing to
  read and nothing spoken. In the session this was found in, the agent made 38 tool
  calls across 28 steps and eleven minutes on an investigation, then
  closed the turn with an empty message — which from the user's side is
  indistinguishable from the agent having stopped. A turn that has work behind it and
  produces no text is now asked once to wrap up and speak; if that also comes back
  empty, the turn ends with a reported failure and a spoken explanation rather than
  being stored as a silent success.
- **You can see what the agent is thinking, and what it did about it.** A long turn
  used to show a wall of identical `run_shell` rows: the reasoning that explained
  them sat in a separate Activity panel, truncated to a trailing fragment, in no
  particular order relative to the commands it produced. A turn is now grouped into
  the model rounds that produced it — each one with its reasoning behind a "Think"
  disclosure, then the calls that round made — in the transcript and in the Activity
  panel, which are two views of the same thing. The reasoning is stored per round, so
  reopening a conversation shows the process rather than only the answer, and the
  tool calls, outcomes and durations that were already being stored are now replayed
  instead of being fetched and ignored.
- **Running work shows how long it has been running.** Each step and each tool call
  carries an elapsed time, and a turn that is still going ends with a live
  "Deep diving… 14m 38s" line. The status pill said "Thinking" whether that had been
  true for one second or twenty minutes, which left no way to tell working from hung.
- **The agent keeps a plan you can watch.** A new `todo_write` tool lets the agent
  record what it intends to do and update it as it goes, shown in a Plan tab in the
  right-hand panel beside the work it describes. It is stored with the conversation
  rather than the turn, so it survives a reload and shows what is still outstanding.
- **A turn that stops says why, on screen and aloud.** A turn cut short by the step
  limit or an error now ends with a banner naming the reason and offering
  **Continue**, and the agent speaks the same sentence. It also warns once when the
  step budget is nearly spent, so the stop is expected rather than a surprise.
  Previously the indicator returned to "Idle" whether the work had finished or been
  abandoned, and the only way to find out was to ask again.

### Notes

- Reopening a conversation rebuilds that process view from what was already stored.
  Conversations recorded before this release have no stored reasoning, so they show
  their steps and commands without the Think blocks.
- The local speech engines remain an opt-in install (`--with-voice-local` at build
  time, or **Settings → Voice**, the tray menu, or `surtitle voice install` after).
  Neither the in-app install nor an in-place update prunes them: both sync with
  `--inexact`, which is what stops a later `uv sync` from removing the extra that
  was just installed.

## [0.6.0] - 2026-09-19

### Fixed

- **The agent no longer loses what it found, and does the work again.** Every turn
  records the work it did under `[work this turn]`, and the agent is instructed to
  reuse those results — but the line recorded for a command held only its status
  ("finished in 146 ms") and never its output, so there was nothing to reuse. In one
  session it checked `import sherpa_onnx`, saw the module was missing, re-ran the
  same check on the next turn, and then asked the user to confirm a fact it had
  already established. A result now carries a bounded excerpt of what it printed,
  and the guard that refuses a repeated call belongs to the conversation rather than
  to a single turn.
- **An approval prompt no longer vanishes before it can be answered.** The agent
  says what it is about to do before doing it, so a request to approve a file edit
  could arrive while the reply was still being spoken. When the reply finished, that
  state change cleared the prompt from the screen while the server carried on
  waiting for an answer — 637 ms after it appeared, in the log this was found in.
  The prompt now survives the reply ending, and a question still open when the page
  reloads is asked again.
- **Reloading the page no longer throws away a running turn.** A reload disconnects
  before it reconnects, and the disconnect closed the conversation, cancelling
  whatever the agent was doing — so reloading while it was working lost the work.
  A conversation with work in flight now waits for the browser to come back, and is
  closed if it does not.
- **Changing a setting no longer breaks the conversation you are in.** Saving any
  preference closed the DeepSeek client and installed a replacement that live
  conversations could not see, so the next thing said failed with "Cannot send a
  request, as the client has been closed". Switching the voice engine was enough to
  trigger it, and every later turn in that conversation failed the same way.
- **A local turn ends when you finish, not when a timer expires.** Local
  recognition put a hard 20-second ceiling on an utterance and applied it whether or
  not anyone was still talking: an explanation was cut off mid-word at 20.16 s and
  the agent began work on half a problem statement. The timer is now graded by what
  the transcript looks like — a trailing "and" or "the" earns a longer wait, and
  anything that cannot be shown to be finished gets part of one — and the ceiling is
  a backstop that may only close a turn once you have actually paused.
- **`surtitle doctor` says when it skipped the local voice check.** It reported
  nothing at all unless a local engine was selected, and a missing line is
  indistinguishable from a pass: a machine with the `voice-local` extra missing
  produced a clean report, and that clean report was the reason the real cause went
  unexamined.

### Added

- **`Longest single spoken turn` is now a setting.** The ceiling that ended local
  turns was not exposed anywhere, so there was no way to work around it. It is in
  Settings beside the other turn-detection options, and both take effect without a
  restart.

### Changed

- **Local turn-taking waits longer before assuming you have finished.** The
  ceiling moves from 20 seconds to 60, an unfinished-sounding transcript earns part
  of the extension rather than the bare silence, and the backstop can no longer fire
  while audio is still arriving. If you use a local engine, the agent will pause
  longer before answering than it did — that is the intended trade, and
  `Local end-of-turn silence` tunes it. Turn detection on the hosted engine is
  unchanged.

## [0.5.2] - 2026-09-19

### Fixed

- **In-place updates on Windows did nothing.** The tray offered the release, the
  download and checksum succeeded, Surtitle closed — and it came back on the old
  version. `run.bat` starts the server with the installation folder as its working
  directory, and the updater inherited that directory, so PowerShell was running
  *inside* the folder it was about to rename. Windows refuses to rename a directory
  that any process has as its current directory, so the first `Move-Item` failed
  every time, the script threw, and nothing was swapped. The updater now runs from
  the app's data folder, and both scripts change directory there before touching
  anything. The moves also retry for a directory that is briefly still held, while
  a source that does not exist fails immediately rather than slowly.
- **A failed update is no longer silent, and no longer leaves the app down.** The
  swap happens after Surtitle has exited, so its outcome can only be reported in a
  file: every step is now written to
  `%LOCALAPPDATA%\Surtitle\updates\apply-update.log` with the result in
  `last-update.txt` beside it. The tray shows a failure as its own row naming the
  reason, selecting it explains what to do and where the log is, the Status dialog
  and `surtitle status` carry it, and retrying says what went wrong last time
  rather than repeating a blind attempt. If the swap fails the old installation is
  put back *and started again* — previously a failure left the user with no running
  app, and a leftover `.old` folder that could not be removed aborted the script
  before it relaunched anything.

  The tests missed this because they run the POSIX swap script, which allows that
  rename, from outside the directory it moves. There is now a test that asserts the
  updater's working directory is outside the installation, and the whole reporting
  path is covered.

## [0.5.1] - 2026-09-19

### Fixed

- **The folder picker leads with the folder you are browsing.** At `C:\` it listed
  the other drives first, and on a machine with a lot of mapped drives that was the
  entire visible panel — the folders inside `C:\` sat below the fold, and the one
  row on screen was the hidden `$Recycle.Bin`. The folder's own entries now come
  first, the other volumes follow under an "Other drives" label, the level reports
  how many folders it holds, and a level whose folders are all hidden says so
  instead of looking empty. The Hidden toggle shows that it is on, the up button is
  disabled at a filesystem root, and each level starts scrolled to its first entry.
- **Two 0.5.0 tests that misbehaved on Windows.** The Windows chooser-refusal test
  called the dialog entry point on Windows, where it is not the refusal branch — it
  opens a real modal dialog with nobody to close it, and both `windows-latest` CI
  jobs sat in "Run the test suite" for 43 minutes. It skips there now, as do the
  symlink tests on a platform that cannot create symlinks without elevation, and a
  POSIX-only file-name test.

## [0.5.0] - 2026-09-19

### Added

- **The project folder is chosen from a picker that works everywhere.** The native
  folder chooser could not be seen on Windows: it ran in-process on a worker thread
  of a detached server, so its window had no main thread and no claim to the
  foreground, and clicking Browse appeared to do nothing at all. The server now
  answers the question itself — `GET /api/dialog/browse` lists one directory level
  with the breadcrumb chain leading to it, `POST` creates a child folder — so
  **Browse…** is a JSON round trip that works from any browser on any machine,
  including a remote browser and a headless host the native chooser can never serve.
  The Windows chooser is kept as **System…** and now runs in its own process, with
  COM, per-monitor-v2 DPI awareness and a synthesized Alt press to take the
  foreground. Both halves are behind one capability the UI reads from `/api/health`,
  so the native button appears only where it could work. Listing is directories
  only, paths must be fully qualified, and both endpoints are loopback only.
- **A project can be deleted, and its conversations with it, without touching your
  files.** Project rows gained rename and delete actions. `DELETE /api/projects/{id}`
  drops the project's live conversations before forgetting it, and reports how many
  conversations went and which folder it left alone; the confirm dialog says the
  same thing before anything happens. A conversation can also be deleted straight
  from the live list, rather than only after archiving it.
- **Portable git and svn, so the agent has history on a machine with neither.** The
  tray's **Install git and svn…**, `surtitle tools install`, or the app downloads
  MinGit and Apache Subversion into `%LOCALAPPDATA%\Surtitle\tools` — no installer,
  no administrator rights, nothing added to your own `PATH`. Each archive is
  verified against a pinned SHA-256 and the unpacked binary must then run and report
  the version it should, because a corrupt download is common and a tree that
  unpacks but does not execute is the failure worth catching. Windows only,
  deliberately: elsewhere Surtitle uses the `git` and `svn` on your `PATH`, and says
  so instead of installing a second copy your system cannot see. `surtitle tools
  status`, `surtitle doctor` and `GET/POST /api/tools/vcs` report and drive the same
  thing; the install endpoint is loopback only.
- **The agent is told to use version control, and to ask before saving with it.**
  When a piece of work is done — the idea mostly works or is actually finished — the
  agent asks, in one short question, whether to add, commit and push and how
  detailed the commit message should be: one line, a summary, or detailed. It never
  commits, tags or pushes unasked, and an earlier yes does not cover later work. A
  session also reports which git and svn are installed and what the project's
  working copy currently is.
- **`vcs_status`, `vcs_guide` and `vcs_commit`.** `vcs_status` reports the working
  copy — system, branch or revision, uncommitted work, ahead/behind, remote.
  `vcs_guide` is the correct usage for git and svn: the model of each system, the
  commands that matter, how to undo at each level of destruction, and what is never
  committed. It is a tool result rather than prompt text, because it is long and only
  needed once the agent is about to touch history. `vcs_commit` is the one mutating
  step, behind an approval: it refuses an empty change rather than leaving an empty
  commit, refuses a body when you asked for one line, and keeps `.surtitle/` out of
  the commit — reporting it as excluded rather than including it silently.
- **The tray notices a new release and says so a few times.** At most three mentions
  of a given version, never two within twelve hours, and the count resets when a
  *newer* version appears; it lives in the data directory, so restarting the app does
  not restart the nagging, and the tray and the browser share one budget. The lookup
  is cached for hours, because the tray polls every couple of seconds and GitHub is
  not free. On Windows the announcement is a notification balloon rather than a
  dialog, and the menu gains a row naming the version and offering whichever update
  this installation can perform. A prerelease is never announced to somebody running
  a released build.

### Fixed

- **A conversation or project deletion no longer leaves a live session behind.** The
  runtimes hold the project root, so they outlived the record they belonged to and
  could still answer a WebSocket for a project that no longer existed.

## [0.4.0] - 2026-09-18

### Added

- **The launcher entry and start-at-sign-in are reachable from the tray.** Both
  were previously available only by re-running Setup, which is how someone who
  extracted the archive ended up with no Start Menu entry and no way to start at
  sign-in without going looking for a script. The right-click menu now offers
  **Add Start Menu entry** (which also repairs a stale entry, such as the iconless
  shortcut an earlier release created) and **Start at sign-in** / **Don't start at
  sign-in**. The work stays in the installers, run in a new shortcuts-only mode
  that touches nothing else — important, because the app is running out of the
  environment a full install would rebuild.
- **A downloaded release can update itself.** The tray's **Update to the latest
  release…** now works for a zip install instead of opening a browser. It
  downloads the build for this platform, verifies it against the release's
  published `SHA256SUMS.txt`, stages it beside the install, and — once the server
  has exited, which Windows requires because a running program's files are locked
  — swaps the directories and starts the new version. A failed swap puts the old
  directory back, so a failed update leaves a working installation rather than
  half of one. The data directory (settings, database, ~400 MB of speech models)
  lives outside the install and is untouched, which is why an update never
  re-downloads the models. `surtitle update` does the same from a terminal. An
  unpacked *source* ZIP still cannot replace itself and is pointed at the download
  page, as is a folder the user cannot write to.

### Fixed

- **An unattended installer run no longer deletes a sign-in entry it was never
  asked about.** With no `-Startup`/`-NoStartup` (or `--startup`/`--no-startup`)
  and nobody to answer the prompt, it used to take the answer as "no" and remove
  an existing entry. It now leaves the setting exactly as it found it — which is
  also what lets the tray add a Start Menu entry without disturbing sign-in.
- **A release archive's shortcut and app keep their icon.** Both installers
  resolved the icon from `src/surtitle/web`, which a release archive deliberately
  excludes — the copy inside the installed package is the one that travels — so
  the Start Menu entry was created with a generic icon and the macOS app was built
  without one. They now ask the interpreter that runs Surtitle where its own
  package lives, which is right in both layouts, and say so when no icon is found
  rather than failing quietly.
- **Launching no longer re-syncs the environment.** `run.bat`, `run.ps1` and
  `run.sh` ran `uv sync` on every start, which was wrong twice over: without
  `--extra voice-local` it uninstalls that extra even with `--inexact` (the extra
  is in the lock, so `--inexact` does not protect it), so the offline engines had
  to be installed again after every launch from a shortcut; and an environment
  that had drifted from the lock was rebuilt, re-downloading Python and every
  dependency. A warm environment is now launched as it is, and the bootstrap runs
  only when nothing is installed yet. Refreshing dependencies belongs to Setup,
  `-Update`, the tray's update, or an explicit `uv sync`.

## [0.3.0] - 2026-09-18

### Added

- **A release archive can add a launcher entry and a sign-in entry.** `Setup.bat`
  and `Setup.command` now ship inside the archive and detect it: they skip the
  Python and model steps the bundled runtime has already done, and go straight to
  a Start Menu entry (or `~/Applications/Surtitle.app`) and the "start when you
  sign in?" question. Before this the archive carried only the installer that
  refuses to run inside an archive, so someone who downloaded the zip had no way
  to get a menu entry without opening a terminal — which is exactly the case the
  setup path was asked to cover.

## [0.2.0] - 2026-09-18

### Added

- **Choose the project folder with a native dialog.** The new-project dialog has
  a **Browse…** button wherever the machine can show a chooser — the Win32 folder
  picker on Windows, `choose folder` on macOS, zenity or kdialog on Linux — and
  the chosen folder pre-fills the project name. The browser cannot supply a
  usable path (a file handle is not a location the agent could be confined to and
  read), so `POST /api/dialog/folder` opens the dialog on the machine running the
  server. That endpoint is loopback-only — a remote caller must not be able to put
  a modal window on someone's desktop — and a machine with no desktop answers 501
  instead of hanging on a window nobody can see.
- **Update from the tray.** A git checkout gets two rows — **Update to the latest
  release…** and **Update to current main…** — each confirmed before anything
  moves, then run by the server (`POST /api/update`, loopback only) so the menu
  can report progress. The update is a fast-forward or a tag checkout, never a
  rebase: local work stops it rather than being overwritten, and `uv sync
  --inexact` refreshes the dependencies afterwards. The running process keeps the
  code it started with, so the result says to restart. A release archive cannot
  replace its own running files, so it is offered **Get the latest release…**,
  which opens the download page instead of failing. `surtitle update
  [--target release|main]` with `--check` does the same from a terminal.
- **Install the offline speech engines from the tray.** The right-click menu has
  an **Install local voice…** item whenever offline speech is incomplete. It
  asks before a large download, then hands the work to the server
  (`POST /api/voice/install`, loopback only) so it continues whichever icon
  started it. While it runs the menu row becomes progress, and it reads "Local
  voice is installed" once there is nothing left to do. `surtitle voice install`
  and `surtitle voice status` do the same from a terminal, and exit non-zero when
  offline speech is not ready.
- **One double-clickable setup file per platform.** `Setup.bat` on Windows and
  `Setup.command` on macOS install everything a source checkout needs: uv and
  Python, the dependencies, optionally the offline speech engines and their
  models, a launcher entry, and the sign-in question. Neither needs a terminal, a
  typed command, or administrator rights — the Windows one passes
  `-ExecutionPolicy Bypass` for its own invocation rather than asking anyone to
  change a machine-wide setting.
- **A launcher entry and an optional sign-in entry**, both per-user. On Windows
  the installer adds a Start Menu shortcut (already there) and now asks whether
  Surtitle should start when you sign in, creating or removing a Start-folder
  shortcut; the sign-in launch starts minimized with `--no-browser`. On macOS it
  writes `Surtitle.app` into `~/Applications` with the application icon, and a
  per-user LaunchAgent for sign-in. `-Startup`/`-NoStartup` and
  `--startup`/`--no-startup` answer without a prompt; `-Yes`/`--yes` means "take
  the defaults", which is not to add anything to sign-in silently.

### Fixed

- **An installation is no longer told the wrong reason it cannot update.** A
  release archive, an unpacked source ZIP and a git clone with git missing all
  used to report "not installed from a git checkout". They are now three separate
  answers: a clone pulls, a clone without git is told git is not installed, and
  something with no history at all is pointed at the download page. `surtitle
  update --check` says which applies.
- **The installers refuse to run inside a release archive.** `scripts/install.ps1`
  and `scripts/install.sh` ship inside the archive, and running one there built a
  second environment beside the bundled runtime — and since the launcher prefers
  `venv\`, it quietly changed which interpreter ran. They now say the archive is
  already installed and exit without touching anything.
- The download size quoted before fetching speech models was stale by more than
  four times: the registry's models are about 395 MB, not the ~86 MB the
  installers and prompts claimed. The app now reports the real figure from the
  model registry, and the scripts and docs no longer name a number at all.
- A launcher started with **no arguments now starts the app** instead of printing
  help and exiting. `run.bat`, `run.ps1` and `run.sh` forwarded an empty argument
  list to the CLI, so the double-click the documentation recommends left a window
  that flashed and vanished. Passing a command is unchanged: `run.bat doctor`,
  `run.bat models list`, `run.bat run --port 9000` all behave exactly as before.

## [0.1.0] - 2026-09-17

### Changed

- **The project is now Surtitle.** The previous working name has been removed
  from the package, CLI, environment variables, configuration paths, app data
  directory, and release artifacts.

  | Previous | Current |
  | --- | --- |
  | `src/agenticvoice/` | `src/surtitle/` |
  | `agenticvoice` (CLI) | `surtitle` |
  | `AGENTICVOICE_*` | `SURTITLE_*` |
  | `.agenticvoice.json` (per-project config) | `.surtitle.json` |
  | `.agenticvoice/` (per-project state) | `.surtitle/` |
  | `…/Application Support/AgenticVoice` (macOS app data) | `…/Application Support/Surtitle` |
  | `%LOCALAPPDATA%\AgenticVoice` (Windows app data) | `%LOCALAPPDATA%\Surtitle` |
  | `agenticvoice.db`, `agenticvoice.log` | `surtitle.db`, `surtitle.log` |
  | `agenticvoice-<version>-<platform>-<arch>` (archive) | `surtitle-<version>-<platform>-<arch>` |

- **Document conversion no longer requires LibreOffice.** A built-in engine reads
  Word, Excel, PowerPoint, OpenDocument and PDF files and writes PDF, text, CSV,
  HTML and XLSX with nothing installed. It carries content rather than layout.
  LibreOffice is still used when it is present — for the formats the built-in
  engine cannot write (`docx`, `odt`, `rtf`, `ods`, `odp`, `pptx`, `png`, `jpg`)
  and as a fallback that preserves appearance. The new `backend` argument
  (`auto`, `builtin`, `libreoffice`) selects one, and the result reports which
  ran in `data.backend`.
- `read_file` reads Office documents directly, extracting headings, paragraphs,
  lists and tables, so a `.docx` or `.xlsx` no longer has to be converted before
  it can be read.

### Added

- A **taskbar notification icon on Windows**, with a menu that reports live
  status, shows what the run has cost, and stops the server gracefully. It is on
  by default for `surtitle run` (`--no-tray` turns it off, `--tray` forces it),
  and `surtitle tray` attaches one to a server started any other way. The icon is
  built directly on `Shell_NotifyIcon` through `ctypes`, so it adds no dependency
  to the release archive.

  `Status…` reports the address, uptime, model, voice engines, which credentials
  are set, and what is stored; `Usage…` reports turns, tool calls, tokens in and
  out, cache hits, and an estimated cost. The estimate prices the peak and
  off-peak rates separately and applies the cache-hit discount, and shows no
  money figure at all for a model with no published rate.
- `GET /api/status`, the payload behind both the icon and the new
  `surtitle status` command (`--json` for scripting).
- `POST /api/shutdown`, which stops the server gracefully and refuses any request
  that does not come from this machine.
- `SURTITLE_PRICE_INPUT`, `SURTITLE_PRICE_CACHED_INPUT` and
  `SURTITLE_PRICE_OUTPUT` to override the published per-million-token rates
  without waiting for a release.
- An application icon — a waveform on the brand blue — used for the tray, the
  browser favicon and the Start Menu entry, regenerable with
  `uv run python scripts/make_icon.py`.
- `scripts/install.ps1` adds a Surtitle entry to the Start Menu for the current
  user (`-NoShortcut` skips it). It is a per-user shortcut, so it still needs no
  administrator rights.
- Local, offline speech recognition and synthesis, with models that never leave
  the machine. Each direction — recognition and synthesis — independently
  chooses a hosted or local engine. See [`docs/VOICE.md`](docs/VOICE.md).
- `surtitle models list | download | verify` to manage the local speech models.
- `scripts/install.sh` and `scripts/install.ps1` to install Python,
  dependencies, and optionally the offline voice engines in one step, each with
  an update path (`--update` on macOS/Linux, `-Update` on Windows).
- `pythonpath` in the pytest configuration so tests can share helpers through
  `tests.*` imports.

### Fixed

- `run.bat`, `run.ps1` and `run.sh` now find a `uv` that is installed but not on
  the current `PATH`. uv's installer updates the *user* `PATH`, which does not
  affect the shell that ran it, so the documented "install uv, then run the
  launcher" sequence failed in the same window — with `run.bat` then advising you
  to install the uv you had just installed. All three launchers now also check
  uv's documented install location (`%USERPROFILE%\.local\bin\uv.exe`,
  `~/.local/bin/uv`), which is what `install.ps1` had always done.
- The launchers find an existing `.venv` at the **checkout root**. They live in
  `scripts/` in a checkout but at the archive root in a release, and they only
  ever looked in their own directory — so `scripts\.venv` was checked and the
  real `.venv` was not. The manual path in the README (`python -m venv .venv`,
  `pip install -e .`, `run.bat`) reported "no Python environment was found" with
  a working environment sitting right there.
- A first run from a source checkout is no longer silent. It downloads the whole
  dependency set, and `uv sync --quiet` gave no sign of progress for several
  minutes — indistinguishable from the hang the user had just escaped. The sync
  is verbose until a `.venv` exists, and quiet afterwards.
- The "no Python environment was found" message now says which of the two
  situations you are in and what to type. It also names `install.ps1` as the
  one-step option, and `docs/WINDOWS.md` covers the `RemoteSigned` case where a
  downloaded script still needs `Unblock-File`.

- `scripts/run.sh` and `scripts/run.ps1` no longer remove the optional
  `voice-local` extra. A plain `uv sync` prunes anything the lock does not name,
  and `uv run` synced a second time on every launch, so installing the offline
  engines appeared to work and was undone by the next run.
- The `voice-local` extra now installs `sherpa-onnx-core` next to the bindings.
  That wheel marks its dependency as dynamic, which hides the edge from the
  resolver, so `sherpa-onnx` was installed without the native runtime it links
  against and failed to import.
- Release archives run on the machine that receives them. The bundled virtual
  environment linked `venv/bin/python` to an absolute path inside the build
  directory, so an extracted archive could only start where it was built — and
  Python 3.12 and later refused to extract it at all. Archive verification now
  asserts that the interpreter resolves inside the extracted tree, which is the
  check that would have caught this.
- Two tests that passed only on a machine with the right tools installed: the
  document-conversion tests needed a real LibreOffice, and the search test
  assumed ripgrep was on `PATH`.
- Windows archive verification tested the wrong interpreter. It looked for the
  POSIX venv layout and the Windows runtime layout but not `venv\Scripts\python.exe`,
  then silently fell back to the bundled runtime — which cannot import the
  application, so the smoke test failed while proving nothing about the archive.
- Path confinement on Windows refused paths it should have allowed.
  `Path.resolve()` returns an extended-length (`\\?\`) path for some inputs, and
  that prefix is a different first component, so a contained path looked like an
  escape. Long project paths hit this too.
- The Windows archive could not run anywhere but the build machine. A Windows
  virtual environment's `Scripts\python.exe` is a launcher that reads an absolute
  base path from `pyvenv.cfg`, so an extracted archive failed with
  `No Python at '...'` — and verification never noticed, because it extracted on
  the machine that built it. Windows archives now install into the bundled
  runtime, which is self-contained and moves with the tree.
- Release archives are pinned to `uv.lock`. The build looked for a
  `requirements-release.txt` that never existed and fell back to installing the
  project by path, so every archive was a fresh resolution — the last one carried
  `uvicorn` 0.53.0 while the lock pins 0.52.4, a version nothing had tested. The
  lock is now exported and installed from.
- The bundled wheelhouse is actually built. It asked `uv` for `pip download`,
  which is not a subcommand uv has, so on every platform the step failed and was
  reported as skipped, leaving archives without the offline repair their
  documentation promised. It now uses the bundled interpreter's own pip. That
  adds roughly 44 MB to a macOS archive; `--skip-wheelhouse` still produces the
  smaller build.
- A Windows release can be cut. The release workflow's smoke test asserted
  `venv\Scripts\python.exe`, which a Windows archive deliberately does not have —
  dependencies go into the bundled runtime, because a Windows venv cannot be
  relocated — so every `v*` tag would have failed after a full build. Archive
  verification now lives in one place, `scripts/build_release.py`, which decides
  the expected layout from the archive's own `BUILD-INFO.json`, and the new
  `--verify-only` re-runs that same verifier over `dist/` or over a download the
  user names. CI's completeness check had been passing for the wrong reason: it
  matched the substring `venv` inside the bundled runtime's `Lib\venv`.

### Migration — this rename is breaking

An existing installation will not pick up its settings, credentials, or history
until the data directory is moved. To carry them across:

```bash
# macOS
cd ~/Library/Application\ Support
mv AgenticVoice Surtitle
mv Surtitle/agenticvoice.db  Surtitle/surtitle.db
mv Surtitle/agenticvoice.log Surtitle/surtitle.log
```

```powershell
# Windows (PowerShell)
Set-Location $env:LOCALAPPDATA
Move-Item AgenticVoice Surtitle
Move-Item Surtitle\agenticvoice.db  Surtitle\surtitle.db
Move-Item Surtitle\agenticvoice.log Surtitle\surtitle.log
```

Then:

- Rename any `AGENTICVOICE_*` variables in your `.env` to `SURTITLE_*`. Run
  `surtitle init` if you would rather scaffold a fresh `.env`.
- Rename per-project configuration from `.agenticvoice.json` to
  `.surtitle.json`; a project picks its settings back up once renamed, and is
  otherwise treated as new.
- Rename per-project state directories from `.agenticvoice/` to `.surtitle/` to
  keep the isolated virtual environment and remembered package approvals.
  Leaving them in place is harmless — they are simply rebuilt on demand.
- Speech models are **not** carried over by the `.env` change alone. If you skip
  the data-directory move, run `surtitle models download` to fetch them again.

[Unreleased]: https://github.com/mjoconr/Surtitle/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/mjoconr/Surtitle/compare/v0.9.2...v0.10.0
[0.9.2]: https://github.com/mjoconr/Surtitle/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/mjoconr/Surtitle/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.9.0
[0.8.1]: https://github.com/mjoconr/Surtitle/releases/tag/v0.8.1
[0.8.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.8.0
[0.7.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.7.0
[0.6.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.6.0
[0.5.2]: https://github.com/mjoconr/Surtitle/releases/tag/v0.5.2
[0.5.1]: https://github.com/mjoconr/Surtitle/releases/tag/v0.5.1
[0.5.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.5.0
[0.4.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.4.0
[0.3.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.3.0
[0.2.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.2.0
[0.1.0]: https://github.com/mjoconr/Surtitle/releases/tag/v0.1.0
