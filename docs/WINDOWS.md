# Running on Windows

The goal: **download one file, extract it, double-click `run.bat`.** No installer, no
administrator rights, no system Python, and no internet connection required to start.

## Using a release build

1. Download `surtitle-<version>-win32-amd64.zip` from [Releases](../../releases).
2. Right-click → **Extract All** (or use 7-Zip). Extract to anywhere writable — your
   Desktop or Documents is fine. *Do not* run it from inside the zip preview; Windows
   will extract to a temporary folder and the bundled runtime will not be found.
3. Double-click **`run.bat`**.
4. Your browser opens `http://127.0.0.1:8765`.

If SmartScreen shows "Windows protected your PC", choose **More info → Run anyway**. The
archive is unsigned, which is what triggers that prompt; `SHA256SUMS.txt` is published
alongside each release so you can verify the download.

The console window that opens is the server. Closing it stops the app.

### What is in the archive

| Path | Purpose |
|---|---|
| `run.bat` | Launcher for Command Prompt / double-click |
| `run.ps1` | Launcher for PowerShell |
| `python/` | Standalone CPython — needs no installer and no registry entries |
| `venv/` | All dependencies, already installed |
| `wheelhouse/` | Offline copies of every wheel, for repair without network |
| `src/` | The application source |
| `BUILD-INFO.json`, `VERSION` | What was built and when |
| `SHA256SUMS.txt` | Checksums (releases only) |

Uninstalling is deleting the folder. Nothing is written to the registry, to
`Program Files`, or to `PATH`.

### Where your data goes

`%LOCALAPPDATA%\Surtitle` — settings, credentials and the session database.
Delete that folder as well for a complete removal.

## Running from source

```powershell
git clone <this repo>; cd Surtitle
py -3.12 -m venv .venv          # or any Python 3.11+
.\.venv\Scripts\pip install -e .
.\scripts\run.bat
```

Or install [uv](https://docs.astral.sh/uv/) and let it handle Python entirely:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
.\scripts\run.ps1
```

`run.ps1` and `run.bat` both prefer, in order: a bundled runtime, `uv` from a source
checkout, then an existing `.venv`.

If PowerShell refuses to run the script, use:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

Or use `run.bat`, which has no execution-policy restriction.

## Package installation by the agent

The agent can install Python packages it needs. On Windows this stays entirely inside
the project:

```
<your project>\.surtitle\venv\Scripts\python.exe
```

Nothing is installed system-wide, nothing is added to `PATH`, and no elevation is
requested. The application's own environment is never modified, which is also why a
release archive cannot be corrupted by agent activity.

## Building a release archive

Build **on Windows**. Compiled wheels (numpy, Pillow, matplotlib) are platform-specific,
and a cross-built archive would ship macOS binaries that fail on import. The GitHub
Actions workflow does this correctly on `windows-latest`.

```powershell
uv run python scripts\build_release.py
```

Output lands in `dist\`. Useful flags:

| Flag | Effect |
|---|---|
| `--skip-wheelhouse` | Smaller archive; offline repair is no longer possible |
| `--keep-build` | Reuse `build\release` instead of starting fresh |

To cut the size of the archive (roughly 130–180 MB, dominated by matplotlib and the
interpreter), consider a `requirements-release.txt` that omits matplotlib for
environments that never chart.

## Troubleshooting

**"no Python environment was found"**
The archive was not fully extracted, or `run.bat` was run from inside the zip preview.
Extract properly and check that `venv\Scripts\python.exe` exists.

**"Port 8765 is in use"**
The app probes forward and reports which port it chose. To pin one:
`run.bat run --port 9000`, or `run.bat run --strict-port` to fail instead of probing.

**The microphone does not work**
Windows Settings → Privacy → Microphone → allow desktop apps. Browsers will not offer
microphone access to a non-secure origin except `localhost`, which is why the app binds
`127.0.0.1`; do not change the host to a LAN address and expect the mic to work.

**LibreOffice conversion is unavailable**
Install LibreOffice, or set `soffice_path` in `.surtitle.json`:

```json
{ "soffice_path": "C:/Program Files/LibreOffice/program/soffice.exe" }
```

**Antivirus flags or slows the first run**
A bundled interpreter plus a virtual environment is a large number of new executables,
which heuristics dislike. Adding the extracted folder as an exclusion resolves it.
Signed releases would avoid this and are a possible future addition.

**An MCP server fails to start**
The reason appears in the app. Common causes: a Windows path using forward slashes in
`.surtitle.json` (both work, but a space in the path must be quoted), the executable
not being on `PATH`, or the server needing an environment variable that is not listed in
its `env` map.

## Platform notes

These are the places Windows genuinely differs, and how each is handled:

| Concern | Handling |
|---|---|
| Subprocess tree kill | `taskkill /F /T /PID` rather than `terminate()`, which only kills the immediate process |
| Long path support | Symlink-aware resolution; extended-length `\\?\` prefixes are rejected as escapes |
| Case-insensitive paths | Containment compares case-folded, so `C:\Proj` and `c:\proj` are the same directory |
| Credential file permissions | `0600` is applied on POSIX; on Windows the file inherits the user profile ACL, so it stays per-user but is not mode-enforced |
| Shell | `cmd.exe /d /s /c` |
| LibreOffice | `soffice.exe`, discovered at `C:\Program Files\LibreOffice\program` |
| Virtual environment layout | `Scripts\python.exe` and `Lib\site-packages` instead of `bin/` and `lib/` |
