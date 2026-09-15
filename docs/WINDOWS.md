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

## Installing and updating

`scripts/install.ps1` does the whole job — Python, dependencies, and optionally the
local speech engines and their models — with no administrator rights:

```powershell
# Everything, including offline speech recognition and synthesis
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Yes

# Hosted voice only (smaller and faster; needs a Deepgram key)
.\scripts\install.ps1 -NoVoice

# Update an existing installation
.\scripts\install.ps1 -Update

# Report what is installed and what is missing; change nothing
.\scripts\install.ps1 -Check
```

The script is idempotent: running it twice is a fast no-op.

**Why updating is cheap.** The code and its virtual environment live in the
checkout; the speech models live in `%LOCALAPPDATA%\Surtitle\models`. Updating
replaces the code and re-verifies the models without re-downloading ~90 MB, and
deleting the checkout does not delete the models. Nothing is written to the
registry, to `Program Files`, or to `PATH`.

```powershell
.\scripts\run.ps1 models list       # what is installed, and its size
.\scripts\run.ps1 models download   # fetch or repair models
.\scripts\run.ps1 models verify     # re-check checksums
```

## Offline voice

The local engines run entirely on the CPU, with no API key and no network:

```powershell
.\scripts\install.ps1                    # installs the extra and downloads models
# then: Settings -> Voice -> Speech-to-text engine / Text-to-speech engine = local
```

`uv sync --extra voice-local` installs `sherpa-onnx` (which ships its own ONNX
runtime), and `surtitle models download` fetches the models. Both are needed;
the first without the second reports a missing model by filename rather than
failing silently.

Measured on a 2019 Intel i9: recognition runs at about 0.13× real time, and speech
synthesis at about 0.63–0.70× — fast enough to stay ahead of playback. Turn
detection is silence-based, which is weaker than the hosted engine's contextual
detector; see [`VOICE.md`](VOICE.md) for the numbers and the trade-off.

### If the local voice crashes instead of erroring

Seen once on a real Windows 11 machine, and recorded because it is genuinely
frustrating to diagnose: the process died with a Windows status code
(`0xC0000409`, stack buffer overrun) *after* printing

```
The requested API version [28] is not available, only API versions [1, 17] are
supported in this build. Current ORT Version is: 1.17.1
```

That is an ONNX Runtime API-version mismatch, raised in C++ where no Python
exception can catch it. It could not be reproduced on demand — the same machine,
same wheel, same commands subsequently synthesised speech successfully, and a
fresh environment never showed it — so it is attributed to that machine's state
rather than to a defect here. What is known, and worth checking first, is that
modern Windows ships its **own** `onnxruntime.dll`:

```
C:\Windows\System32\onnxruntime.dll   1.17.x
```

while `sherpa-onnx` bundles 1.23 next to its extension module. Windows resolves
native DLLs by base name and keeps one per process, so a copy loaded earlier by
anything else wins — which is why `surtitle doctor` reports it:

```
[warn] Native library conflicts: onnxruntime.dll on PATH at C:\WINDOWS\system32\onnxruntime.dll
```

**If the local engines die on load,** check for a competing copy and remove it:

```powershell
where.exe onnxruntime.dll                       # several copies is normal
py -m pip list | Select-String onnx             # a global install is the usual cause
py -m pip uninstall onnxruntime onnxruntime-gpu
```

The System32 copy belongs to Windows and cannot be removed; it is what the warning
is telling you about. The local engines are otherwise self-contained — `sherpa-onnx`
needs no separately installed ONNX runtime — and `surtitle doctor` verifies the
model files, the wheel and a real synthesis independently of this problem.

### What the installer needs on Windows

Nothing but a network connection: `scripts\install.ps1` installs `uv`, which brings
its own Python, into `%USERPROFILE%\.local\bin`. No administrator rights, no
registry writes, and nothing added to the system `PATH`.

Python **3.13** works: `sherpa-onnx` 1.13.8 publishes `win_amd64` wheels for
`cp311` through `cp314`, and the project supports all of them.



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
| `--with-voice-local` | Bundle the local speech engines (~30 MB); models still download on first use |
| `--with-local-models` | Bundle the models too (~90 MB), for a fully offline archive |

The default archive contains neither local-voice piece, which keeps the documented
"no network needed to start" property true for the shipped download. The two flags
are independent so the trade-off is a build decision rather than a surprise.

To cut the size of the archive (roughly 130–180 MB, dominated by matplotlib and the
interpreter), consider a `requirements-release.txt` that omits matplotlib for
environments that never chart.

## Optional: a native `surtitle.exe` launcher

`run.bat` and `run.ps1` do the job with no build step, and they are what the
release ships. A native executable is a nicer double-click experience (no console
flash, no execution policy, less antivirus suspicion), and it can be produced from
macOS or Linux without a Windows machine — Zig cross-compiles to a Windows PE:

```bash
zig build-exe launcher.zig -target x86_64-windows -O ReleaseSmall \
    -femit-bin=build/surtitle.exe
```

Verified on this project: Zig 0.16 on macOS produces a working `PE32+ x86-64`
Windows binary with no Windows toolchain involved. A launcher that spawns
`venv\Scripts\python.exe -m surtitle run` and forwards its arguments is all
the code that is needed.

Not implemented here, deliberately: Zig 0.16 replaced `std.heap.GeneralPurposeAllocator`
with `heap.DebugAllocator` and moved process and IO APIs behind a new async-first
`std.Io` layer, and it removed `std.process.argsWithAllocator` and
`std.fs.selfExePath`. Porting is straightforward against a stable Zig release, but
it is not worth maintaining an untestable binary for a convenience wrapper while
the API is in flux. If you want it, the cross-compile path above is proven to work.

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

**A conversion needs a format only LibreOffice can write**
Document conversion itself needs nothing installed: the built-in engine reads Word,
Excel, PowerPoint, OpenDocument and PDF files and writes PDF, text, CSV, HTML and
XLSX. Only the remaining targets (`docx`, `odt`, `rtf`, `ods`, `odp`, `pptx`, `png`,
`jpg`) need LibreOffice. Install it, or set `soffice_path` in `.surtitle.json`:

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
| LibreOffice (optional) | `soffice.exe`, discovered at `C:\Program Files\LibreOffice\program`. Only needed for the formats the built-in converter cannot write |
| Virtual environment layout | `Scripts\python.exe` and `Lib\site-packages` instead of `bin/` and `lib/` |
