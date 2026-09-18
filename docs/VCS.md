# Version control

Surtitle's agent works on your files, and version control is how that work becomes
recoverable. So it can use git and svn properly, and it will always ask before it
saves anything with them.

## Getting the tools

A fresh Windows machine has neither `git` nor `svn`, and installing either normally
needs an administrator. The app can fetch portable builds of both into its own
data folder instead:

- **From the tray**: right-click the icon → **Install git and svn…**
- **From a terminal**: `surtitle tools install`
- **From the app**: the same request goes to the server, which runs it in the
  background — the menu row shows progress and nothing needs restarting.

They unpack into:

```
%LOCALAPPDATA%\Surtitle\tools\git       (MinGit 2.51.0, cmd\git.exe)
%LOCALAPPDATA%\Surtitle\tools\svn       (Apache Subversion 1.14.5, bin\svn.exe)
```

That folder is outside every project, so an update never re-downloads them and
deleting a project never removes them. Nothing is installed system-wide, nothing
is added to your own `PATH`, and no elevation is requested — the tools are put on
the `PATH` of the processes Surtitle itself starts, and nowhere else.

Both downloads are verified twice: the archive must match the SHA-256 pinned in
`surtitle/vcs/provision.py`, and the unpacked binary must then run and report the
version it is supposed to be. A version is pinned by hand rather than tracking
"latest", because a floating URL makes the checksum meaningless.

`surtitle tools status` reports what is installed and where it came from, and
exits non-zero when either is missing, so a script can branch on it. The same
information is in `surtitle doctor`.

**Windows only, deliberately.** Portable builds are published for Windows; on
macOS and Linux your package manager is the right answer, and Surtitle uses
whatever `git` and `svn` it finds on `PATH`. `tools install` says so rather than
quietly dropping a second copy of git somewhere your system cannot see it.

## What the agent is told

Two layers, because they answer different questions.

**The standing rule** is in the system prompt, always. When a piece of work is
done — meaning the idea mostly works or is actually finished, not merely that it
started or stopped — the agent asks you two things in one short question:

1. whether to **add, commit and push**;
2. how detailed the commit message should be: **one line**, **a summary**, or
   **detailed**.

It then does exactly what you asked. It never commits, tags or pushes unasked, and
an earlier yes does not cover later work: each finished piece is its own question.

**The facts** are added per session: which copy of git and svn this machine is
using, where it is, and whether the project is a working copy at all — with its
branch, what is uncommitted, and how far ahead or behind it is. Those are read once
per conversation rather than per turn, because finding them means running commands.

## The tools

| Tool | Approval | What it does |
|---|---|---|
| `vcs_status` | never | Reports the working copy: system, branch or revision, what is uncommitted, ahead/behind, remote, and which tools are installed. Use it instead of guessing from file names. |
| `vcs_guide` | never | The usage notes for git or svn — read on demand, not kept resident. |
| `vcs_commit` | **ask** | Stages and commits, optionally pushes. The one mutating step. |

`vcs_commit` takes the message, the level of detail you chose, optional specific
paths, and whether to push. It is approval-gated like every other tool that changes
something, and it enforces three things rather than trusting the caller:

- an **empty change is refused**, not committed — an empty commit looks like
  success in a tool result and leaves a puzzling entry in your history;
- a **body is refused when you asked for one line**, because that is the agent not
  having listened;
- **`.surtitle/` is never committed** — it holds the project notebook, the isolated
  environment and uploads, which are the agent's working files rather than your
  work — and it is reported as excluded rather than silently included.

## The guide

`vcs_guide` is a page of correct usage that the agent reads before its first
version-control action in a conversation. It is a tool result rather than prompt
text: it is long, it is only needed at that moment, and loading it into every
conversation would spend your money to no purpose.

It covers, for each system:

- **the model** — working tree, index and `HEAD` for git; a working copy with one
  central repository and no local history for svn;
- **reading state before touching anything**, and doing it before every commit;
- **undoing at each level of destruction**, from `git revert` down to
  `reset --hard`, and why a command that discards work is never run without asking;
- **the conflicts and detached-HEAD cases**, and the rule that a merge you do not
  understand is reported rather than invented;
- **what is never committed** — secrets, generated output, environments, and
  `.surtitle/`.

## What it will not do

It will not commit, tag or push without being asked. It will not force-push or
rewrite published history. It will not create a repository in a folder that has
none without asking first — whether your project is under version control is your
decision, not the agent's.
