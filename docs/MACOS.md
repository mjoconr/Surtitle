# Running on macOS

The goal: **download one file, extract it, run `./run.sh`.** No installer, no
administrator rights, no system Python, and no compiler.

## Using a release build

1. Download the archive for your Mac from [Releases](../../releases) —
   `surtitle-<version>-darwin-arm64.tar.gz` for Apple silicon, `…-darwin-x86_64.tar.gz`
   for Intel.
2. Extract it anywhere writable:

   ```bash
   tar -xzf surtitle-<version>-darwin-x86_64.tar.gz
   ```

3. Run the launcher and open the address it prints:

   ```bash
   cd surtitle
   ./run.sh
   ```

   The **first run takes a few minutes**: there is no bundled Python, so `uv`
   fetches a Python runtime and the dependencies and builds the environment beside
   the launcher. Every run after that starts immediately. Double-click
   **`Setup.command`** instead if you would rather have an entry in
   `~/Applications` and be asked about starting at sign-in.

4. Your browser opens `http://127.0.0.1:8765`. Closing the terminal stops the app.

Nothing is installed system-wide. The environment lives in `.venv/` inside the
extracted folder, and the app's data in `~/Library/Application Support/Surtitle`.

## Why the macOS archive carries no binaries

A file downloaded **in a browser** gets `com.apple.quarantine`, and macOS refuses
to run unsigned executables that carry it. An archive that bundles a Python runtime
therefore fails one dialog at a time — the interpreter, every compiled extension
module (`*.cpython-*.so`, including the Rust-built `_pydantic_core` and `jiter`),
the launcher — each saying the developer cannot be verified and that macOS cannot
check it for malware. It reads like a malware report and is not one: those binaries
simply have no Developer ID signature.

So the macOS archive contains **none of them**. It is sources, JSON, markdown and a
shell script — about a megabyte — and there is nothing in it for Gatekeeper to
refuse. You can check both halves of that claim:

```bash
tar -tzf surtitle-<version>-darwin-x86_64.tar.gz | wc -l        # ~90 files
mkdir -p /tmp/x && tar -xzf surtitle-<version>-darwin-x86_64.tar.gz -C /tmp/x
find /tmp/x -type f -exec file {} + | grep -c Mach-O             # 0
xattr -l /tmp/x/surtitle/run.sh                                  # quarantined…
./tmp/x/surtitle/run.sh --version                                # …and it still runs
```

The quarantine flag is inherited from the archive, and the shell script runs anyway
because the interpreter Apple signed is `/bin/sh`, not the script. What the first
run installs afterwards — the Python runtime, the wheels, and `uv` itself if you
install it from its own installer — is fetched by a *program*, and programs do not
set the quarantine attribute. That is the whole trick, and it is also why a git
checkout was never affected.

The alternative — bundling the runtime and signing it — is described at the end of
this page.

## If you build a bundled archive yourself

`python scripts/build_release.py --with-voice-local` still produces the
self-contained archive, and `--with-local-models` adds the speech models for a
machine with no network. Those are the ones macOS will block, because they are the
ones with binaries in them:

```bash
xattr -dr com.apple.quarantine surtitle-<version>-darwin-x86_64.tar.gz   # before extracting
xattr -dr com.apple.quarantine ./surtitle                                # or afterwards
```

`xattr -dr` clears it on everything already inside the folder, which is what the
per-binary dialogs are complaining about. Downloading without a browser avoids it
too — `curl` and `gh` never set the attribute:

```bash
gh release download <version> --pattern 'surtitle-*-darwin-*.tar.gz'
curl -fLO https://github.com/mjoconr/Surtitle/releases/download/<version>/surtitle-<version>-darwin-x86_64.tar.gz
```

The in-app updater has the same property, since it downloads with Python rather
than a browser.

### Verify what you downloaded

There is no signature to check, so check the digest. `SHA256SUMS.txt` is published
alongside every release, and the build's own verifier can re-check an artifact as a
user would receive it:

```bash
shasum -a 256 -c SHA256SUMS.txt --ignore-missing
uv run python scripts/build_release.py --verify-only surtitle-<version>-darwin-x86_64.tar.gz
```

For a thin archive that verifier checks the tree is complete, that every source file
compiles, and that the launcher is valid shell. Proving the *first run* works is the
build's job, and it does it by installing the archive from scratch on the runner.

## A managed or corporate Mac

An endpoint agent or an approved-software policy can block even a thin archive's
first run, because the binaries it fetches are still unsigned binaries arriving on a
managed machine. The practical answers, in order of preference:

- **Run from the checkout.** Same code, and the environment comes from `uv`:

  ```bash
  git clone https://github.com/mjoconr/Surtitle
  cd Surtitle
  ./scripts/run.sh        # first run fetches Python and the dependencies
  ```

- **Ask IT to allow it**, naming the folder. Nothing needs an installer, admin
  rights or a background service: it is a directory, a launcher, and a Python
  runtime in `.venv/`.

## What would remove the block for good

Signing the environment with an Apple **Developer ID** certificate and notarizing
it: every Mach-O signed, the result submitted to Apple, and the ticket distributed
with the download. That needs a paid Apple Developer account, it has to be redone
for every release and for any wheel that comes with a compiled module, and
`stapler` cannot staple a `.tar.gz` — the macOS artifact would become a `.dmg` or
`.pkg`. Until then, carrying no binaries is the cheaper answer, and it is what the
release does.

Note also that a Mac archive can only be checked on a Mac of its own architecture:
the verifier runs binaries from the archive, so an `x86_64` build fails on Apple
silicon and vice versa. See [`RELEASING.md`](RELEASING.md#7-what-one-machine-cannot-check).
