"""Installing the local speech engines and their models from inside the app.

The tray icon and the Settings screen both need to do what
``surtitle models download`` does, plus the step that command assumes has already
happened: getting ``sherpa-onnx`` installed in the first place. A user who
extracted a release archive, or who ran the installer with ``-NoVoice``, has the
models command available and the runtime missing, and asking them to find a
terminal is exactly the dead end this module exists to remove.

The two halves are deliberately separate, because they fail for different reasons
and have different fixes:

* the **runtime** is a wheel, installed with ``uv sync --extra voice-local`` in a
  source checkout or ``pip install`` into the bundled runtime of an archive;
* the **models** are hundreds of megabytes of data, fetched by
  :mod:`surtitle.voice.models`.

An install is a background job — it downloads from the network and must never
block the server, the tray's menu, or a request handler.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from surtitle.config import Settings

__all__ = [
    "InstallJob",
    "InstallPlan",
    "InstallResult",
    "LocalVoiceState",
    "install",
    "runtime_plan",
    "runtime_present",
    "state",
]

# The range the project pins in pyproject.toml. Kept as strings here rather than
# read from the lock, because the fallback path runs where no lock is shipped.
_SHERPA_REQUIREMENTS = ("sherpa-onnx>=1.13,<2", "sherpa-onnx-core>=1.13,<2")


def runtime_present() -> bool:
    """True when ``sherpa_onnx`` can be imported by this interpreter.

    ``find_spec`` rather than an import: importing the bindings loads a native
    ONNX runtime and takes a noticeable moment, and this is asked on every tray
    poll. A partially installed package raises instead of returning ``None``, and
    that is a "not present" like any other.
    """
    try:
        return importlib.util.find_spec("sherpa_onnx") is not None
    except (ImportError, ValueError):
        return False


def _checkout_root() -> Path | None:
    """The source checkout this installation came from, if there is one.

    ``uv sync`` only means anything beside a project; a wheel installed into a
    bundled runtime has no ``pyproject.toml`` to sync, and the fallback is pip.
    """
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "pyproject.toml").is_file() and (candidate / "uv.lock").is_file():
        return candidate
    return None


def _uv_fallback() -> str | None:
    """uv where its own installer puts it, for a shell that cannot see it yet."""
    candidate = Path.home() / ".local" / "bin" / ("uv.exe" if sys.platform == "win32" else "uv")
    return str(candidate) if candidate.is_file() else None


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """The command that adds the local engines, and what to call it in a report."""

    command: list[str]
    cwd: Path | None = None
    label: str = ""


def runtime_plan(
    *,
    uv: str | None = None,
    checkout: Path | None = None,
    executable: str | None = None,
) -> InstallPlan:
    """How to install the local engines here.

    In a source checkout ``uv sync --extra voice-local`` installs the versions the
    lock names, which is the point of the lock. Anywhere else — a release
    archive, a wheel, a checkout without uv — the bundled interpreter's own pip
    installs the same two wheels directly; the runtime ships its own ONNX
    runtime, so there is no third package to keep in step.
    """
    if uv is None:
        uv = shutil.which("uv") or _uv_fallback()
    if checkout is None:
        checkout = _checkout_root()
    if executable is None:
        executable = sys.executable

    if uv and checkout is not None:
        return InstallPlan(
            command=[uv, "sync", "--extra", "voice-local", "--inexact"],
            cwd=checkout,
            label="uv sync --extra voice-local",
        )
    return InstallPlan(
        command=[executable, "-m", "pip", "install", *_SHERPA_REQUIREMENTS],
        label="pip install sherpa-onnx",
    )


@dataclass(frozen=True, slots=True)
class LocalVoiceState:
    """Whether local speech can be used right now, and what is missing if not."""

    runtime: bool
    models: bool
    detail: str
    missing_models: int = 0
    missing_bytes: int = 0

    @property
    def ready(self) -> bool:
        return self.runtime and self.models


def state(
    settings: Settings, *, runtime: bool | None = None, statuses: list[Any] | None = None
) -> LocalVoiceState:
    """Report the two halves, statting model files but never loading them."""
    from surtitle.voice import models

    if runtime is None:
        runtime = runtime_present()
    rows = models.status(settings) if statuses is None else statuses
    models_ready = bool(rows) and all(item.present for item in rows)

    if runtime and models_ready:
        detail = "local speech is ready"
    elif not runtime:
        detail = "the local speech engines are not installed"
    elif rows:
        missing = sum(1 for item in rows if not item.present)
        detail = f"{missing} speech model(s) still to download"
    else:
        detail = "no speech models are registered"

    # Carried so a confirmation can quote a real figure: the models run to
    # hundreds of megabytes, and a hopeful round number in a "download?" prompt
    # is a promise the download will not keep.
    missing_rows = [item for item in rows if not item.present]
    missing_bytes = sum(int(getattr(item, "total_bytes", 0) or 0) for item in missing_rows)
    return LocalVoiceState(
        runtime=runtime,
        models=models_ready,
        detail=detail,
        missing_models=len(missing_rows),
        missing_bytes=missing_bytes,
    )


@dataclass(slots=True)
class InstallResult:
    ok: bool
    message: str
    steps: list[str] = field(default_factory=list)


def install(
    settings: Settings,
    *,
    progress: Callable[[Any], None] | None = None,
    runner: Callable[..., Any] | None = None,
) -> InstallResult:
    """Install the engines if needed, then download the models.

    Blocking by design: the caller decides where it runs (a background thread, a
    CLI command). ``runner`` exists so a test can assert the command without a
    network or a package manager.
    """
    from surtitle.voice import models

    run = runner or subprocess.run
    steps: list[str] = []

    if not runtime_present():
        plan = runtime_plan()
        steps.append(plan.label)
        try:
            completed = run(
                plan.command,
                cwd=str(plan.cwd) if plan.cwd is not None else None,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return InstallResult(False, f"could not run {plan.label}: {exc}", steps)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            return InstallResult(
                False,
                f"{plan.label} failed: {detail[-1] if detail else 'no output'}",
                steps,
            )
        if not runtime_present():
            # pip can report success while the wheel is not importable here (a
            # platform mismatch, for instance), and saying so now is far more
            # useful than a later "No module named 'sherpa_onnx'".
            return InstallResult(
                False,
                "the engines were installed but cannot be imported; a restart may be needed",
                steps,
            )
        steps.append("installed the local speech engines")

    try:
        models.download(settings, progress=progress)
    except models.ModelUnavailable as exc:
        return InstallResult(False, f"could not download the speech models: {exc.reason}", steps)
    except (OSError, ValueError) as exc:
        return InstallResult(False, f"could not download the speech models: {exc}", steps)

    steps.append("downloaded the speech models")
    return InstallResult(True, "local speech is installed; restart Surtitle to use it", steps)


class InstallJob:
    """One background install, with a state the tray and the UI can read.

    Deliberately not a task in the server's event loop: the work is a blocking
    subprocess and a long download, and the loop must stay free to answer the
    very requests that report progress.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._ok: bool | None = None
        self._message = "not started"
        self._percent = 0.0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "ok": self._ok,
                "message": self._message,
                "percent": round(self._percent, 1),
            }

    def start(self, settings: Settings) -> bool:
        """Start an install. False when one is already running."""
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._ok = None
            self._message = "starting…"
            self._percent = 0.0

        thread = threading.Thread(
            target=self._run, args=(settings,), name="surtitle-voice-install", daemon=True
        )
        self._thread = thread
        thread.start()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """Block until a running install finishes. True when it is not running."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.running

    def _run(self, settings: Settings) -> None:
        try:
            result = install(settings, progress=self._progress)
        except Exception as exc:  # a failure is reported, never raised into a request
            result = InstallResult(False, f"the install failed: {exc}")
        with self._lock:
            self._running = False
            self._ok = result.ok
            self._message = result.message
            if result.ok:
                self._percent = 100.0

    def _progress(self, update: Any) -> None:
        with self._lock:
            if update.stage == "download" and update.total:
                self._percent = 100.0 * update.received / update.total
                self._message = f"downloading {update.asset} ({self._percent:.0f}%)"
            else:
                self._percent = 0.0
                self._message = f"{update.asset}: {update.message or update.stage}"
