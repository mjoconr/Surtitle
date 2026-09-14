"""Document conversion through a local LibreOffice install.

LibreOffice's headless mode is the most reliable way to turn real-world Office
documents into something an agent can read: it handles ``.docx``, ``.xlsx``,
``.pptx``, ``.odt`` and friends, which pure-Python libraries only partly cover.

Two operational details matter more than the conversion itself:

* **A private user profile per run.** LibreOffice refuses to start a second
  instance against the same profile, so a shared profile is the classic cause of
  "conversion hangs forever" reports. ``-env:UserInstallation`` gives each call
  its own profile.
* **Killing the whole process group on timeout.** A hung ``soffice`` used to
  leave orphans behind; the shell runner kills the process group, and the
  ``timeout`` here is generous because a cold LibreOffice start is slow.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from surtitle.tools.fs_tools import ToolContext, ToolResult
from surtitle.tools.path_guard import PathEscapeError, resolve_in_root
from surtitle.tools.project_config import configured_soffice

__all__ = ["SUPPORTED_TARGETS", "convert_document", "list_supported_targets"]

log = logging.getLogger(__name__)

# Writer, Calc, Impress and the common interchange formats. Keys are the
# friendly names the model is told to use; values are LibreOffice's filter names,
# which are the only way to disambiguate formats that share an extension.
SUPPORTED_TARGETS: dict[str, tuple[str, str]] = {
    # name: (extension, filter)
    "pdf": ("pdf", "writer_pdf_Export"),
    "docx": ("docx", "MS Word 2007 XML"),
    "odt": ("odt", "writer8"),
    "txt": ("txt", "Text (encoded):UTF8"),
    "html": ("html", "HTML (StarWriter)"),
    "rtf": ("rtf", "Rich Text Format"),
    "csv": ("csv", "Text - txt - csv (StarCalc):44,34,76,1"),
    "xlsx": ("xlsx", "Calc MS Excel 2007 XML"),
    "ods": ("ods", "calc8"),
    "pptx": ("pptx", "Impress MS PowerPoint 2007 XML"),
    "odp": ("odp", "impress8"),
    "png": ("png", "writer_png_Export"),
    "jpg": ("jpg", "writer_jpg_Export"),
}

# A cold start of LibreOffice can take tens of seconds on Windows.
_DEFAULT_TIMEOUT = 180.0

# One conversion at a time, process-wide.
#
# LibreOffice is a large single-instance office suite: it is happy enough on its
# own, but concurrent starts on a loaded machine occasionally produce no output at
# all. Serialising costs nothing for a tool that is used a few times per session
# and removes an intermittent failure that is hard to attribute afterwards.
_LO_LOCK = asyncio.Lock()


def list_supported_targets() -> list[str]:
    """Target format names, for the tool schema and error messages."""
    return sorted(SUPPORTED_TARGETS)


async def convert_document(
    ctx: ToolContext,
    source: str,
    *,
    target: str = "pdf",
    output: str | None = None,
    overwrite: bool = True,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ToolResult:
    """Convert a document to another format using local LibreOffice.

    ``source`` and ``output`` are project-relative. When ``output`` is omitted,
    the converted file lands beside the source with the new extension.
    """
    target_name = target.strip().lower().lstrip(".")
    if target_name not in SUPPORTED_TARGETS:
        return ToolResult(
            ok=False,
            error=(
                f"Unsupported target format {target!r}. "
                f"Supported: {', '.join(list_supported_targets())}."
            ),
        )

    try:
        source_path = resolve_in_root(ctx.root, source)
    except PathEscapeError as exc:
        return ToolResult(ok=False, error=str(exc), display=f"blocked: {source}")

    if not source_path.absolute.is_file():
        return ToolResult(ok=False, error=f"Source file not found: {source_path.relative}")

    soffice = configured_soffice(ctx.root)
    if soffice is None:
        return ToolResult(
            ok=False,
            error=(
                "LibreOffice was not found, so document conversion is unavailable. "
                "Install it, or set 'soffice_path' in .surtitle.json (or the "
                "SURTITLE_SOFFICE environment variable) to the soffice binary."
            ),
            display="LibreOffice not found",
        )

    extension, filter_name = SUPPORTED_TARGETS[target_name]

    if output:
        try:
            destination = resolve_in_root(ctx.root, output)
        except PathEscapeError as exc:
            return ToolResult(ok=False, error=str(exc), display=f"blocked: {output}")
    else:
        destination = resolve_in_root(ctx.root, source_path.relative)
        destination = type(destination)(
            absolute=destination.absolute.with_suffix(f".{extension}"),
            relative=str(Path(destination.relative).with_suffix(f".{extension}")),
            root=destination.root,
        )

    if destination.absolute == source_path.absolute:
        return ToolResult(
            ok=False,
            error="The output path would overwrite the source file. Choose another name.",
        )

    if destination.absolute.exists() and not overwrite:
        return ToolResult(
            ok=False,
            error=f"{destination.relative} already exists. Pass overwrite=true to replace it.",
        )

    destination.absolute.parent.mkdir(parents=True, exist_ok=True)

    # LibreOffice ignores the requested output filename: `--convert-to` always
    # writes `<source stem>.<ext>` into `--outdir`. Asking for
    # `reports/summary.pdf` from `note.txt` therefore produces `note.pdf` and,
    # without this staging step, the conversion appears to fail. Convert into a
    # private staging directory, find what was actually written, then move it
    # into place. Staging also means a failed run cannot leave a half-written
    # file at the destination.
    with tempfile.TemporaryDirectory(prefix="surtitle-lo-out-") as staging:
        staging_dir = Path(staging)

        # A second private directory for the user profile: LibreOffice refuses
        # to start two instances against one profile, and a stale lock is the
        # classic cause of conversions hanging forever.
        with tempfile.TemporaryDirectory(prefix="surtitle-lo-profile-") as profile:
            argv = [
                str(soffice),
                f"-env:UserInstallation={Path(profile).as_uri()}",
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nodefault",
                "--nofirststartwizard",
                "--convert-to",
                f"{filter_name}" if target_name == "csv" else extension,
                "--outdir",
                str(staging_dir),
                str(source_path.absolute),
            ]
            async with _LO_LOCK:
                result = await _run(argv, timeout=timeout)

        if result.timed_out:
            return ToolResult(
                ok=False,
                error=(
                    f"LibreOffice did not finish within {timeout:.0f}s. A cold start can be "
                    "slow; try again, or convert a smaller document."
                ),
                display="Conversion timed out",
            )

        produced = _find_output(staging_dir, source_path.absolute.stem, extension)

        if produced is None:
            # Report LibreOffice's own output, which names the real cause
            # (missing filter, corrupt file, unsupported format).
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            detail = [line for line in detail if "Task policy set failed" not in line]
            return ToolResult(
                ok=False,
                error=(
                    f"LibreOffice did not produce a .{extension} file from "
                    f"{source_path.relative}. " + (detail[-1] if detail else "No output written.")
                ),
                display="Conversion failed",
            )

        try:
            if destination.absolute.exists():
                destination.absolute.unlink()
            shutil.move(str(produced), str(destination.absolute))
        except OSError as exc:
            return ToolResult(ok=False, error=f"Could not place the converted file: {exc}")

    size = destination.absolute.stat().st_size
    return ToolResult(
        ok=True,
        data={
            "source": source_path.relative,
            "path": destination.relative,
            "target": target_name,
            "bytes": size,
            "libreoffice": str(soffice),
        },
        display=f"Converted {source_path.relative} to {destination.relative}",
        artifacts=[destination.relative]
        if extension in {"pdf", "xlsx", "csv", "png", "jpg"}
        else [],
    )


def _find_output(staging: Path, stem: str, extension: str) -> Path | None:
    """Locate the file LibreOffice actually wrote.

    It normally uses the source stem, but some import filters rewrite the name,
    so fall back to any file with the expected extension.
    """
    expected = staging / f"{stem}.{extension}"
    if expected.is_file():
        return expected
    candidates = sorted(p for p in staging.iterdir() if p.suffix.lower() == f".{extension}")
    if len(candidates) == 1:
        return candidates[0]
    # Prefer the first non-empty file when several were produced.
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


class _RunResult:
    __slots__ = ("returncode", "stderr", "stdout", "timed_out")

    def __init__(self, stdout: str, stderr: str, returncode: int | None, timed_out: bool) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = timed_out


async def _run(argv: list[str], *, timeout: float) -> _RunResult:
    """Run LibreOffice, killing the whole process group if it overruns."""
    import contextlib
    import os

    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except FileNotFoundError:
        return _RunResult("", f"Executable not found: {argv[0]}", None, False)
    except OSError as exc:
        return _RunResult("", f"Could not start LibreOffice: {exc}", None, False)

    communicate = asyncio.create_task(process.communicate())
    done, _pending = await asyncio.wait({communicate}, timeout=timeout)

    if not done:
        _kill_tree(process)
        with contextlib.suppress(Exception):
            await asyncio.wait({communicate}, timeout=5)
        if not communicate.done():
            communicate.cancel()
        return _RunResult("", "", None, True)

    stdout_bytes, stderr_bytes = communicate.result()
    return _RunResult(
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        process.returncode,
        False,
    )


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate LibreOffice and its children."""
    import contextlib
    import os
    import subprocess
    import time

    if process.returncode is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), 15)
    with contextlib.suppress(Exception):
        process.terminate()
    for _ in range(30):
        if process.returncode is not None:
            return
        time.sleep(0.1)
    with contextlib.suppress(Exception):
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), 9)
        process.kill()


def shutil_which(name: str) -> str | None:  # pragma: no cover - thin wrapper
    """Exposed for tests that want to assert discovery behaviour."""
    return shutil.which(name)
