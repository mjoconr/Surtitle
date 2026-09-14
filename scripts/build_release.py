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
    """Install a standalone Python and return its installation root.

    Returns the *root* rather than an interpreter path on purpose.
    ``uv python find`` resolves through the active project and can hand back the
    application's own ``.venv`` interpreter, which would make the "bundled"
    runtime a copy of the very thing we are trying to avoid depending on.
    """
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    print(f"· installing standalone Python {version}")
    run([uv, "python", "install", version])

    install_dir = run([uv, "python", "dir"], capture=True).strip()
    if not install_dir:
        raise SystemExit("`uv python dir` returned nothing")

    # uv names managed installs cpython-<major>.<minor>-<platform>-<arch>-<flavour>.
    prefix = f"cpython-{version}"
    candidates = sorted(
        path
        for path in Path(install_dir).iterdir()
        if path.is_dir() and path.name.startswith(prefix) and _interpreter_in(path) is not None
    )
    if not candidates:
        raise SystemExit(
            f"no standalone Python matching {prefix}* found in {install_dir}. "
            "Run `uv python install` yourself to see the failure."
        )

    root = candidates[-1]
    print(f"· standalone Python root: {root}")
    return root


def _interpreter_in(root: Path) -> Path | None:
    """Find the interpreter inside a Python installation root."""
    major, minor = sys.version_info.major, sys.version_info.minor
    if os.name == "nt":
        candidates = (root / "python.exe", root / "bin" / "python.exe")
    else:
        candidates = (
            root / "bin" / f"python{major}.{minor}",
            root / "bin" / "python3",
            root / "bin" / "python",
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def copy_python_runtime(source_root: Path, destination: Path) -> Path:
    """Copy a standalone Python installation and verify the copy actually runs.

    Verifying matters: a runtime whose shared library did not come along looks
    perfectly fine on disk and only fails later, when the user starts the app.
    """
    print(f"· copying Python runtime from {source_root}")
    shutil.copytree(source_root, destination, symlinks=False, dirs_exist_ok=True)

    interpreter = _interpreter_in(destination)
    if interpreter is None:
        raise SystemExit(f"no interpreter found in the copied runtime at {destination}")

    probe = subprocess.run(
        [str(interpreter), "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        raise SystemExit(
            "the copied Python runtime does not run, so the archive would be broken.\n"
            f"  interpreter: {interpreter}\n"
            f"  output: {detail[-1] if detail else 'no output'}"
        )
    print(f"· bundled runtime verified: {interpreter}")
    return interpreter


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


def verify_archive(archive_path: Path) -> None:
    """Extract the archive and prove the bundled runtime actually runs.

    A release archive is only useful if it starts on a machine with no Python and
    no network, so the build checks that here rather than trusting the layout.
    This has already caught a missing ``__main__`` (broken launchers), a runtime
    copied from the wrong place (broken shared library), and absent web assets
    (a UI that would 404).
    """
    import tarfile
    import tempfile
    import zipfile

    print("· verifying the archive as a user would receive it")
    with tempfile.TemporaryDirectory(prefix="surtitle-verify-") as temp:
        target = Path(temp) / "extracted"
        target.mkdir(parents=True)

        if archive_path.suffix == ".zip":
            with zipfile.ZipFile(archive_path) as bundle:
                bundle.extractall(target)
        else:
            with tarfile.open(archive_path) as bundle:
                bundle.extractall(target)

        roots = [entry for entry in target.iterdir() if entry.is_dir()]
        root = roots[0] if len(roots) == 1 and not (target / "VERSION").exists() else target

        # The venv interpreter must be used: it is the one that can see the
        # installed application. The raw runtime is only a fallback for the
        # layout check.
        interpreter = _interpreter_in(root / "venv") or _interpreter_in(root / "python")
        if interpreter is None:
            # Some layouts nest everything under a single top-level directory.
            interpreter = _interpreter_in(root / "venv") or None
            for candidate_root in (root, *[p for p in root.iterdir() if p.is_dir()]):
                interpreter = _interpreter_in(candidate_root / "venv")
                if interpreter is not None:
                    break
        if interpreter is None:
            raise SystemExit("archive has no usable interpreter (looked for venv/)")

        checks = (
            (
                "imports the application",
                [str(interpreter), "-c", "import surtitle, surtitle.server"],
            ),
            (
                "runs the documented entry point",
                [str(interpreter), "-m", "surtitle", "--version"],
            ),
            (
                "has the web assets",
                [
                    str(interpreter),
                    "-c",
                    "import pathlib, surtitle as a;"
                    " w = pathlib.Path(a.__file__).parent / 'web';"
                    " assert (w / 'index.html').is_file();"
                    " assert (w / 'js' / 'app.js').is_file();"
                    " print('ok')",
                ],
            ),
        )

        # Run in a scratch home with a cleared environment, so nothing resolves
        # back to this build machine.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
        }
        environment["SURTITLE_HOME"] = str(Path(temp) / "home")

        for label, argv in checks:
            result = subprocess.run(
                argv, capture_output=True, text=True, env=environment, cwd=str(Path(temp))
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip().splitlines()
                raise SystemExit(
                    f"archive verification failed: it does not {label}\n"
                    f"  {' '.join(argv)}\n"
                    f"  {detail[-1] if detail else 'no output'}"
                )
            print(f"  ✓ {label}")

    print("· archive verified")


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
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip extracting and smoke-testing the finished archive.",
    )
    args = parser.parse_args()

    uv = find_uv()
    version = project_version()
    print(f"Surtitle {version} release build for {sys.platform}/{platform.machine()}")

    if BUILD_DIR.exists() and not args.keep_build:
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    runtime_root = standalone_python(uv)
    runtime_dir = BUILD_DIR / "python"
    bundled_python = copy_python_runtime(runtime_root, runtime_dir)
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
    if not args.skip_verify:
        verify_archive(output)

    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"\nBuilt {output}")
    print(f"  {size_mb:.1f} MB · {manifest['version']} · {manifest['platform']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
