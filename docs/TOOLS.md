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
| `write_file`, `edit_file` | ask | yes |
| `run_python`, `run_shell` | ask | yes |
| `make_pdf`, `make_spreadsheet`, `make_chart` | ask | yes |
| `convert_document` | ask | yes |
| `install_packages` | ask (per new package) | yes |
| `<server>__<tool>` (MCP) | ask, unless `trusted_tools` | yes |

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

## Execution

**`run_python`** — writes the script into the project (so relative paths behave as the
user expects), runs it, then removes it. It prefers the project's own interpreter once
one exists, which is what makes `install_packages` useful, and reports `isolated` and
`interpreter` in the result so a missing import is diagnosable without another round trip.

**`run_shell`** — `cmd.exe` on Windows, `/bin/sh` elsewhere. A real shell, so pipes and
built-ins work.

Both:

- run with `cwd` inside the project root;
- enforce a timeout (max 300 s) and kill the whole **process group** on expiry, so a
  spawned child cannot outlive the call;
- truncate output keeping both head and tail, since the first failure is at the top and
  the traceback is at the bottom;
- state truncation explicitly, so the model does not reason from a partial log as if it
  were whole.

Cancellation is real: pressing stop (or barging in) kills the subprocess tree.

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
