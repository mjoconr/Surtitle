---
name: state-note
description: How a state note under docs/state/items/ is written, and what the numbers mean.
---

# Recording something in the state notes

Everything this project is carrying lives as one file in `docs/state/items/`, named
`NN-two-word-title.md`. `docs/CURRENT_STATE.md` is *rendered* from these files and is
never edited by hand — an edit there is lost the next time it is regenerated.

A note that is already there is **updated in place**: a question that has been settled
keeps its number and its file, and becomes `kind: done` with `status: closed`. Only
something the notes do not mention yet gets a new file, and then the number is the
next free one, zero-padded to two digits, with `kind: openq` and `status: open`.

Every note opens with three front-matter fields, and all three are required:

- `kind` — `openq` for a question nobody has settled, `done` for something closed.
- `order` — the same number as the filename, unpadded.
- `status` — `open` while it still matters, `closed` once it does not.

`template.md` beside this file is the shape to copy. Keep the body to the question
and what would settle it: these are read one at a time, not as a report.
