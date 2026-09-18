# Contributing

## Getting set up

```bash
git clone <this repo> && cd Surtitle
uv sync
uv run pytest
```

`uv` manages Python and dependencies. `pip install -e .` works too if you prefer, or
if you already have a suitable Python.

No API keys are needed: the default test suite is fully offline. Every DeepSeek and
Deepgram interaction is replayed through scripted fakes, so tests are deterministic
and free to run.

## Checks before opening a pull request

```bash
uv run ruff format src tests scripts
uv run ruff check src tests scripts
uv run pytest
```

CI runs exactly these on Linux, macOS and Windows across Python 3.11 and 3.13, so
anything platform-specific is worth checking locally on that platform.

## Test conventions

Markers:

| Marker | Meaning |
|---|---|
| *(default)* | Offline, deterministic, no network, no keys |
| `live` | Reaches the network (real PyPI install, real LibreOffice conversion) |

Document conversion is covered on both paths: the built-in engine by
`tests/test_document_native.py`, which needs nothing installed, and the LibreOffice
path by the `live` tests, which skip themselves when `soffice` is absent. A new format
is one extractor plus one renderer in `src/surtitle/tools/document_native.py`, not a
converter per source/target pair.

`addopts = "-m 'not live'"` keeps the default suite offline. Run the network subset
explicitly:

```bash
uv run pytest -m live
```

### What to test

Prefer asserting on **observable outcomes** over implementation details. A converter
test opens the PDF it produced; an install test imports the package in the environment
it was installed into. "The function returned `ok`" is much weaker than "the user can
open the file".

Three areas deserve adversarial tests rather than happy-path ones, because they are the
security boundary:

- **`tests/test_path_guard.py`** — traversal, symlink escape, absolute paths, Windows
  device prefixes, UNC shares, case folding, name-prefix siblings (`/tmp/abc` vs
  `/tmp/abc-evil`).
- **`tests/test_environment.py`** — requirement validation refusing flags, URLs and paths;
  approval memory; isolation.
- **`tests/test_server.py`** — that no endpoint ever returns a credential value.

When you fix a bug, add the test that would have caught it. Several existing tests exist
for exactly that reason and are worth reading before changing the code they cover — the
comments explain the failure mode.

## Design rules

**A secret never leaves the process toward the UI.** Credentials cross the wire only as
`{configured, source, writable}` — never a value, a suffix, or a length. If you add a
field to `SettingsStore.describe()`, it must not be a secret.

**Register an approval waiter before announcing the request.** See
`ApprovalBroker.register` / `decision`. Announcing first creates a race where a fast UI
answer arrives before anything is waiting, and the turn hangs.

**A tool failure is information, not an exception.** Return
`ToolResult(ok=False, error=...)` so the model can adapt. Only programmer errors should
propagate.

**New tools declare an approval policy.** The `Tool` dataclass requires it, so this is
enforced by construction. Decide deliberately; do not default to `never`.

**Confinement lives in one place.** Never build a project path by hand — always
`resolve_in_root`. A single bypass is a full escape.

**Comments explain *why*, not *what*.** Where a decision is non-obvious, or where a
subtle bug was fixed, say what would go wrong. Code that merely restates itself does not
need a comment.

## Adding a tool

See the end of [`docs/TOOLS.md`](docs/TOOLS.md) — the pattern is a `Tool` with a JSON Schema, a
handler returning `ToolResult`, and an approval policy, then a line in
`default_tool_list()`.

## Adding an MCP server

No code needed: it is configuration in `.surtitle.json`. If you are fixing the MCP
client itself, `tools/mcp.py` speaks stdio JSON-RPC directly; the tests spin up a real
subprocess server so the transport is exercised rather than mocked.

## Commits

Conventional commits, one logical change each:

```
feat(voice): stream TTS per sentence instead of per block
fix(session): register the approval waiter before announcing it
docs(windows): explain the SmartScreen prompt
test(tools): cover requirement strings that look like flags
```

## Packaging changes

`scripts/build_release.py` must produce an archive that runs with no system Python and
no network. If you add a dependency, verify the build still works on Windows, since
compiled wheels are platform-specific. CI builds and smoke-tests both platforms on every
pull request, including importing the app from the bundled runtime.

Cutting a release from that archive — version bumps, the changelog
section, tagging, and checking the published artifacts — is in
[`docs/RELEASING.md`](docs/RELEASING.md).

## Reporting issues

Include:

- your platform and how you launched the app (archive or source checkout);
- the output of `./scripts/run.sh doctor` (with keys redacted);
- the relevant lines from the log file, whose path `surtitle init --show` prints.

Never paste an API key into an issue.
