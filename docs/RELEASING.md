# Releasing

A release is a tag. Pushing `vX.Y.Z` starts `.github/workflows/release.yml`, which
verifies the tree, builds one archive per platform, and publishes them with a
`SHA256SUMS.txt` on a GitHub release. Users then get it by the tray's update, or by
extracting the archive.

Two rules hold everything together:

- **The tag names the version, and the version lives in three files.** CI refuses a
  tag that disagrees with `pyproject.toml`, and a test refuses a version that
  disagrees with the package or has no changelog section.
- **A published tag is never moved.** The assets are what people downloaded; a tag
  that no longer describes them is worse than an extra patch release.

## Before you start

`main` should be green and the working tree clean.

```bash
uv lock --check
uv run --frozen ruff format --check src tests scripts
uv run --frozen ruff check src tests scripts
uv run --frozen pytest -q
```

Nothing here runs Windows, so a Windows-only mistake — a test that assumes a POSIX
runner, a path that only works on one platform — is invisible until CI sees it.
That is expected, and it is why the tag is pushed *after* CI, not with it.

## 1. Choose the version

Semantic versioning, and the audience is a user deciding whether to update:

| Change | Version |
|---|---|
| Something shipped behaves wrongly and now does not | patch |
| Something a user can now do that they could not | minor |
| Anything that changes how an existing install is set up | minor, and say so loudly |

A fix that only makes the tests honest, or only edits documentation, still needs a
release if the *shipped* code changed; it does not if nothing a user runs changed.

## 2. Bump it in three places

- `pyproject.toml` — `[project] version`
- `src/surtitle/__init__.py` — `__version__`
- `CHANGELOG.md` — replace `## [Unreleased]` with `## [x.y.z] - YYYY-MM-DD`

`tests/test_build_release.py::TestVersionConsistency` fails if those disagree or if
the changelog has no section for the version, so this is enforced rather than
remembered. The `Unreleased` heading stays at the top for the next change.

Then tell the lock, which is the fourth place the version is written:

```bash
uv lock
```

`uv.lock` records the version of the editable project. It is derived rather than
hand-edited, but it goes stale the moment `pyproject.toml` moves, and nothing else
refreshes it: every command in this document runs with `--frozen`, which means
"do not touch the lockfile". So the release pipeline never rewrites it, and step
3's `git add -A` catches a change that never appears. That is how it came to say
`0.1.0` against a `0.5.2` project, five releases on.

`--frozen` is still right everywhere else — a release must not re-resolve
dependencies — but it does not *assert* the lock is current. `uv lock --check` in
the pre-flight above does, and it is the difference between a rule and a habit.

Write the changelog entry for someone deciding whether to update: what was wrong,
what it means for them, and what they have to do — not which files moved. The
workflow also generates notes from the commit log, so the entry is for the
repository; the two are allowed to differ.

## 3. Commit and push `main`

```bash
git add -A
git commit -m "chore(release): x.y.z"
git push origin main
```

Then wait for CI to finish before tagging:

```bash
gh run list --limit 3
gh run watch <id>            # lint, both OS test jobs, wheels, both archive builds
```

## 4. Tag

```bash
git tag -a vX.Y.Z -m "Surtitle X.Y.Z

<one or two sentences a user would understand>"
git push origin vX.Y.Z
```

`release.yml` then runs in three stages:

1. **Verify** — lint, format check, the full suite on ubuntu, and that the tag
   matches `pyproject.toml`.
2. **Build** — `macos-latest` and `windows-latest`, each running
   `scripts/build_release.py`, which smoke-tests the archive it just made.
3. **Publish** — generates `SHA256SUMS.txt` over the artifacts and creates the
   GitHub release with them attached.

For a look before announcing, run the workflow by hand
(`gh workflow run release.yml -f draft=true`); the default for a manual dispatch is
a draft release.

## 5. Verify what was published, not the run

A green run is not the evidence; the artifacts are.

```bash
gh release view vX.Y.Z --json url,assets --jq '.url, (.assets[] | "\(.name)  \(.size)")'

mkdir -p /tmp/rel && gh release download vX.Y.Z --pattern SHA256SUMS.txt --dir /tmp/rel
cat /tmp/rel/SHA256SUMS.txt

curl -sSL -o /tmp/w.zip \
  "https://github.com/mjoconr/Surtitle/releases/download/vX.Y.Z/surtitle-X.Y.Z-win32-AMD64.zip"
shasum -a 256 /tmp/w.zip     # must equal the line in SHA256SUMS.txt
```

Then confirm the archive carries *this release's* change rather than a plausible
tree:

```bash
python3 -c "import json,zipfile; z=zipfile.ZipFile('/tmp/w.zip'); \
print(json.loads(z.read([n for n in z.namelist() if n.endswith('BUILD-INFO.json')][0])))"
```

`BUILD-INFO.json` must name the version, platform and machine, and the file you
changed must be inside the archive — grep it. A release whose assets do not contain
the fix is the failure this step exists to catch.

`scripts/build_release.py --verify-only <archive>` re-runs the build's own verifier
against a download, which is also how a user can check one they extracted.

## 6. When the release run fails

**Failed before publishing** (the verify or build stage): fix on `main`, then move
the tag to the fixed commit.

```bash
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z
git tag -a vX.Y.Z -m "..."     # on the fixed commit
git push origin vX.Y.Z
```

**Already published**: do not move the tag. Cut `X.Y.(Z+1)` with the fix. This is
why 0.5.1 and 0.5.2 exist — their predecessors' publish stages had succeeded, so
the corrections went into a new patch rather than under an existing tag.

## 7. What one machine cannot check

These are only reachable on Windows, so a release should say so if it touched them,
and someone should try them:

- the native folder chooser (**System…** in the new-project dialog);
- the tray: notification balloons, balloon-less fallbacks, the Start Menu entry,
  start-at-sign-in;
- the in-place update, entirely — download, swap, relaunch;
- anything under `%LOCALAPPDATA%\Surtitle`.

The in-app folder browser, the CLI, the doctor, and the HTTP API are all testable
from anywhere and are covered by the suite.

## How users actually get a release

- **A git checkout** — the tray's update pulls the tag, or `main`.
- **A release archive** — the tray downloads the build for the platform, verifies
  it against `SHA256SUMS.txt`, stages it, and swaps it in after Surtitle has exited.
  The outcome is reported in the tray and written to
  `%LOCALAPPDATA%\Surtitle\updates\` (`last-update.txt` and `apply-update.log`).
- **When the updater itself is broken**, a release cannot deliver its own fix: the
  user has to extract the archive over their installation once, by hand. Say so in
  the release notes when that is the case — 0.5.2 is the example.
- Settings, the database and the models live in the app data directory, not in the
  installation, so extracting an archive over an existing install keeps all of them.

## Deliberately manual

- No tag on merge: a release is a decision, not a side effect.
- No moving a published tag, and no editing published assets.
- No release notes written only by the workflow. The changelog entry is written by
  a person who knows what the change means.
