"""Replacing an extracted release archive with a newer one, in place.

A downloaded Surtitle is a directory with its own Python inside it. Windows keeps
a running program's files locked, so the directory cannot be swapped while the
server that lives in it is still up. The update therefore runs in three stages:

1. **Stage** (while the server runs): download the release asset for this
   platform, check it against the release's ``SHA256SUMS.txt``, and unpack it
   into a sibling directory ``<install>.new``. Sibling, not the data directory,
   so the final swap is two renames on one filesystem rather than a copy.
2. **Hand over**: write a small updater script and start it detached, then let
   the server stop. The script waits for the process to exit.
3. **Swap** (after the server exits): rename the old directory aside, move the
   staged one into place, and delete the old one. If the move fails the old
   directory is put back, so a failed update leaves a working install rather
   than half of one.

The data directory — settings, database, speech models — lives outside the
install directory, so none of this touches it. That is also why an update never
needs to re-download ~400 MB of models.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from surtitle.config import Settings

__all__ = [
    "PreparedUpdate",
    "ReleaseAsset",
    "asset_for",
    "begin",
    "expected_sums",
    "install_root",
    "stage",
    "supported",
]

MARKER = "BUILD-INFO.json"
REPO_SLUG = "mjoconr/Surtitle"
RELEASES_API = f"https://api.github.com/repos/{REPO_SLUG}/releases"
SUMS_ASSET = "SHA256SUMS.txt"

_CHUNK = 1 << 20
_TIMEOUT = 60.0


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    tag: str
    name: str
    url: str
    size: int = 0


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    """Everything the swap needs, already staged and verified."""

    root: Path
    staged: Path
    script: Path
    relaunch: Path
    tag: str


def install_root() -> Path | None:
    """The extracted archive this code runs from, if it is one.

    Found by looking for the build marker up the tree rather than by counting
    parents: a release installs into the bundled runtime (``python/Lib/...`` on
    Windows, ``python/lib/python3.x/...`` elsewhere), so the depth differs by
    platform and by Python version.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / MARKER).is_file():
            return parent
    return None


def _relaunch_path(root: Path) -> Path:
    """The documented entry point: at the root in an archive, in scripts/ otherwise."""
    for candidate in (
        root / "run.bat",
        root / "run.sh",
        root / "scripts" / "run.bat",
        root / "scripts" / "run.sh",
    ):
        if candidate.is_file():
            return candidate
    return root / ("run.bat" if os.name == "nt" else "run.sh")


def supported(root: Path | None = None) -> bool:
    """True when this installation can replace itself.

    Needs a build marker (so we know what we are replacing) and a writable parent,
    because the staging directory and the swap both happen beside the install.
    """
    root = root if root is not None else install_root()
    if root is None:
        return False
    parent = root.parent
    return os.access(root, os.W_OK) and os.access(parent, os.W_OK)


def _get_json(url: str, *, timeout: float = _TIMEOUT) -> dict[str, Any]:
    import httpx

    response = httpx.get(
        url,
        timeout=timeout,
        headers={"Accept": "application/vnd.github+json"},
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def release(
    tag: str | None = None, *, fetcher: Callable[[str], Any] | None = None
) -> dict[str, Any]:
    """The release to install: a named tag, or the latest published one."""
    fetch = fetcher or _get_json
    url = f"{RELEASES_API}/latest" if tag is None else f"{RELEASES_API}/tags/{tag}"
    try:
        payload = fetch(url)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def asset_for(
    payload: dict[str, Any], *, platform: str | None = None, machine: str | None = None
) -> ReleaseAsset | None:
    """Pick the asset that matches this installation.

    Windows ships a ``.zip``; macOS and Linux ship ``.tar.gz``. Matching the
    platform substring as well keeps a Windows user from being handed the macOS
    build if a future release carries more than one.
    """
    tag = str(payload.get("tag_name") or "").strip()
    here = platform or sys.platform
    arch = (machine or os.uname().machine) if hasattr(os, "uname") else ""
    wanted_suffix = ".zip" if here == "win32" else ".tar.gz"
    wanted_platform = "win32" if here == "win32" else ("darwin" if here == "darwin" else "linux")

    best: ReleaseAsset | None = None
    for item in payload.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        if not name.endswith(wanted_suffix) or wanted_platform not in name or not url:
            continue
        candidate = ReleaseAsset(tag=tag, name=name, url=url, size=int(item.get("size") or 0))
        if arch and arch.lower() in name.lower():
            return candidate
        best = best or candidate
    return best


def expected_sums(
    payload: dict[str, Any], *, fetch_text: Callable[[str], str] | None = None
) -> dict[str, str]:
    """``filename -> sha256`` from the release's published sums, if it has them.

    An empty mapping means the sums could not be read; the caller decides whether
    that is fatal. Here it is: an update that replaces the program you run should
    not skip the only check that it is the program you asked for.
    """
    url = ""
    for item in payload.get("assets") or []:
        if isinstance(item, dict) and str(item.get("name") or "") == SUMS_ASSET:
            url = str(item.get("browser_download_url") or "")
            break
    if not url:
        return {}

    fetch = fetch_text or (lambda target: _get_text(target))
    try:
        text = fetch(url)
    except Exception:
        return {}

    sums: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            sums[parts[-1].lstrip("*")] = parts[0].lower()
    return sums


def _get_text(url: str, *, timeout: float = _TIMEOUT) -> str:
    import httpx

    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.text


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, *, opener: Callable[[str], Any] | None = None) -> None:
    """Stream an asset to disk without holding it in memory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if opener is not None:
        destination.write_bytes(opener(url))
        return

    import httpx

    with httpx.stream("GET", url, timeout=_TIMEOUT, follow_redirects=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(_CHUNK):
                handle.write(chunk)


def _extract(archive: Path, destination: Path) -> None:
    """Unpack an asset and flatten its single top-level directory, if it has one."""
    destination.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)
    else:
        with tarfile.open(archive) as bundle:
            try:
                bundle.extractall(destination, filter="data")
            except TypeError:  # interpreter without extraction filters
                bundle.extractall(destination)

    if (destination / MARKER).is_file():
        return
    entries = list(destination.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in list(inner.iterdir()):
            shutil.move(str(child), str(destination / child.name))
        with contextlib.suppress(OSError):
            inner.rmdir()


def stage(
    settings: Settings,
    payload: dict[str, Any],
    *,
    opener: Callable[[str], Any] | None = None,
    fetch_text: Callable[[str], str] | None = None,
    root: Path | None = None,
    platform: str | None = None,
    machine: str | None = None,
) -> tuple[Path | None, str]:
    """Download, verify and unpack the release. Returns ``(staged_dir, error)``.

    The staged directory is a sibling of the install so the swap is a rename; the
    download itself lands in the data directory so a failed update leaves no
    half-written tree beside a working installation.
    """
    root = root if root is not None else install_root()
    if root is None:
        return None, "this installation has no build marker to replace"
    if not supported(root):
        return None, "the folder Surtitle is installed in is not writable"

    asset = asset_for(payload, platform=platform, machine=machine)
    if asset is None:
        return None, "that release has no build for this platform"

    sums = expected_sums(payload, fetch_text=fetch_text)
    if not sums:
        return None, "the release publishes no checksums, so the download cannot be verified"
    expected = sums.get(asset.name)
    if not expected:
        return None, f"the release publishes no checksum for {asset.name}"

    downloads = settings.data_dir / "updates"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / asset.name
    if archive.exists():
        archive.unlink()

    _download(asset.url, archive, opener=opener)
    actual = sha256_of(archive)
    if actual != expected:
        with contextlib.suppress(OSError):
            archive.unlink()
        return None, f"{asset.name} did not match its published checksum"

    staged = root.parent / (root.name + ".new")
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)
    _extract(archive, staged)
    if not (staged / MARKER).is_file():
        shutil.rmtree(staged, ignore_errors=True)
        return None, f"{asset.name} did not look like a Surtitle build"
    return staged, ""


_UPDATER_SH = """#!/bin/sh
# Written by Surtitle to finish an update after the server has exited.
# args: <server pid> <install dir> <staged dir> <relaunch>
pid="$1"
install="$2"
staged="$3"
relaunch="$4"
backup="${install}.old"

while kill -0 "$pid" 2>/dev/null; do sleep 0.4; done

rm -rf "$backup"
mv "$install" "$backup" || exit 1
if mv "$staged" "$install"; then
  rm -rf "$backup"
else
  mv "$backup" "$install"
  exit 1
fi

if [ -n "$relaunch" ]; then
  "$relaunch" >/dev/null 2>&1 &
fi
exit 0
"""

_UPDATER_PS1 = """# Written by Surtitle to finish an update after the server has exited.
param(
    [Parameter(Mandatory)][int] $ServerPid,
    [Parameter(Mandatory)][string] $Install,
    [Parameter(Mandatory)][string] $Staged,
    [string] $Relaunch = ''
)
$ErrorActionPreference = 'Stop'
$backup = "${Install}.old"

while (Get-Process -Id $ServerPid -ErrorAction SilentlyContinue) {
    Start-Sleep -Milliseconds 400
}

if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
Move-Item -LiteralPath $Install -Destination $backup
try {
    Move-Item -LiteralPath $Staged -Destination $Install
} catch {
    Move-Item -LiteralPath $backup -Destination $Install
    throw
}
Remove-Item -LiteralPath $backup -Recurse -Force
if ($Relaunch) { Start-Process -FilePath $Relaunch }
"""


def write_updater(settings: Settings, staged: Path, root: Path) -> Path:
    """Write the post-exit swap script and return its path."""
    updates = settings.data_dir / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        script = updates / "apply-update.ps1"
        script.write_text(_UPDATER_PS1, encoding="utf-8")
    else:
        script = updates / "apply-update.sh"
        script.write_text(_UPDATER_SH, encoding="utf-8")
        script.chmod(0o755)
    return script


def _launch(script: Path, root: Path, staged: Path, relaunch: Path) -> None:
    """Start the updater detached, so it outlives the server it is replacing."""
    if os.name == "nt":
        creation = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-ServerPid",
                str(os.getpid()),
                "-Install",
                str(root),
                "-Staged",
                str(staged),
                "-Relaunch",
                str(relaunch),
            ],
            creationflags=creation,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    subprocess.Popen(
        ["/bin/sh", str(script), str(os.getpid()), str(root), str(staged), str(relaunch)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def begin(
    settings: Settings,
    payload: dict[str, Any],
    *,
    opener: Callable[[str], Any] | None = None,
    fetch_text: Callable[[str], str] | None = None,
    root: Path | None = None,
) -> tuple[PreparedUpdate | None, str]:
    """Stage the release and hand over to the updater. Returns ``(prepared, error)``.

    On success the caller must stop the server: the updater is already waiting for
    this process to exit before it swaps the directory.
    """
    root = root if root is not None else install_root()
    staged, error = stage(settings, payload, opener=opener, fetch_text=fetch_text, root=root)
    if staged is None or root is None:
        return None, error

    script = write_updater(settings, staged, root)
    _launch(script, root, staged, _relaunch_path(root))
    return PreparedUpdate(
        root=root,
        staged=staged,
        script=script,
        relaunch=_relaunch_path(root),
        tag=str(payload.get("tag_name") or ""),
    ), ""


def describe(payload: dict[str, Any]) -> str:
    """One line naming what would be installed, for a confirmation dialog."""
    tag = str(payload.get("tag_name") or "the newest release").strip()
    asset = asset_for(payload)
    if asset is None:
        return tag
    return f"{tag} ({asset.name})"
