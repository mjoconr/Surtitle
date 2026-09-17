# Running on Windows

The goal: **download one file, extract it, double-click `run.bat`.** No installer, no
administrator rights, no system Python, and no internet connection required to start.

## Using a release build

1. Download `surtitle-<version>-win32-AMD64.zip` from [Releases](../../releases).
2. Right-click → **Extract All** (or use 7-Zip). Extract to anywhere writable — your
   Desktop or Documents is fine. *Do not* run it from inside the zip preview; Windows
   will extract to a temporary folder and the bundled runtime will not be found.
3. Double-click **`run.bat`**.
4. Your browser opens `http://127.0.0.1:8765`.

If SmartScreen shows "Windows protected your PC", choose **More info → Run anyway**. The
archive is unsigned, which is what triggers that prompt; `SHA256SUMS.txt` is published
alongside each release so you can verify the download.

The console window that opens is the server. Closing it stops the app.

### The taskbar icon

While the server runs, a Surtitle icon sits in the notification area. Left-click or
double-click it to open the app in your browser; right-click it for:

| Item | What it does |
|---|---|
| **Open Surtitle** | Opens the app in your default browser |
| **Status…** | Address, uptime, model, which voice engines are in use, whether each key is set, how many conversations are live, and what is in the database |
| **Usage…** | Turns answered, tool calls, tokens in and out, cache hits, and an estimated cost for this run |
| **Stop Surtitle** | Graceful shutdown — the same as Ctrl+C in the console, not a kill |

The two lines at the top of the menu are the live state, not commands: how many
conversations are open, how many turns have been answered, tokens used, estimated
spend, and uptime.

`--no-tray` turns the icon off; `--tray` forces it on (it is on by default on
Windows). If the server was started by something other than `surtitle run`, attach
an icon to it with:

```powershell
run.bat tray                              # find the running server
run.bat tray --url http://127.0.0.1:9000  # or name it
```

Windows files a **newly registered** notification icon under the overflow arrow
(`^`) rather than on the taskbar itself. That is a per-icon user setting, not
something the application can change, so on first run look under `^`, then drag
Surtitle onto the taskbar — or turn it on in **Settings → Personalization →
Taskbar → Other system tray icons**. The console says the same thing once at
startup.

**What the cost estimate is.** `Usage…` prices tokens against DeepSeek's
published rates, including the peak/off-peak difference and the cache-hit
discount, and it says which date those rates were checked. If the model has no
published rate in this build, it shows tokens and *no* money figure rather than a
confident `$0.00`. To correct the rates without waiting for a release, set
`SURTITLE_PRICE_INPUT`, `SURTITLE_PRICE_CACHED_INPUT` and `SURTITLE_PRICE_OUTPUT`
(USD per million tokens) in the environment or `.env`.

**Stop needs a local connection.** The tray talks to the server over
`127.0.0.1`, and `POST /api/shutdown` refuses anything that is not from this
machine. Binding the app to a LAN address (`--host 0.0.0.0`) exposes the UI
itself, which has no authentication — the stop button is not the weak link, but
it is not a remote-control endpoint either.

### What is in the archive

| Path | Purpose |
|---|---|
| `run.bat` | Launcher for Command Prompt / double-click |
| `run.ps1` | Launcher for PowerShell |
| `run.sh` | Launcher for macOS and Linux (shipped, but not the Windows entry point) |
| `python/` | Standalone CPython — needs no installer and no registry entries. A Windows archive installs the dependencies into this runtime as well. |
| `venv/` | **macOS and Linux only** — the dependencies, already installed. A Windows archive deliberately has no `venv/`: a Windows virtual environment records an absolute base path and cannot be moved once the archive is extracted, so its interpreter would not start. |
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

The simple way: **double-click `Setup.bat`** at the top of the checkout. It needs
no terminal, installs everything, and is described under
[Installing and updating](#installing-and-updating) below.

To do it by hand instead:

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
checkout, then an existing `.venv`. With no arguments they start the app — that is
what a double-click, the Start Menu entry and the sign-in shortcut all rely on.
Passing a command still works exactly as before: `run.bat doctor`,
`run.bat models list`, `run.bat run --port 9000`.

Both look for `uv` on `PATH` *and* in `%USERPROFILE%\.local\bin`, where uv's own
installer puts it. That matters immediately after installing uv: the installer
updates your user `PATH`, which does not change the window you are already in, so
running the launcher in that same window works without opening a new one.

If PowerShell refuses to run the script, use:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

Or use `run.bat`, which has no execution-policy restriction.

## Installing and updating

`Setup.bat` is the double-clickable face of `scripts/install.ps1`. It forwards its
arguments to the installer and holds the window open afterwards so the result can
be read:

```
Setup.bat                 install everything, including offline voice
Setup.bat -NoVoice        hosted voice only (smaller and faster)
Setup.bat -Update         update an existing installation
Setup.bat -Check          report what is installed; change nothing
```

A double-click starts with an execution policy of `Restricted`, which would refuse
`install.ps1` — and leaves nowhere to type the bypass — so `Setup.bat` passes
`-ExecutionPolicy Bypass` for that one invocation rather than asking you to change
a machine-wide setting.

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

The script is idempotent: running it twice is a fast no-op. It also adds a
Surtitle entry to the Start Menu for the current user, pointing at `run.bat` and
carrying the app's own icon, so it can be pinned to the taskbar or the Start Menu
without going through the extracted folder. `-NoShortcut` skips that step, and
removing it is deleting one `.lnk` — nothing outside your profile is touched.

It then **asks whether Surtitle should start when you sign in**, and adds or
removes a shortcut in your Startup folder accordingly. Nothing is added behind your
back: `-Yes` (and a run with no console to answer on) means no, `-Startup` says
yes, `-NoStartup` says no and removes an existing entry. The sign-in shortcut
starts the server minimized with `--no-browser`, so signing in does not throw a
browser window over whatever you were doing; the notification icon and the Start
Menu entry open the UI when you want it. Removing it is deleting one `.lnk` from
`shell:startup`.

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
| `--verify-only` | Don't build: re-check the archive(s) already in `dist\` (or the paths you name). The same verifier the build runs, so it is also how you check a download after extracting it |

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
Two very different causes, and the message now tells them apart.

*From a release archive:* it was not fully extracted, or `run.bat` was run from
inside the zip preview. Extract properly and check that
`venv\Scripts\python.exe` exists.

*From a source checkout:* uv is missing, or it is installed but invisible to the
window you are in. uv's installer adds `%USERPROFILE%\.local\bin` to your **user
PATH**, which only affects windows opened *afterwards* — so a uv installed one
command ago cannot be found by `where uv` in the very window that installed it.
The launchers look in that directory as well, so either works:

```powershell
# A new window picks the PATH up:
.\scripts\run.bat

# Or add it to this one:
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
.\scripts\run.bat
```

Installing in one step avoids the question entirely, and needs no PATH at all:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

**PowerShell still refuses a script after `Set-ExecutionPolicy RemoteSigned`**
`RemoteSigned` runs local scripts but requires a signature on ones carrying the
mark-of-the-web, which is what a script extracted from a downloaded zip has. The
file is not untrusted — you downloaded it — so unblock it, or bypass the policy
for one command:

```powershell
Unblock-File .\scripts\install.ps1
# or
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`run.bat` is never affected by any of this: batch files are not subject to the
execution policy.

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
| Long path support | The extended-length `\\?\` prefix `resolve()` adds for long and unusual paths is stripped before containment, so a long path is not mistaken for an escape |
| Case-insensitive paths | Containment compares case-folded, so `C:\Proj` and `c:\proj` are the same directory |
| Credential file permissions | `0600` is applied on POSIX; on Windows the file inherits the user profile ACL, so it stays per-user but is not mode-enforced |
| Shell | `cmd.exe /d /s /c` |
| LibreOffice (optional) | `soffice.exe`, discovered at `C:\Program Files\LibreOffice\program`. Only needed for the formats the built-in converter cannot write |
| Virtual environment layout | `Scripts\python.exe` and `Lib\site-packages` instead of `bin/` and `lib/` |
| Release archive layout | Installs into the bundled runtime (`python\python.exe`) rather than a virtual environment: a Windows venv's `Scripts\python.exe` is a launcher for an absolute base path in `pyvenv.cfg`, so it cannot be moved once extracted |
