"""Document conversion, built-in first and LibreOffice when it helps.

Two engines, chosen per call:

* The **built-in** converter (:mod:`surtitle.tools.document_native`) needs
  nothing installed. It reads Word, Excel, PowerPoint, OpenDocument, PDF and the
  plain formats, and writes PDF, text, CSV, HTML and XLSX. Fidelity is honest
  rather than high: it carries content, not layout.
* **LibreOffice**, when it happens to be installed, keeps layout and handles the
  formats that would be a research project to parse (legacy ``.doc``, ``.rtf``,
  high-fidelity export). It is a quality upgrade, never a requirement.

Three operational details matter more than the conversion itself:

* **A private user profile per run.** LibreOffice refuses to start a second
  instance against the same profile, so a shared profile is the classic cause of
  "conversion hangs forever" reports. ``-env:UserInstallation`` gives each call
  its own profile.
* **Killing the whole process group on timeout.** A hung ``soffice`` used to
  leave orphans behind; the shell runner kills the process group, and the
  ``timeout`` here is generous because a cold LibreOffice start is slow.
* **Staging the output.** LibreOffice ignores the requested output filename, so a
  private staging directory is used and the file it actually wrote is moved into
  place.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from surtitle.tools import document_native
from surtitle.tools.fs_tools import ToolContext, ToolResult
from surtitle.tools.path_guard import PathEscapeError, ResolvedPath, resolve_in_root
from surtitle.tools.project_config import configured_soffice

__all__ = ["SUPPORTED_TARGETS", "convert_document", "list_supported_targets"]

log = logging.getLogger(__name__)

# Which engine performs the conversion. ``auto`` prefers the built-in converter
# because it is instant and always available, and reaches for LibreOffice when
# the built-in one cannot do the job and LibreOffice is installed.
Backend = Literal["auto", "builtin", "libreoffice"]

_BACKENDS: frozenset[str] = frozenset({"auto", "builtin", "libreoffice"})

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


def _readable_formats() -> str:
    return ", ".join(sorted(suffix.lstrip(".") for suffix in document_native.SOURCE_SUFFIXES))


def _no_converter_error(source: str, target: str, suffix: str) -> ToolResult:
    """Explain why nothing on this machine can perform the conversion."""
    builtin = (
        f"The built-in converter reads {_readable_formats()} "
        f"and writes {', '.join(sorted(document_native.TARGETS))}"
    )
    if suffix.lower() in document_native.SOURCE_SUFFIXES:
        builtin += f", so it cannot produce {target}"
    else:
        builtin += f", so it cannot read {suffix or 'that file'}"
    return ToolResult(
        ok=False,
        error=(
            f"No converter can turn {source} into {target} here. {builtin}, and "
            "LibreOffice (which covers more formats) is not installed."
        ),
        display="No converter available",
    )


def _libreoffice_missing() -> ToolResult:
    return ToolResult(
        ok=False,
        error=(
            "LibreOffice was not found, so this conversion is unavailable. "
            "Install it, or set 'soffice_path' in .surtitle.json (or the "
            "SURTITLE_SOFFICE environment variable) to the soffice binary. "
            "The built-in converter (backend='builtin') needs none of that."
        ),
        display="LibreOffice not found",
    )


def _conversion_result(
    source: str, destination: Path, relative: str, target: str, backend: str
) -> ToolResult:
    extension = SUPPORTED_TARGETS[target][0]
    return ToolResult(
        ok=True,
        data={
            "source": source,
            "path": relative,
            "target": target,
            "bytes": destination.stat().st_size,
            "backend": backend,
        },
        display=f"Converted {source} to {relative}",
        artifacts=[relative] if extension in {"pdf", "xlsx", "csv", "png", "jpg"} else [],
    )


async def convert_document(
    ctx: ToolContext,
    source: str,
    *,
    target: str = "pdf",
    output: str | None = None,
    overwrite: bool = True,
    timeout: float = _DEFAULT_TIMEOUT,
    backend: Backend = "auto",
) -> ToolResult:
    """Convert a document using the built-in converter or LibreOffice.

    ``source`` and ``output`` are project-relative. When ``output`` is omitted,
    the converted file lands beside the source with the new extension.

    ``backend`` selects the engine: ``auto`` (the default) uses the built-in
    converter and falls back to LibreOffice when it is installed and the built-in
    one cannot help; ``builtin`` never shells out; ``libreoffice`` requires it.
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

    chosen = backend.strip().lower() if isinstance(backend, str) else "auto"
    if chosen not in _BACKENDS:
        return ToolResult(
            ok=False,
            error=(f"Unknown backend {backend!r}. Use 'auto', 'builtin' or 'libreoffice'."),
        )

    try:
        source_path = resolve_in_root(ctx.root, source)
    except PathEscapeError as exc:
        return ToolResult(ok=False, error=str(exc), display=f"blocked: {source}")

    if not source_path.absolute.is_file():
        return ToolResult(ok=False, error=f"Source file not found: {source_path.relative}")

    extension, _filter_name = SUPPORTED_TARGETS[target_name]

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

    suffix = source_path.absolute.suffix
    native_capable = document_native.can_convert(suffix, target_name)
    soffice = None if chosen == "builtin" else configured_soffice(ctx.root)

    if chosen == "builtin" and not native_capable:
        return ToolResult(
            ok=False,
            error=(
                f"The built-in converter cannot turn {suffix or 'that file'} into "
                f"{target_name}. It reads {_readable_formats()} and writes "
                f"{', '.join(sorted(document_native.TARGETS))}. Use backend='libreoffice' "
                "or 'auto' to allow LibreOffice to try."
            ),
            display="Unsupported conversion",
        )
    if chosen == "libreoffice" and soffice is None:
        return _libreoffice_missing()

    attempts: list[str] = []
    if chosen == "builtin":
        attempts = ["builtin"]
    elif chosen == "libreoffice":
        attempts = ["libreoffice"]
    elif native_capable:
        attempts = ["builtin"] + (["libreoffice"] if soffice is not None else [])
    elif soffice is not None:
        attempts = ["libreoffice"]
    else:
        return _no_converter_error(source_path.relative, target_name, suffix)

    failure = ""
    for attempt in attempts:
        if attempt == "libreoffice" and soffice is not None:
            # Always the last attempt, so its detailed error is the one reported.
            return await _convert_with_libreoffice(
                source_path.absolute,
                source_path.relative,
                destination,
                target_name,
                soffice,
                timeout,
            )
        try:
            document_native.convert(source_path.absolute, target_name, destination.absolute)
        except document_native.NativeConversionError as exc:
            # Falling back is the whole point of `auto`, so a built-in failure is
            # recorded rather than returned.
            failure = str(exc)
            log.info("built-in conversion of %s failed: %s", source_path.relative, exc)
            continue
        return _conversion_result(
            source_path.relative, destination.absolute, destination.relative, target_name, "builtin"
        )

    return ToolResult(
        ok=False,
        error=f"Could not convert {source_path.relative} to {target_name}: {failure}",
        display="Conversion failed",
    )


async def _convert_with_libreoffice(
    source: Path,
    source_relative: str,
    destination: ResolvedPath,
    target_name: str,
    soffice: Path,
    timeout: float,
) -> ToolResult:
    """Run LibreOffice headless, staging the output before moving it into place.

    LibreOffice ignores the requested output filename: ``--convert-to`` always
    writes ``<source stem>.<ext>`` into ``--outdir``. Asking for
    ``reports/summary.pdf`` from ``note.txt`` therefore produces ``note.pdf``
    and, without this staging step, the conversion appears to fail. Staging also
    means a failed run cannot leave a half-written file at the destination.
    """
    extension, filter_name = SUPPORTED_TARGETS[target_name]
    destination_path = destination.absolute
    relative = destination.relative

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
                str(source),
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

        produced = _find_output(staging_dir, source.stem, extension)

        if produced is None:
            # Report LibreOffice's own output, which names the real cause
            # (missing filter, corrupt file, unsupported format).
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            detail = [line for line in detail if "Task policy set failed" not in line]
            return ToolResult(
                ok=False,
                error=(
                    f"LibreOffice did not produce a .{extension} file from "
                    f"{source_relative}. " + (detail[-1] if detail else "No output written.")
                ),
                display="Conversion failed",
            )

        try:
            if destination_path.exists():
                destination_path.unlink()
            shutil.move(str(produced), str(destination_path))
        except OSError as exc:
            return ToolResult(ok=False, error=f"Could not place the converted file: {exc}")

    return _conversion_result(
        source_relative, destination_path, relative, target_name, "libreoffice"
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
