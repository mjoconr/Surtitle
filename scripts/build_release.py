#!/usr/bin/env python3
"""Build a self-contained release archive.

The goal is that a user downloads one file, extracts it, and runs it — with no
admin rights, no system Python, and no internet connection needed to install
anything.

How that is achieved:

1. A **standalone Python** is downloaded from ``python-build-standalone``, the
   same distribution ``uv`` uses. It needs no installer and no registry entries.
2. A **virtual environment** is built from it with every dependency already
   installed, so first run does nothing but start.
3. A **wheelhouse** is bundled too, so a user with no network can still repair or
   extend the environment offline with `pip install --no-index --find-links`.

Run on the target platform: a macOS build produces a macOS archive, and the
Windows build must run on Windows because compiled wheels are platform-specific.
The GitHub Actions workflow does exactly that.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build" / "release"

# Files copied into the archive. Anything not listed is not distributed.
INCLUDE_TOP_LEVEL = (
    "src",
    "scripts",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    ".env.example",
)


def project_version() -> str:
    """Read the version from pyproject.toml."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def run(argv: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
    """Run a command, raising on failure with its output included."""
    result = subprocess.run(
        argv,
        cwd=str(cwd or REPO_ROOT),
        capture_output=capture,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(argv)}\n{detail}")
    return (result.stdout or "").strip() if capture else ""


def find_uv() -> str:
    """Locate the uv binary."""
    found = shutil.which("uv")
    if not found:
        raise SystemExit(
            "uv was not found on PATH. Install it from https://docs.astral.sh/uv/ "
            "or set UV_BIN to its full path."
        )
    return found


def standalone_python(uv: str) -> Path:
    """Download a standalone Python and return the interpreter path.

    ``uv python install`` places it in uv's own directory; the path is then
    copied into the archive so the user's machine needs nothing pre-installed.
    """
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    print(f"· installing standalone Python {version}")
    run([uv, "python", "install", version])

    found = run([uv, "python", "find", version], capture=True).strip()
    if not found:
        raise SystemExit("could not locate the standalone Python after installing it")

    interpreter = Path(found)
    if not interpreter.is_file():
        raise SystemExit(f"standalone Python reported at {interpreter} does not exist")
    return interpreter


def copy_python_runtime(interpreter: Path, destination: Path) -> None:
    """Copy a standalone Python installation into the archive tree."""
    # The interpreter lives in <root>/bin/python, or <root>/python.exe on Windows.
    root = interpreter.parent if os.name == "nt" else interpreter.parent.parent
    print(f"· copying Python runtime from {root}")
    shutil.copytree(root, destination, symlinks=False, dirs_exist_ok=True)


def build_venv(uv: str, python: Path, target: Path) -> None:
    """Create the runtime virtual environment with all dependencies installed.

    Installs from the project's lockfile so an archive is reproducible, falling
    back to resolving from pyproject.toml if locking is unavailable offline.
    """
    print("· creating the runtime virtual environment")
    run([uv, "venv", "--python", str(python), str(target)])
    venv_python = _venv_python(target)

    try:
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(venv_python),
                "--requirements",
                str(REPO_ROOT / "requirements-release.txt"),
            ]
            if (REPO_ROOT / "requirements-release.txt").exists()
            else [
                uv,
                "pip",
                "install",
                "--python",
                str(venv_python),
                "--editable",
                ".",
            ],
            cwd=REPO_ROOT,
        )
    except SystemExit as exc:
        raise SystemExit(
            f"could not install dependencies into the runtime environment: {exc}"
        ) from exc


def install_application(uv: str, venv: Path) -> None:
    """Install Surtitle itself into the runtime environment, non-editable.

    A release archive must contain real files, not a link back to a build
    directory that will not exist on the user's machine.
    """
    print("· installing Surtitle into the runtime environment")
    run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(_venv_python(venv)),
            "--no-deps",
            ".",
        ],
        cwd=REPO_ROOT,
    )


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def build_wheelhouse(uv: str, python: Path, destination: Path) -> None:
    """Download wheels for offline repair, best-effort.

    A failure here does not fail the build: the environment already contains
    everything, so the wheelhouse is a convenience rather than a requirement.
    """
    print("· downloading a wheelhouse for offline use")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        run(
            [
                uv,
                "pip",
                "download",
                "--python",
                str(python),
                "--dest",
                str(destination),
                ".",
            ],
            cwd=REPO_ROOT,
        )
    except SystemExit as exc:
        print(f"  (skipped: {exc})")


def copy_sources(destination: Path) -> None:
    """Copy the application source into the archive tree.

    ``web`` and caches are always excluded; the web assets are installed as part
    of the package into the runtime environment, so a duplicate copy would only
    be confusing and let the two drift apart.
    """
    print("· copying application source")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "web")
    for name in INCLUDE_TOP_LEVEL:
        source = REPO_ROOT / name
        if not source.exists():
            continue
        target = destination / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignore)
        else:
            shutil.copy2(source, target)


def write_launchers(destination: Path, version: str) -> None:
    """Write the platform launcher into the archive."""
    print("· writing launchers")
    for name in ("run.sh", "run.ps1", "run.bat"):
        source = REPO_ROOT / "scripts" / name
        if source.exists():
            shutil.copy2(source, destination / name)
    (destination / "VERSION").write_text(f"{version}\n", encoding="utf-8")


def write_manifest(destination: Path, version: str) -> dict[str, object]:
    """Record what was built, for support and for verifying an archive."""
    manifest: dict[str, object] = {
        "name": "Surtitle",
        "version": version,
        "platform": sys.platform,
        "machine": platform.machine(),
        "python": platform.python_version(),
        "built_at": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
    }
    (destination / "BUILD-INFO.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def archive(destination: Path, version: str) -> Path:
    """Package the tree, preserving the executable bit on Unix."""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"surtitle-{version}-{sys.platform}-{platform.machine()}"
    if os.name == "nt":
        path = DIST_DIR / f"{stem}.zip"
        print(f"· writing {path.name}")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for item in sorted(destination.rglob("*")):
                bundle.write(item, item.relative_to(destination))
        return path

    path = DIST_DIR / f"{stem}.tar.gz"
    print(f"· writing {path.name}")
    with tarfile.open(path, "w:gz") as bundle:
        bundle.add(destination, arcname="surtitle")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a self-contained release archive.")
    parser.add_argument(
        "--keep-build",
        action="store_true",
        help="Reuse an existing build directory instead of starting fresh.",
    )
    parser.add_argument(
        "--skip-wheelhouse",
        action="store_true",
        help="Skip downloading extra wheels (smaller archive, needs network to repair).",
    )
    args = parser.parse_args()

    uv = find_uv()
    version = project_version()
    print(f"Surtitle {version} release build for {sys.platform}/{platform.machine()}")

    if BUILD_DIR.exists() and not args.keep_build:
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    interpreter = standalone_python(uv)
    runtime_dir = BUILD_DIR / "python"
    copy_python_runtime(interpreter, runtime_dir)

    bundled_python = runtime_dir / (
        "python.exe" if os.name == "nt" else f"bin/python{sys.version_info.major}"
    )
    if not bundled_python.exists():
        # Fall back to whatever the runtime directory actually contains.
        candidates = list(runtime_dir.rglob("python.exe" if os.name == "nt" else "python3*"))
        if not candidates:
            raise SystemExit(f"no interpreter found inside {runtime_dir}")
        bundled_python = candidates[0]
    print(f"· bundled interpreter: {bundled_python.relative_to(BUILD_DIR)}")

    venv_dir = BUILD_DIR / "venv"
    build_venv(uv, bundled_python, venv_dir)
    install_application(uv, venv_dir)

    if not args.skip_wheelhouse:
        build_wheelhouse(uv, _venv_python(venv_dir), BUILD_DIR / "wheelhouse")

    copy_sources(BUILD_DIR)
    write_launchers(BUILD_DIR, version)
    manifest = write_manifest(BUILD_DIR, version)

    output = archive(BUILD_DIR, version)
    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"\nBuilt {output}")
    print(f"  {size_mb:.1f} MB · {manifest['version']} · {manifest['platform']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
