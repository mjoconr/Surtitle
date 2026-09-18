"""How to use git and svn correctly, in the words the agent gets to read.

A model's idea of version control is mostly right and reliably wrong in the same
few places: it stages things nobody asked it to, it writes a commit subject where
a description was wanted, it reaches for ``git reset --hard`` or ``svn revert``
when a reversible command would do, and it treats an svn commit as local when it
is immediate and permanent. This text exists to close that gap in one read,
instead of discovering it one mistake at a time in somebody's repository.

The guide is a *tool result*, not part of the prompt: it is long, it is only
needed once the agent is actually about to touch version control, and loading it
into every conversation would spend the user's money to no purpose.
"""

from __future__ import annotations

__all__ = ["DETAIL_LEVELS", "GIT_GUIDE", "SVN_GUIDE", "detail_menu", "guide_for"]

# The commit-message levels the agent must offer the user. Defined here because
# two places need the same words: the guide the agent reads, and the prompt that
# tells it to ask. A third copy would drift.
DETAIL_LEVELS: dict[str, str] = {
    "one-line": "a subject line only — for a small, obvious change",
    "summary": "a subject plus a few bullets: what changed and why",
    "detailed": (
        "a subject, a body, and the reasoning — what was wrong, what you chose, "
        "what you rejected and what it affects"
    ),
}


def detail_menu() -> str:
    """The three levels, rendered as a list the agent can put to the user."""
    return "; ".join(f"{name} — {text}" for name, text in DETAIL_LEVELS.items())


GIT_GUIDE = """\
# git, as this environment expects it

## The model

Three places hold content, and confusing them is the source of most mistakes:

- the **working tree** — the files on disk;
- the **index** (staging area) — what the next commit will contain;
- **HEAD** — the commit currently checked out.

`git add` copies working-tree content into the index. `git commit` turns the index
into a commit. So a commit contains what you staged, not what is on disk, and
`git diff` (unstaged changes) and `git diff --staged` (what a commit would take)
answer different questions. A **branch** is a movable name for a commit; `main` is
the shared line, and everything you do on a branch is local until you push.

## Reading state before you touch anything

    git status --short --branch        # branch, changed files, ahead/behind
    git diff                           # unstaged changes
    git diff --staged                  # exactly what a commit would record
    git log --oneline -20              # recent history
    git log --follow -- <path>         # history of one file across renames
    git blame <path>                   # who last touched each line, and when
    git show <rev>:<path>              # the file as of a revision
    git log -S"<string>"               # when a string appeared or disappeared

Read `git status` and `git diff --staged` before every commit. That is the one
habit that prevents nearly all of the accidents below.

## Doing the work

    git switch -c <branch>             # start a branch
    git switch <branch>                # change branch
    git add <path>...                  # stage specific files
    git add -A                         # stage everything, including deletions
    git restore <path>                 # discard unstaged changes to a file
    git restore --staged <path>        # unstage, keeping the change on disk
    git commit -m "<subject>"          # commit what is staged
    git push                           # publish the current branch

Prefer `git add <paths>` to `git add -A` when you know which files changed: `-A`
is how build output, an editor's swap file or a stray database ends up in
history. Add a path when you can name it.

## Undoing, in order of how much it destroys

    git revert <rev>                   # a new commit that undoes an old one
    git restore <path>                 # discard local changes to a file
    git restore --staged <path>        # unstage only
    git reset --soft HEAD~1            # uncommit, keep changes staged
    git reset --hard <rev>             # DESTROYS uncommitted work

Prefer `revert` on anything that has been pushed; it is honest history and needs
no permission. `reset --hard`, `push --force`, `clean -fd`, `checkout -- .` and
`branch -D` all discard work irreversibly — never run one without asking the user
first, and never at all on a branch somebody else may have pulled.

## Rules for this environment

- **Never rewrite published history.** No `push --force` (or `--force-with-lease`)
  unless the user asked for that exact thing, and never on `main`.
- **Never commit secrets.** API keys, tokens, `.env` files, credentials, private
  keys. If a secret is already tracked, say so rather than committing around it.
  Naming where a secret lives is fine; committing its value is not.
- **Never commit generated output or environments** — virtualenvs, `node_modules`,
  build directories, `__pycache__`, model files.
- **Never commit `.surtitle/`.** It is Surtitle's own per-project state (notes,
  environment, uploads); it is not part of the user's work. If a repository has no
  `.gitignore` entry for it and you are about to make the first commit, add one.
- A commit is a claim about *why* a change exists. The diff already says which
  lines moved; the message should say what was wrong and what the change does
  about it. One subject line under about 72 characters, imperative mood
  ("Add retry to the uploader", not "Added" or "Adds").

## When it goes wrong

- **A conflict** (`git status` shows `UU`): open the file, resolve it by hand,
  `git add` it, then `git commit`. Never invent a resolution you do not
  understand — report which files conflicted and ask.
- **Detached HEAD** (`git status` says "HEAD detached"): commits here are not on a
  branch. `git switch -c <name>` before committing anything.
- **Uncommitted work you did not create**: leave it alone. Do not stash, commit or
  revert changes you did not make; ask whose they are.
"""


SVN_GUIDE = """\
# svn, as this environment expects it

## The model

Subversion has one central repository and no local history. Your directory is a
**working copy**, which remembers the revision it came from. There is no staging
area: `svn commit` sends the changes you selected straight to the server, and
everyone sees them immediately. That single fact drives most of the differences
from git:

- there is no local commit to fall back on, so `svn revert` destroys work with no
  recovery;
- a commit that someone else's commit has overtaken fails until you `svn update`;
- nothing is committed unless it is *versioned*: a new file needs `svn add` first.

## Reading state before you touch anything

    svn info                           # URL, revision, working copy root
    svn status                         # local changes; `?` means unversioned
    svn status --show-updates          # ...and what is out of date on the server
    svn diff                           # local modifications
    svn diff -r PREV                   # what the last commit changed
    svn log -l 20                      # recent history
    svn log -v <path>                  # history of one path
    svn blame <path>                   # who last touched each line

Read `svn status` and `svn diff` before every commit.

## Doing the work

    svn update                         # bring the working copy up to date
    svn add <path>                     # version a new file or directory
    svn delete <path>                  # schedule a file for deletion
    svn move <from> <to>               # rename, keeping history
    svn commit -m "<subject>"          # send changes to the server
    svn commit -m "<subject>" <paths>  # ...only these paths
    svn revert <path>                  # DISCARD local changes, unrecoverably
    svn resolved <path>                # mark a conflict as resolved

`svn add` is the step people forget: a commit silently omits unversioned files,
so new work looks committed when it is not. Check `svn status` for `?` lines
before you commit.

## Conflicts

    svn update                         # stops with a C on the conflicting path
    # ...edit the file, choosing what is correct
    svn resolved <path>
    svn commit -m "<subject>"

`svn resolve --accept=working` declares your file the answer. Only use it after
you have actually edited the file — it is not a way to make the warning go away.
Never invent a merge you do not understand; report the conflicting paths instead.

## Rules for this environment

- **`svn revert` is irreversible** — there is no local commit to recover from.
  Never run it without asking, and never on files you did not change.
- **A commit is public immediately.** Treat `svn commit` exactly as you treat
  `git push`: ask before doing it, every time.
- **`svn update` before you commit**, or the server may reject the commit as out
  of date — and an update can conflict, which is better discovered before the
  commit than after.
- **Never commit secrets or generated output**, and never `.surtitle/` (Surtitle's
  own per-project state). If the working copy has no `svn:ignore` for it, say so.
- A commit message is a claim about *why* the change exists. The diff already says
  which lines moved.
"""


def guide_for(system: str) -> str:
    """The guide for ``git``, ``svn``, or both when the caller does not know."""
    chosen = (system or "").strip().lower()
    if chosen.startswith("git"):
        return GIT_GUIDE
    if chosen.startswith("svn"):
        return SVN_GUIDE
    return f"{GIT_GUIDE}\n\n-----\n\n{SVN_GUIDE}"
