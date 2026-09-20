#!/usr/bin/env python3
"""Build a self-contained release archive.

The goal is that a user downloads one file, extracts it, and runs it — with no
admin rights, no system Python, and no internet connection needed to install
anything.

How that is achieved:

1. A **standalone Python** is downloaded from ``python-build-standalone``, the
   same distribution ``uv`` uses. It needs no installer and no registry entries.
2. The application's dependencies are installed from ``uv.lock``, so the archive
   carries exactly the versions that were tested. On POSIX they go into a virtual
   environment beside the runtime; on Windows they go into the runtime itself,
   because a Windows venv cannot be relocated.
3. A **wheelhouse** is bundled too, so a user with no network can still repair or
   extend the environment offline with `pip install --no-index --find-links`.

Local voice is optional in two independent halves, and the flags say which:

* ``--with-voice-local`` installs the ``sherpa-onnx`` runtime into the archive, so
  the local engines work without a network at all. It adds roughly 30 MB.
* ``--with-local-models`` downloads the speech models (~90 MB) into ``models/``
  inside the archive, so an extracted archive can run fully offline out of the
  box. Without it, the app downloads whatever models the user asks for on first
  use — which keeps the archive small, and is the default.

``--thin`` is the other way round: it ships the sources and the launcher and
**nothing executable**, and ``run.sh`` fetches Python and the dependencies with
``uv`` on first run. That is for macOS, where a download carrying compiled
binaries is refused by Gatekeeper one dialog at a time: the archive arrives with
``com.apple.quarantine`` and the bundle has no Developer ID signature, so the
interpreter, every extension module and the launcher are each blocked with a
message about malware. A thin archive contains no Mach-O at all — shell scripts,
Python sources and text run under Apple-signed interpreters — and everything
``uv`` fetches afterwards is downloaded by a program rather than by a browser, so
it does not carry the flag either. ``docs/MACOS.md`` is the long version.

Run on the target platform: a macOS build produces a macOS archive, and the
Windows build must run on Windows because compiled wheels are platform-specific —
except ``--thin``, which has no compiled wheels in it at all.
The GitHub Actions workflow does exactly that.
"""

from __future__ import annotations

import argparse
import contextlib
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
    # The double-clickable setup, so an extracted archive can add a Start Menu
    # entry, a ~/Applications app and an optional sign-in entry without anyone
    # needing a terminal. It detects the archive and skips the Python work the
    # bundled runtime already did.
    "Setup.bat",
    "Setup.command",
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


def _interpreter_candidates(root: Path, windows: bool) -> tuple[Path, ...]:
    """Where an interpreter can sit, for a venv or for a bundled runtime.

    Windows puts a virtual environment's interpreter in ``Scripts/`` while a
    bundled runtime keeps it at the top level, so both are checked. Missing the
    first one is what made archive verification run the raw runtime on Windows.
    """
    if windows:
        return (
            root / "Scripts" / "python.exe",
            root / "python.exe",
            root / "bin" / "python.exe",
        )
    major, minor = sys.version_info.major, sys.version_info.minor
    return (
        root / "bin" / f"python{major}.{minor}",
        root / "bin" / "python3",
        root / "bin" / "python",
    )


def _interpreter_in(root: Path, *, windows: bool | None = None) -> Path | None:
    """Find the interpreter inside a Python installation root.

    ``windows`` overrides the platform, so the layout that matters on Windows can
    be exercised by a test running anywhere.
    """
    on_windows = os.name == "nt" if windows is None else windows
    for candidate in _interpreter_candidates(root, on_windows):
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

    # uv marks the interpreters it manages as externally managed, and a copy keeps
    # the marker. This copy belongs to the application and exists to be installed
    # into, so the protection is only an obstacle here.
    for marker in destination.rglob("EXTERNALLY-MANAGED"):
        marker.unlink()

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


def export_lock_requirements(uv: str, destination: Path, *, voice_local: bool = False) -> Path:
    """Write the locked dependency set out as a requirements file.

    Installing from `uv.lock` is the point: resolving from pyproject.toml instead
    would let two archives built a week apart contain different libraries, and the
    lock is the only record of what was actually tested. `--no-emit-project` keeps
    the application itself out, because it is installed separately and must not be
    an editable link back to the build machine.
    """
    print("· exporting the locked dependencies")
    argv = [
        uv,
        "export",
        "--frozen",
        "--no-hashes",
        "--no-emit-project",
        "--output-file",
        str(destination),
    ]
    if voice_local:
        argv += ["--extra", "voice-local"]
    try:
        run(argv, cwd=REPO_ROOT)
    except SystemExit as exc:
        raise SystemExit(f"could not export the locked dependencies: {exc}") from exc

    if not destination.is_file() or not destination.read_text(encoding="utf-8").strip():
        raise SystemExit(
            "the exported requirements file is empty, so the archive would contain "
            "no dependencies at all"
        )
    pinned = sum(
        1
        for line in destination.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    print(f"  {pinned} pinned requirement(s)")
    return destination


def prepare_environment(
    uv: str,
    runtime_python: Path,
    workspace: Path,
    requirements: Path,
    *,
    voice_local: bool = False,
) -> Path:
    """Install the app's dependencies and return the interpreter that runs them.

    ``runtime_python`` is the interpreter of the bundled runtime, which the caller
    has already copied and proved runs. ``requirements`` is the lock export, so the
    archive carries exactly the tested versions.

    On POSIX the returned interpreter lives in a virtual environment beside that
    runtime. On Windows it is the runtime itself, because a Windows venv cannot be
    moved: ``Scripts\\python.exe`` is a launcher that reads an absolute ``home``
    out of ``pyvenv.cfg`` and refuses to start once that path is gone — which is
    precisely what happens to an extracted archive. The bundled runtime is
    self-contained and relocates with the tree, so installing into it is both
    simpler and the only thing that works.
    """
    if voice_local:
        print("· including the local speech engines (sherpa-onnx)")

    if os.name == "nt":
        print("· installing into the bundled runtime (a Windows venv cannot be moved)")
        interpreter = runtime_python
    else:
        print("· creating the runtime virtual environment")
        # --relocatable matters: without it the venv's `bin/python` is an absolute
        # symlink back into the build directory, so the extracted archive only runs
        # on the machine that built it (and Python 3.12+ refuses to extract the
        # absolute link at all).
        target = workspace / "venv"
        run([uv, "venv", "--relocatable", "--python", str(runtime_python), str(target)])
        _relativise_interpreter_links(target)
        interpreter = _venv_python(target)

    try:
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(interpreter),
                "--requirements",
                str(requirements),
            ],
            cwd=REPO_ROOT,
        )
    except SystemExit as exc:
        raise SystemExit(
            f"could not install dependencies into the runtime environment: {exc}"
        ) from exc
    return interpreter


def install_application(uv: str, interpreter: Path) -> None:
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
            str(interpreter),
            "--no-deps",
            ".",
        ],
        cwd=REPO_ROOT,
    )


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _relativise_interpreter_links(venv: Path) -> None:
    """Point the venv's interpreter symlinks at a path inside the archive.

    ``uv venv --relocatable`` makes the console scripts and ``pyvenv.cfg``
    portable but still writes ``bin/python`` as an absolute symlink into the build
    directory. That link dangles as soon as the archive is extracted anywhere
    else — the app then cannot be imported at all — and Python 3.12+ refuses to
    extract such an archive in the first place. The bundled runtime travels
    inside the archive, so the link only has to be relative to it.
    """
    if os.name == "nt":
        # Windows virtual environments copy the interpreter instead of linking.
        return
    for link in sorted((venv / "bin").glob("python*")):
        if not link.is_symlink():
            continue
        target = Path(os.readlink(link))
        if not target.is_absolute():
            continue
        link.unlink()
        link.symlink_to(os.path.relpath(target, link.parent))


def build_wheelhouse(runtime_python: Path, requirements: Path, destination: Path) -> None:
    """Download the pinned wheels, so the archive can be repaired without network.

    Uses the bundled runtime's own pip. uv has no `pip download` at all — the
    previous call to one silently did nothing on every platform — and the POSIX
    virtual environment is not seeded with pip, so the runtime is the one
    interpreter guaranteed to have it.

    A failure does not fail the build: the environment already contains
    everything, so this is a convenience rather than a requirement. It is
    reported rather than swallowed, because a wheelhouse that is quietly missing
    is worse than one that is loudly missing.
    """
    print("· downloading a wheelhouse for offline use")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        run(
            [
                str(runtime_python),
                "-m",
                "pip",
                "download",
                "--dest",
                str(destination),
                "--requirement",
                str(requirements),
                "--disable-pip-version-check",
                "--no-input",
            ],
            cwd=REPO_ROOT,
        )
    except SystemExit as exc:
        print(f"  wheelhouse not built: {exc}")
        return
    wheels = [item for item in destination.iterdir() if item.is_file()]
    total = sum(item.stat().st_size for item in wheels)
    print(f"  wheelhouse: {len(wheels)} file(s), {total / (1024 * 1024):.0f} MB")


def prefetch_models(interpreter: Path, destination: Path) -> None:
    """Download the local speech models into the archive tree.

    Run against the *archive's* interpreter, so the download uses the same model
    registry the shipped app will use. A failure is fatal here rather than
    skipped: a build that claims to include models and does not is worse than one
    that refuses to build.
    """
    print("· downloading local speech models into the archive")
    destination.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["SURTITLE_MODELS_DIR"] = str(destination)
    result = subprocess.run(
        [str(interpreter), "-m", "surtitle", "models", "download", "--yes"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise SystemExit(
            "could not download the local speech models:\n  "
            + ("\n  ".join(detail[-5:]) if detail else "no output")
        )
    total = sum(path.stat().st_size for path in destination.rglob("*") if path.is_file())
    print(f"  models: {total / (1024 * 1024):.0f} MB")


def copy_sources(destination: Path, *, include_web: bool = False) -> None:
    """Copy the application source into the archive tree.

    Caches are always excluded. ``web`` is excluded too by default: a bundled
    archive has the package installed into its environment, web assets and all, so
    a second copy here could only drift from it.

    A thin archive is the opposite case and passes ``include_web=True``: it has no
    installed package, and the environment ``uv`` builds for it is built *from this
    tree*, so leaving the interface behind would ship an application whose UI
    answers 404.
    """
    print("· copying application source")
    patterns = ["__pycache__", "*.pyc", ".pytest_cache"]
    if not include_web:
        patterns.append("web")
    ignore = shutil.ignore_patterns(*patterns)
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


def write_manifest(
    destination: Path, version: str, *, runtime: str = "bundled"
) -> dict[str, object]:
    """Record what was built, for support and for verifying an archive.

    ``runtime`` is ``"bundled"`` for an archive that carries its own interpreter
    and ``"fetched"`` for a thin one, where the launcher installs it on first run.
    The verifier reads it rather than guessing from the file list, so a tree that
    is missing its runtime is told so instead of being mistaken for a thin archive.
    """
    manifest: dict[str, object] = {
        "name": "Surtitle",
        "version": version,
        "platform": sys.platform,
        "machine": platform.machine(),
        "runtime": runtime,
        "python": platform.python_version() if runtime == "bundled" else "fetched by uv",
        "built_at": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
    }
    (destination / "BUILD-INFO.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def archive_stem(
    version: str, *, platform_name: str | None = None, machine: str | None = None
) -> str:
    """The archive's name without its suffix.

    The *machine* is in the name because the archive carries its own interpreter:
    an arm64 build does not run on an Intel Mac, and `surtitle.selfupdate` reads
    this name to refuse one. Both ends have to agree on the spelling, so it lives
    in one function rather than in the caller's memory — and the updater's test
    suite asserts it can still parse what this produces.
    """
    return f"surtitle-{version}-{platform_name or sys.platform}-{machine or platform.machine()}"


def archive(destination: Path, version: str) -> Path:
    """Package the tree, preserving the executable bit on Unix."""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    stem = archive_stem(version)
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


# Every architecture a release ships, as it appears in the archive's name. One
# build job per entry in the release workflow, and this tuple is what says a
# release is complete.
#
# It exists because losing one is silent: a matrix entry that stops building — a
# renamed runner label, a typo in a path — leaves a release whose run is green and
# whose assets look plausible, and the platform it dropped is told there is no
# build for it. Keeping the list here rather than in the workflow means the rule
# can be tested, which is the same reason the layout rules live here.
RELEASE_ARCHITECTURES = (
    ("win32", "AMD64", ".zip"),
    ("darwin", "arm64", ".tar.gz"),
    ("darwin", "x86_64", ".tar.gz"),
)


def expected_archives(version: str) -> list[str]:
    """The archive filename each supported architecture must produce."""
    return [
        f"{archive_stem(version, platform_name=name, machine=machine)}{suffix}"
        for name, machine, suffix in RELEASE_ARCHITECTURES
    ]


def assert_all_archives_present(directory: Path | None = None, version: str = "") -> None:
    """Refuse a release that is missing a platform, naming what is missing.

    Called by the publish job before it publishes and by nothing else: the build
    job makes one archive and cannot know about the others.
    """
    where = directory if directory is not None else DIST_DIR
    wanted = expected_archives(version or project_version())
    missing = [name for name in wanted if not (where / name).is_file()]
    if missing:
        raise SystemExit(
            "this release is missing a build for: "
            + ", ".join(missing)
            + f" — check that every job in RELEASE_ARCHITECTURES ran and produced a file in {where}"
        )
    print(f"· all {len(wanted)} architectures present")


def archive_platform(root: Path, *, fallback: str | None = None) -> str:
    """Which platform an extracted archive targets.

    Read from ``BUILD-INFO.json`` rather than from the machine doing the
    verifying, so a Windows archive can be checked anywhere and the check is
    about the archive rather than about whoever is looking at it.
    """
    manifest = root / "BUILD-INFO.json"
    if manifest.is_file():
        with contextlib.suppress(OSError, ValueError):
            recorded = str(json.loads(manifest.read_text(encoding="utf-8")).get("platform") or "")
            if recorded.strip():
                return recorded.strip()
    return fallback if fallback is not None else sys.platform


def archive_runtime(root: Path) -> str:
    """Whether an extracted archive carries its interpreter or fetches one.

    Read from ``BUILD-INFO.json``, like the platform, so the check is about the
    archive rather than about the machine looking at it. An archive with no
    manifest at all is reported as ``"bundled"``: the missing manifest is then
    caught by the checks that follow instead of being explained away here.
    """
    manifest = root / "BUILD-INFO.json"
    if manifest.is_file():
        with contextlib.suppress(OSError, ValueError):
            recorded = str(json.loads(manifest.read_text(encoding="utf-8")).get("runtime") or "")
            if recorded.strip():
                return recorded.strip()
    return "bundled"


def assert_archive_layout(root: Path, platform_name: str, runtime: str = "bundled") -> None:
    """Assert the extracted tree is the layout its launchers expect.

    The two platforms differ deliberately: a Windows virtual environment records
    an absolute base path in ``pyvenv.cfg`` and cannot be relocated, so its
    dependencies are installed into the bundled runtime instead. The release
    workflow used to assert one layout for both and failed every Windows build;
    keeping the rule here means the build and every workflow share it.

    A thin archive is asserted the other way round: it must *not* contain an
    interpreter. Shipping one inside an archive whose whole purpose is to have no
    binaries would put the user straight back in front of the Gatekeeper dialogs.
    """
    launcher = "run.bat" if platform_name == "win32" else "run.sh"
    if not (root / launcher).is_file():
        raise SystemExit(f"archive has no {launcher} at its root: {root}")

    if runtime == "fetched":
        for name in ("venv", "python"):
            if (root / name).exists():
                raise SystemExit(
                    f"a thin archive must not carry {name}/: the point of it is that "
                    "the download contains no compiled binaries for macOS to refuse"
                )
        for name in ("pyproject.toml", "uv.lock"):
            if not (root / name).is_file():
                raise SystemExit(
                    f"a thin archive needs {name} at its root: the launcher installs "
                    "the environment from it on first run"
                )
        if not (root / "src" / "surtitle" / "__init__.py").is_file():
            raise SystemExit(f"a thin archive needs the application source, absent in {root}")
        return

    if platform_name == "win32":
        if (root / "venv").is_dir():
            raise SystemExit(
                "a Windows archive must not contain venv/: a Windows virtual "
                "environment names an absolute base path in pyvenv.cfg and stops "
                "working once the archive is extracted anywhere else"
            )
        interpreter = root / "python" / "python.exe"
    else:
        interpreter = root / "venv" / "bin" / "python"

    if not interpreter.is_file():
        raise SystemExit(f"archive has no interpreter at {interpreter}")


def verify_thin_tree(root: Path, extracted: Path, *, smoke: bool) -> None:
    """Check a thin archive: the tree is complete, and optionally that it installs.

    There is no interpreter to run, so the checks are about the tree the launcher
    installs *from*: the sources the package needs, the lock the versions come
    from, the launcher itself, and that every Python file in it still parses.
    That is offline, which is what ``--verify-only`` needs.

    ``smoke`` additionally runs the launcher as a user would, which fetches the
    environment with ``uv`` and starts the application. Only the build does that:
    it is the one check that proves the artifact works from nothing, and it is the
    reason a thin archive is verified on the runner rather than by inspection. It
    is skipped when ``uv`` is not on PATH, because a fetcher that is not installed
    cannot be blamed on the archive.
    """
    print("· verifying a thin archive: sources and launcher, no bundled binaries")

    launcher = root / "run.sh"
    if not os.access(launcher, os.X_OK):
        # tar preserves the bit; a launcher that arrives without it is a download
        # that cannot be started at all, which is worth failing a release over.
        raise SystemExit(f"{launcher} is not executable: `./run.sh` would not start")

    required = {
        "src/surtitle/__init__.py": "the package",
        "src/surtitle/server.py": "the server",
        "src/surtitle/web/index.html": "the interface",
        "src/surtitle/web/js/app.js": "the interface script",
        "pyproject.toml": "the project metadata uv installs from",
        "uv.lock": "the pinned versions",
    }
    for relative, why in required.items():
        if not (root / relative).is_file():
            raise SystemExit(f"thin archive is missing {relative} ({why})")

    version = (
        (root / "VERSION").read_text(encoding="utf-8").strip()
        if (root / "VERSION").is_file()
        else ""
    )
    declared = project_version()
    if version != declared:
        raise SystemExit(f"thin archive says {version!r} but the project is {declared!r}")

    python = next(
        (candidate for candidate in ("python3", "python") if shutil.which(candidate)), None
    )
    if python is None:
        raise SystemExit("no python on this machine to syntax-check the archive with")
    compiled = subprocess.run(
        [python, "-m", "compileall", "-q", str(root / "src" / "surtitle")],
        capture_output=True,
        text=True,
    )
    if compiled.returncode != 0:
        detail = (compiled.stderr or compiled.stdout or "").strip().splitlines()
        raise SystemExit(
            "thin archive verification failed: the sources do not compile\n"
            f"  {detail[-1] if detail else 'no output'}"
        )
    print("  ✓ every source file still compiles")

    shell = shutil.which("bash")
    if shell:
        checked = subprocess.run([shell, "-n", str(launcher)], capture_output=True, text=True)
        if checked.returncode != 0:
            raise SystemExit(f"{launcher} is not valid shell:\n  {checked.stderr.strip()}")
        print("  ✓ the launcher is valid shell")

    if not smoke:
        print("  · skipped installing it: --verify-only does not reach the network")
        return

    uv = find_uv()
    if uv is None:
        print("  · uv is not on PATH, so the first-run install was not exercised")
        return

    print("· installing it the way a user's first run does (uv fetches Python and the wheels)")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    }
    # A scratch home for the app's own data, and the extracted tree for the
    # environment, so nothing resolves back to this build machine.
    environment["SURTITLE_HOME"] = str(extracted / "home")
    result = subprocess.run(
        [shell or "sh", str(launcher), "--version"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(root),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise SystemExit(
            "thin archive verification failed: the launcher could not install and run it\n"
            f"  {detail[-1] if detail else 'no output'}"
        )
    reported = (result.stdout or "").strip().splitlines()
    print(f"  ✓ fetches its runtime on first run and runs ({reported[-1] if reported else 'ok'})")


def verify_archive(
    archive_path: Path, *, expect_voice_local: bool = False, smoke: bool = False
) -> None:
    """Extract the archive and prove it actually runs.

    A release archive is only useful if it starts on a machine with no Python and
    no network, so the build checks that here rather than trusting the layout.
    This has already caught a missing ``__main__`` (broken launchers), a runtime
    copied from the wrong place (broken shared library), and absent web assets
    (a UI that would 404). A thin archive is checked the other way round — see
    :func:`verify_thin_tree` — and ``smoke`` asks for the one check that needs the
    network: installing it from scratch with ``uv``, as a first run does.
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
                # `data` is the default from Python 3.12 on and it refuses
                # absolute links. That refusal is a check rather than an
                # obstacle: a relocatable archive must not contain a link back
                # into the build directory.
                try:
                    bundle.extractall(target, filter="data")
                except TypeError:  # interpreter without extract filters
                    bundle.extractall(target)
                except tarfile.FilterError as exc:
                    raise SystemExit(f"archive is not relocatable: {exc}") from exc

        roots = [entry for entry in target.iterdir() if entry.is_dir()]
        root = roots[0] if len(roots) == 1 and not (target / "VERSION").exists() else target

        # The layout is part of what the user receives, so assert it before the
        # smoke test: a missing launcher, or a Windows archive carrying a venv
        # that cannot be relocated, is broken even if the interpreter happens to
        # run on the machine that built it.
        runtime = archive_runtime(root)
        assert_archive_layout(root, archive_platform(root), runtime)

        if runtime == "fetched":
            verify_thin_tree(root, target, smoke=smoke)
            return

        # Pick the interpreter the archive is meant to run with: a virtual
        # environment when there is one, otherwise the bundled runtime. Windows
        # archives install into the runtime, because a Windows venv cannot be
        # moved — see prepare_environment.
        environment = root / "venv"
        if not environment.is_dir():
            # Some layouts nest everything under a single top-level directory.
            environment = next(
                (
                    candidate / "venv"
                    for candidate in (root, *[p for p in root.iterdir() if p.is_dir()])
                    if (candidate / "venv").is_dir()
                ),
                root / "python",
            )

        interpreter = _interpreter_in(environment)
        if interpreter is None:
            raise SystemExit(f"archive has no usable interpreter in {environment}")

        # On Windows a venv's Scripts\python.exe is only a launcher for the base
        # installation named in pyvenv.cfg, so a `home` outside the archive means
        # the extracted copy cannot start anywhere else. POSIX is unaffected: its
        # interpreter is a symlink the check above already covers, and CPython
        # finds the base prefix through it rather than through `home`.
        config = environment / "pyvenv.cfg"
        if os.name == "nt" and config.is_file():
            for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "home" and value.strip():
                    base = Path(value.strip()).resolve()
                    if target.resolve() not in base.parents:
                        raise SystemExit(
                            f"archive is not relocatable: {config} names a base "
                            f"installation at {base}, which is outside the archive"
                        )

        # Verification runs on the machine that built the archive, so a link that
        # still points back into the build directory resolves here and dangles for
        # everyone else — which is exactly how a broken archive ships. Assert the
        # interpreter lives inside the extracted tree instead.
        resolved = Path(interpreter).resolve()
        if target.resolve() not in resolved.parents:
            raise SystemExit(
                f"archive is not relocatable: {interpreter} resolves to {resolved}, "
                f"which is outside the extracted tree"
            )

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

        if expect_voice_local:
            # Importing the engines is the cheap half; constructing a recogniser
            # config proves the bundled wheel matches the ONNX runtime it shipped
            # with, which is the failure a wheel/ABI break produces. No model file
            # is needed for that, so this stays fast.
            checks += (
                (
                    "bundles the local voice engines",
                    [
                        str(interpreter),
                        "-c",
                        "import sherpa_onnx, surtitle.voice.local_stt,"
                        " surtitle.voice.local_tts;"
                        " sherpa_onnx.OnlineRecognizer;"
                        " print('sherpa-onnx', getattr(sherpa_onnx, '__version__', '?'))",
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
    # A Windows console defaults to a legacy code page that cannot encode the tick
    # marks printed while verifying, so a *successful* build ended in
    # UnicodeEncodeError. Ask for UTF-8 and tolerate anything still unmappable:
    # the output should never be the reason a build fails.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")

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
    parser.add_argument(
        "--verify-only",
        nargs="*",
        default=None,
        metavar="ARCHIVE",
        help="Verify existing archives instead of building; defaults to everything in "
        "dist/. This is the same verifier the build runs, so a release workflow can "
        "re-check an artifact without a second, drifting copy of the rules.",
    )
    parser.add_argument(
        "--with-voice-local",
        action="store_true",
        help="Bundle the local speech engines (sherpa-onnx, ~30 MB).",
    )
    parser.add_argument(
        "--with-local-models",
        action="store_true",
        help="Bundle the speech models too (~90 MB), for a fully offline archive. "
        "Implies --with-voice-local.",
    )
    parser.add_argument(
        "--thin",
        action="store_true",
        help="Ship the sources and the launcher only, with no Python runtime and no "
        "compiled modules: run.sh fetches them with uv on first run. For macOS, "
        "where a download carrying unsigned binaries is refused by Gatekeeper.",
    )
    parser.add_argument(
        "--check-archives",
        nargs="?",
        const="",
        default=None,
        metavar="VERSION",
        help="Assert that every supported architecture has an archive, and exit. "
        "Defaults to the packaged version. This is the last gate before a release "
        "is published: a missing platform is invisible otherwise.",
    )
    parser.add_argument(
        "--archives-dir",
        default=None,
        metavar="DIR",
        help="Where the archives are, for --check-archives (default: dist/). The "
        "release workflow downloads them into another directory first.",
    )
    args = parser.parse_args()

    if args.check_archives is not None:
        where = Path(args.archives_dir) if args.archives_dir else None
        assert_all_archives_present(where, version=args.check_archives)
        return 0

    if args.verify_only is not None:
        # Runs before any build work, and before uv is looked for, so an
        # existing download can be checked on a machine with no toolchain.
        archives = [Path(item) for item in args.verify_only]
        if not archives:
            archives = sorted(DIST_DIR.glob("*.zip")) + sorted(DIST_DIR.glob("*.tar.gz"))
        if not archives:
            raise SystemExit(f"no archives to verify in {DIST_DIR}")
        for path in archives:
            if not path.is_file():
                raise SystemExit(f"no such archive: {path}")
            verify_archive(path)
        return 0

    if args.with_local_models:
        args.with_voice_local = True

    if args.thin and (args.with_voice_local or args.with_local_models):
        # Not a limitation to work around: a bundle with the engines in it is a
        # bundle with compiled binaries in it, which is the thing this mode exists
        # to avoid. A thin archive installs the engines on first use instead, from
        # the application's own Settings screen.
        raise SystemExit(
            "--thin cannot be combined with --with-voice-local or --with-local-models: "
            "a thin archive has no environment to install them into. It installs the "
            "engines on first use instead, and there is nothing to bundle."
        )

    uv = find_uv()
    version = project_version()
    kind = "thin" if args.thin else "self-contained"
    print(f"Surtitle {version} {kind} release build for {sys.platform}/{platform.machine()}")

    if BUILD_DIR.exists() and not args.keep_build:
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    if args.thin:
        # Nothing is fetched, installed or copied here beyond the tree itself: the
        # launcher installs from it, and building it needs no interpreter of its own.
        copy_sources(BUILD_DIR, include_web=True)
        write_launchers(BUILD_DIR, version)
        manifest = write_manifest(BUILD_DIR, version, runtime="fetched")
        manifest["voice_local"] = False
        manifest["local_models"] = False
        (BUILD_DIR / "BUILD-INFO.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        output = archive(BUILD_DIR, version)
        if not args.skip_verify:
            verify_archive(output, smoke=True)
        size_mb = output.stat().st_size / (1024 * 1024)
        print(f"\nBuilt {output}")
        print(f"  {size_mb:.1f} MB · {manifest['version']} · runtime fetched on first run")
        return 0

    runtime_root = standalone_python(uv)
    runtime_dir = BUILD_DIR / "python"
    bundled_python = copy_python_runtime(runtime_root, runtime_dir)
    print(f"· bundled interpreter: {bundled_python.relative_to(BUILD_DIR)}")

    # The lock export is the one record of what was tested, and both the install
    # and the wheelhouse consume it. It is written beside the build root rather
    # than inside it, because everything inside is shipped.
    requirements = export_lock_requirements(
        uv,
        BUILD_DIR.parent / "requirements-lock.txt",
        voice_local=args.with_voice_local,
    )
    interpreter = prepare_environment(
        uv,
        bundled_python,
        BUILD_DIR,
        requirements,
        voice_local=args.with_voice_local,
    )
    install_application(uv, interpreter)

    if not args.skip_wheelhouse:
        build_wheelhouse(bundled_python, requirements, BUILD_DIR / "wheelhouse")

    if args.with_local_models:
        prefetch_models(interpreter, BUILD_DIR / "models")

    copy_sources(BUILD_DIR)
    write_launchers(BUILD_DIR, version)
    manifest = write_manifest(BUILD_DIR, version)
    manifest["voice_local"] = args.with_voice_local
    manifest["local_models"] = args.with_local_models
    (BUILD_DIR / "BUILD-INFO.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    output = archive(BUILD_DIR, version)
    if not args.skip_verify:
        verify_archive(output, expect_voice_local=args.with_voice_local)

    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"\nBuilt {output}")
    print(f"  {size_mb:.1f} MB · {manifest['version']} · {manifest['platform']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
