# Tools

Every capability the agent has, what it is allowed to do without asking, and the
failure modes each one is designed around.

## Approval policy

Each tool declares an approval policy next to its schema, so a new tool cannot be
added without someone deciding whether it may run unattended.

| Policy | Meaning |
|---|---|
| `never` | Read-only or otherwise harmless. Never interrupts. |
| `ask` | Requires explicit approval, unless the user has trusted that tool for the project. |

A tool may also narrow the decision per call. `install_packages` uses this: it only
asks about packages the project has not already approved.

| Tool | Approval | Mutating |
|---|---|---|
| `list_dir`, `read_file`, `search_files` | never | no |
| `environment_info`, `search_packages` | never | no |
| `todo_write` | never | no |
| `goal_write` | never | no |
| `subagent` | never | no |
| `skill` | never | no |
| `web_fetch`, `web_search` | ask | no |
| `write_file`, `edit_file` | ask | yes |
| `run_python`, `run_shell` | ask | yes |
| `run_background` | ask | yes |
| `job_output`, `job_kill` | never | no |
| `make_pdf`, `make_spreadsheet`, `make_chart` | ask | yes |
| `convert_document` | ask | yes |
| `install_packages` | ask (per new package) | yes |
| `<server>__<tool>` (MCP) | ask, unless `trusted_tools` | yes |

`todo_write` writes no file, so it never asks: it records the plan the user watches
while the agent works. See [The plan](#the-plan).

`subagent` asks for nothing because it cannot do anything: the child it starts gets
`ToolRegistry.read_only()`, which is this table's `never`/non-mutating rows and
nothing else, minus the tools that need a conversation to belong to and minus
itself. A delegated agent that could write would be editing the project while the
user heard only the parent, so it is powerless by construction rather than by
instruction. See [Sub-agents](#sub-agents).

## Confinement

Every filesystem path in every tool goes through `tools/path_guard.py`.

```python
resolved = resolve_in_root(project_root, requested)
```

The guard:

1. Rejects absolute paths, drive-letter paths, UNC shares, `\\?\` device prefixes and
   `~`, rather than silently rewriting them. Silently mapping `/etc/passwd` to
   `<root>/etc/passwd` would make the agent believe it read a file it never read.
2. Joins onto the resolved root and calls `Path.resolve()`, which collapses symlinks.
3. Tests containment on the **resolved** path, so a symlink pointing outside the tree
   is refused.
4. Compares case-insensitively on Windows, because the filesystem does.

A path that climbs out and back in (`sub/../notes.txt`) is allowed — only the endpoint
matters. Traversal, symlink escape, and a sibling directory sharing a name prefix
(`/tmp/abc` vs `/tmp/abc-evil`) are all refused with a message naming the path.

The tests covering this are adversarial on purpose; they are the boundary.

## Filesystem

**`list_dir`** — entries with type and size; skips `.git`, `node_modules`, `__pycache__`
and friends. Marks artifacts so the UI can highlight them.

**`read_file`** — text, and PDFs via `pypdf` text extraction. Large files are windowed:
the result reports `total_lines`, `has_more` and `next_start_line` so the model can
continue instead of guessing. Content is returned with line numbers so a following
`edit_file` can quote text exactly. Binary files are refused with the size rather than
dumped into the context.

**`search_files`** — uses `ripgrep` when available (respects ignore files, much faster),
falling back to a pure-Python walk so it works on a machine with no extra binaries.
Paths are re-anchored to the project root, because `rg` reports paths relative to the
directory it was given — the cause of an early "wrong file" bug.

**`write_file`** — creates parent directories; reports `created` vs `updated` with a
line delta.

**`edit_file`** — exact string replacement that **requires uniqueness** unless
`replace_all` is set. An ambiguous match is refused and the file is left untouched, which
is what stops a plausible-looking edit from corrupting the wrong part of a file.

## Web

`web_fetch` reads one URL and returns the page as text. The calls it makes look
ordinary — a documentation page, a changelog, an error string — so the feature is
what it refuses:

- **Only `http` and `https`.** `file:///etc/passwd` is not a URL this tool knows
  about.
- **Only public addresses**, and the *resolved address* is what is checked rather
  than the name. So `localhost`, a private IP, and a harmless-looking domain that
  someone has pointed at `10.0.0.1` are refused alike — as are
  `169.254.169.254`, which is the metadata endpoint on a cloud host, and
  `127.0.0.1:8765`, which is this application's own API.
- **Redirects are followed by hand**, one hop at a time, with every hop checked
  again. `follow_redirects=True` would make the address check advisory, and a public
  URL that 302s to the local network is exactly the way in.
- **Only text.** A PDF or an image is reported rather than fetched and guessed at,
  and a body over 2 MB is cut — the model is told it was cut, and the returned text
  is capped again at `_WEB_FETCH_CHARS`.
- **URLs carrying a username or password are refused**, because the approval prompt
  shows the URL, and a credential in one would read as an ordinary address.

Approval is `ask` rather than `never`. Reading a page is harmless; *requesting* one
is not — the URL is the part of a request that can carry project data out, and it is
the part the prompt shows. `run_shell` has always been gated for the same reason,
and a project that trusts this tool stops being asked.

Not covered: the address is validated and then the connection is made by hostname,
so a name server that answers differently in between could still land on a private
address. Pinning the validated address would close that and break virtual hosting
and TLS verification. This is a single-user tool on the user's own machine.

**`web_search`** finds candidates when there is no URL to fetch: it returns a handful
of results, each a title, a URL and a snippet. The snippet is not the page and a
result is not evidence — the agent is told to read the promising ones with
`web_fetch` before relying on them, and to search only after the project's own files
and skills have been tried.

It is a **scrape, and it says so.** There is no key-free search API, so the query goes
to DuckDuckGo's no-JavaScript HTML endpoint and the results are read out of the
markup. DuckDuckGo rate-limits that: measured from one machine, the first query
returned ten results, the next four came back as `202 Accepted` with a *"complete the
following challenge"* CAPTCHA, and a query several minutes later worked again. So the
tool has three distinct outcomes, and keeping them apart is most of the design:

| Outcome | What the agent is told |
|---|---|
| results | the list, with the note that a snippet is not the page |
| **blocked** | a bot challenge, not results — do not conclude that nothing exists |
| **unreadable** | the page was not a result list — the markup changed, not an empty search |

The last two are refusals on purpose. "Nothing matched" is a claim about the world,
and neither of these is evidence for it. The endpoint also wraps every link in a
redirect of its own (`//duckduckgo.com/l/?uddg=…`), which is unwrapped here so the
model is given the address it will actually fetch; a result that is not an `http(s)`
address is dropped rather than offered as a dead end.

The request itself goes through the same validated path as `web_fetch` — the address
checks, the manual redirects, the size and content-type limits live in one place —
and it asks for approval for the same reason: the query is what leaves the machine,
and the query is what the prompt shows. A sub-agent cannot call either tool: a child
has a fresh approval broker and nobody watching it, so an `ask` tool inside one would
wait forever.

## Execution

**`run_python`** — writes the script into the project (so relative paths behave as the
user expects), runs it, then removes it. It prefers the project's own interpreter once
one exists, which is what makes `install_packages` useful, and reports `isolated` and
`interpreter` in the result so a missing import is diagnosable without another round trip.

**`run_shell`** — `cmd.exe` on Windows, `/bin/sh` elsewhere. A real shell, so pipes and
built-ins work. The command string is handed to the platform's own shell rather than
wrapped into a `cmd.exe /s /c` argv by hand: `/s` drops the first quote of the command
and the last quote anywhere on the line, so a command whose first token is a quoted
path — `"C:\Program Files\Python\python.exe" script.py` — would reach the shell with a
stray quote on the executable and fail. `run_background` starts its jobs the same way.

Both:

- run with `cwd` inside the project root;
- enforce a timeout (max 300 s) and kill the whole **process group** on expiry, so a
  spawned child cannot outlive the call;
- truncate output keeping both head and tail, since the first failure is at the top and
  the traceback is at the bottom;
- state truncation explicitly, so the model does not reason from a partial log as if it
  were whole.

Cancellation is real: pressing stop (or barging in) kills the subprocess tree.

### Background jobs

A command that takes four minutes does not belong inside a turn: the turn is blocked
for those four minutes, the user hears nothing, and the model cannot tell "not
finished yet" from "broken". `run_background` starts it and returns a name;
`job_output` reads it — waiting a bounded time if asked, so one step replaces a
polling loop — and `job_kill` stops it.

Three properties make it safe to leave running:

- **A job belongs to its conversation.** The registry is owned by the `Session`, not
  by a turn, so a build started in one turn is there three turns later; and
  `Session.close()` kills everything it started. A background process that outlives
  the window it came from is one the user cannot see or stop.
- **Output goes to a file, not a pipe.** Nothing has to be drained for the command
  to make progress, so a job that prints for an hour cannot fill a pipe buffer and
  block on a reader that stopped reading.
- **The file is read from the end**, capped, and what comes back says it was cut:
  for something that has been running a while, the newest lines are the ones that
  say where it got to.

The registry keeps the newest 20 jobs and drops the oldest *finished* one, so a loop
that starts jobs forever does not grow a list forever. `run_background` is `ask` for
the same reason `run_shell` is — it is the same act, deferred — and the two readers
are `never`, since reading what already ran, or stopping the agent's own job, is not
a new thing to approve. None of the three reaches a sub-agent: the jobs belong to the
parent, and a child has nobody to answer an approval prompt.

## Artifacts

**`make_pdf`** — structured blocks: `heading`, `paragraph`, `bullet_list`, `table`,
`image`, `page_break`, `spacer`. Text is sanitised to Latin-1 because ReportLab's
built-in fonts cannot render curly quotes or em dashes, which would otherwise appear as
black boxes.

**`make_spreadsheet`** — multiple sheets, optional headers, frozen header row, auto-width
columns. Values that openpyxl cannot serialise are stringified rather than raising deep
inside the save call.

**`make_chart`** — `line`, `bar`, `hbar`, `pie`, `scatter`, `area`, `hist` to PNG or SVG,
using the `Agg` backend so no display is needed. Saved charts can be embedded into a PDF
with an `image` block.

These exist so the common cases are one tool call with a typed schema instead of
hand-rolled boilerplate on every request. `run_python` remains the escape hatch.

## Documents

**`read_file`** — reads text, CSV, JSON, Markdown, PDF, and Office documents. A
`.docx`, `.xlsx`, `.pptx` or `.odt` is turned into text by the built-in extractor
(headings, paragraphs, lists and tables as tab-separated rows), so no converter has to
be installed to read one.

**`convert_document`** — two engines, chosen per call.

The **built-in** engine needs nothing installed. It reads `docx`, `xlsm`, `xlsx`,
`pptx`, `odt`, `ods`, `odp`, `pdf`, `csv`, `tsv`, `json`, `html`, `md` and plain text,
and writes `pdf`, `txt`, `csv`, `html` and `xlsx`. It carries *content*, not layout:
fonts, colours, columns, headers and images are dropped. The pipeline is deliberately
two halves — every source is extracted into one small neutral document model
(headings, paragraphs, list items, code, tables) and every target is rendered from it
— so a new format is one function on one side rather than a converter per pair.

**LibreOffice** is used when it is installed: for the targets the built-in engine
cannot write (`docx`, `odt`, `rtf`, `ods`, `odp`, `pptx`, `png`, `jpg`), and as a
fallback when the built-in parser fails on a file. It keeps layout, so it is the better
answer for a document whose appearance matters.

`backend` selects the engine: `auto` (default) prefers the built-in converter and falls
back to LibreOffice; `builtin` guarantees no external program is run; `libreoffice`
requires it. The result reports which one ran in `data.backend`.

Two operational details matter on the LibreOffice path:

- **A private user profile per run.** LibreOffice refuses to start a second instance
  against one profile; a shared profile is the classic cause of "conversion hangs
  forever".
- **Output is staged.** LibreOffice *ignores the requested output filename* and always
  writes `<source stem>.<ext>` into `--outdir`. The converter therefore works in a
  private staging directory, finds what was actually written, and moves it into place.
  Without this, `output="reports/summary.pdf"` silently produced nothing.

LibreOffice is discovered automatically (PATH, then the usual install locations), or set
`soffice_path` in `.surtitle.json` / `SURTITLE_SOFFICE`.

## Capability acquisition

**`search_packages`** — resolves an exact name through the PyPI JSON API (cheap and
reliable); otherwise queries the search page. An unreachable index returns no results
rather than an error, because a direct install may still work.

**`install_packages`** — installs into the project's isolated environment.

```
<project>/.surtitle/
  venv/                 the environment (deleting the project deletes it)
  requirements.txt      every requirement you have approved
```

Why isolated: installing into the application's own environment would corrupt the
shipped runtime, and on Windows would need admin rights to write there at all.

Why gated: a package's build backend runs at install time, so installing is arbitrary
third-party code execution.

Validation is strict. A requirement must be a plain distribution name with at most
version specifiers and extras. Anything starting with `-` (which would become a
package-manager flag such as `--index-url`), a URL, a VCS reference, or a filesystem path
is refused. That is what stops a crafted "package name" from redirecting an install to an
untrusted host.

Approvals are remembered per project, so approving a set once means a fresh clone
installs it silently. A **new** package always asks.

**`environment_info`** — reports whether the project has its own environment and which
packages are installed.

## Version control

Three tools, two of which never change anything. The policy they serve — ask when
the work is done, never commit unasked — is in the system prompt; see
[`VCS.md`](VCS.md) for how git and svn are provided on a machine that has neither.

**`vcs_status`** — the working copy: git or svn, branch or revision, what is
uncommitted, ahead and behind, the remote, and which git and svn are installed
here. Read-only, and cheap enough to call whenever the state matters.

**`vcs_guide`** — the correct usage notes for git or svn: the model of each system,
the commands that matter, how to undo at each level of destruction, the rules about
what is never committed, and the commit-message levels to offer you. Deliberately a
tool result rather than prompt text, because it is long and only needed once the
agent is actually about to touch history.

**`vcs_commit`** — stages and commits, optionally pushes. Approval-gated, and the
only tool that writes to your repository. It refuses an empty change rather than
leaving an empty commit, refuses a body when you asked for a one-line message, and
keeps `.surtitle/` out of the commit — reporting it as excluded rather than
including it silently.

```
Agent: <say>The parser change is working and the tests pass. Should I add, commit
       and push it — and do you want the message as one line, a summary, or
       detailed?</say>
You:   "Summary, and push it."
       vcs_commit(message="Fix the CSV parser dropping quoted newlines\n\n...",
                  detail="summary", push=true)
```

## MCP

Any [Model Context Protocol](https://modelcontextprotocol.io) server configured in
`.surtitle.json` contributes tools named `<namespace>__<tool>`.

```json
{
  "mcp_servers": [
    { "name": "fusion", "command": "fusion-mcp", "args": ["--stdio"],
      "namespace": "fusion360", "trusted_tools": ["get_design"] }
  ]
}
```

Behaviour:

- **Namespaced** so two servers cannot collide on a name like `create`.
- **Approval-gated by default**, because a remote server is exactly where a silent side
  effect is least visible. `trusted_tools` opts specific ones out.
- **Independently started.** A server that fails to start costs only its own tools; the
  failure reason is reported in the UI.
- **Stderr is drained.** A chatty server would otherwise fill its pipe and block forever.

Implemented over stdio JSON-RPC (`initialize`, `notifications/initialized`, `tools/list`,
`tools/call`) rather than the official SDK, whose dependency chain requires a Rust build
and would defeat the no-toolchain Windows release. Timeouts, process-group kills and
shutdown follow the same patterns as the rest of the app.

## The goal

`goal_write` records what the conversation is *for*, in one sentence, and whether it
has been reached. It is not the plan: the plan is the list of steps, the goal is the
thing the steps are for, and it is the broader of the two — every plan item can be
ticked while the thing the user actually asked for is still not done, which is the
case a plan cannot catch.

It is per **conversation** — two columns on the session row — and it is given to the
agent at the start of every turn, above the plan, in the same transient notes block
the plan lives in. Losing the conversation cannot lose it. It is rendered above the
plan in the Plan tab, and echoed to the browser as its own `goal` event when it
changes, for the same reason the plan is: it is state the user watches, so it is
sent from the record rather than read out of a tool result.

It also drives continuation, which is what it is really for. A turn that ends with
the goal still standing — and did some work — is asked once to carry on, the same way
a turn that ends with plan items open is. The plan is named when both are
outstanding, because "you left this item open" is a better instruction than "you have
not finished". A goal marked **achieved** stops driving anything: what was wanted is
kept for the record, and a finished conversation is not nudged onward.

`goal_write` takes no approval. It changes nothing outside the conversation, exactly
like the plan, and it is absent from a sub-agent's registry for the same reason: a
child has no conversation of its own for a goal to belong to.

## The plan

`todo_write` records what the agent intends to do and how far it has got, so a long
piece of work can be watched rather than waited on. It is the one tool that changes
nothing outside Surtitle's own database: no file, no command, so no approval.

The whole list is sent on every call and replaces the previous plan — a merge would
leave items the agent deliberately dropped still showing as outstanding work. Exactly
one item may be `in_progress`. Statuses outside `pending` / `in_progress` /
`completed` are stored as `pending` rather than rejected: a plan that failed to save
because the model invented a fourth value would be worse than one that reads as
not-started.

The plan belongs to the **conversation**, not the turn, which is the point of it: an
agent that stops with three items pending has not finished, and this is what says so
after a reload. It is rendered in the Plan tab of the right-hand panel, and returned
with `GET /api/sessions/{id}` as `todos`.

Stored reasoning is the other half of the same picture, and is deliberately *not*
searchable: it is a private brainstorm full of self-corrections and of guesses the
model declined to act on, and returning one later as "what you did before" would put
a discarded idea back in front of the model as though it were a finding.

## Sub-agents

`subagent` hands one self-contained piece of reading to a second agent and returns
what it found. The reason is context: the reading that answers a question is usually
several times larger than the answer, and in the parent's transcript it would stay
there for the rest of the conversation. A child spends its own context on it and
hands back the answer, plus the files it read so the parent can go straight to the
source.

The child is a second `AgentLoop` with the same project, the same model and none of
the conversation: no history, no store, and `ToolRegistry.read_only()` for tools —
the `never`/non-mutating rows of the table above, minus anything that needs a
conversation (`todo_write`, `search_history`) and minus `subagent` itself, so one
level of delegation rather than a tree. `skill` is kept: a child following the
project's written-down procedure is the point of having one. It also gets a fresh
`RepeatCallGuard`: the parent having read a file is no reason for the child to be
refused the same file.

What the child produces is an answer and a list of files; a child that finishes
without one is reported to the parent as a failed call, with the reason (`no_answer`,
`step_limit`, `cancelled`), rather than as an empty finding. Its budget is its own
and smaller — `_SUBAGENT_MAX_STEPS`, 24 — because a delegation that needs two hundred
steps is the task, and should have been done as one.

Nothing the child does is stored, so the conversation records one call and one
result. Its steps are re-emitted on the parent's side channel marked with
`subagent`, and numbered after the parent's round: the parent is blocked inside a
tool call while a child runs, so without that offset a long investigation would
advance no steps at all and the turn would fall silent — the failure the progress
narration exists to prevent. The session says one line naming what is being looked
into, and only if the agent has not already spoken; the child is never spoken
itself, and its reasoning is discarded.

## Skills

A skill is a procedure written down for the agent — how releases are cut here, what
the house style is, which checks this project wants run before a commit. It is a
directory holding a `SKILL.md`, in the project's `skills/` or in the user's own
`$SURTITLE_HOME/skills/`. A project skill wins a name it shares with a personal one:
it is the more specific statement of how work is done *here*.

The file may open with front matter naming it and describing it in a line:

```markdown
---
name: release-notes
description: How a changelog entry is written in this project.
---

Read `CHANGELOG.md`...
```

Front matter is optional — the directory names the skill and its first real sentence
describes it — and it is deliberately not parsed as YAML. Two strings do not justify
a dependency, and a project should not need one to describe a procedure.

What the agent is given each turn is the **catalogue**: every skill's name and that
one line, in the same transient notes block the plan lives in. What it is not given
is the bodies. That split is the whole design. Putting every procedure in the system
prompt spends the context of every turn on the ones nobody is using; naming them
without a way to read them leaves the agent guessing at what it cannot see. So the
catalogue is short, and `skill` reads one in full when the task matches — the body
alone, without the front matter, plus the names of any files beside it (a template, a
script) that `read_file` can then reach.

Two rules, both about the name being the only thing the model supplies:

- **A name is a name.** It is matched against a pattern rather than treated as a
  path, and the directory it resolves to is then proved to be inside the skills root.
  A skill called `../../.ssh` is not a skill. A name that fails either check is not
  listed either, because listing something the agent can never load is worse than
  saying nothing.
- **A body has a limit**, 20,000 characters, and a body that hits it says so. A skill
  silently halved reads as the whole procedure, and the file is on disk and readable.

The tool only reads. It does not do what the skill describes, and it takes no
approval for the same reason the plan does not: it changes nothing. Absent a
`skills/` directory the section is absent, and the agent is told nothing about
skills at all.

## Adding a tool

```python
_MY_TOOL = Tool(
    name="my_tool",
    description="What it does, and when to use it instead of another tool.",
    parameters={
        "type": "object",
        "properties": {"path": _string("Relative path.")},
        "required": ["path"],
    },
    handler=my_handler,
    approval="ask",          # decide explicitly
    summary="Do the thing",  # shown in the approval prompt
    mutating=True,
)
```

Then add it to `default_tool_list()`. Handler signature:
`handler(ctx: ToolContext, **arguments) -> ToolResult`, sync or async. Return failures as
`ToolResult(ok=False, error=...)` rather than raising: tool failures are information the
model should see and adapt to.
