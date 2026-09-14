"""Environment diagnostics.

``surtitle doctor`` is the first thing a new user should run: it proves the
install is complete and names the exact fix for anything missing, rather than
letting the app fail later inside a WebSocket handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import platform
import sys
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from surtitle.config import APP_NAME, Settings
from surtitle.platform_utils import port_is_free
from surtitle.voice.stt import listen_url
from surtitle.voice.tts import speak_url

__all__ = ["Check", "CheckStatus", "format_report", "run_checks"]


class CheckStatus(StrEnum):
    """Outcome of a single diagnostic."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(slots=True)
class Check:
    """One diagnostic result."""

    name: str
    status: CheckStatus
    detail: str
    fix: str | None = None

    @property
    def ok(self) -> bool:
        """True when this check does not block startup."""
        return self.status in {CheckStatus.OK, CheckStatus.WARN, CheckStatus.SKIP}


@dataclass(slots=True)
class Report:
    """The full diagnostic run."""

    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]

    @property
    def ok(self) -> bool:
        return not self.failed


# Packages the agent and the artifact tools need.
_REQUIRED_IMPORTS = (
    "fastapi",
    "uvicorn",
    "httpx",
    "websockets",
    "pydantic",
    "pypdf",
    "reportlab",
    "openpyxl",
)

# Optional imports that unlock extra tool capability.
_OPTIONAL_IMPORTS = (
    "matplotlib",
    "pandas",
    "docx",
)


def _check_python() -> Check:
    version = platform.python_version()
    if sys.version_info < (3, 11):  # noqa: UP036 - runtime guard, see pyproject
        return Check(
            "Python version",
            CheckStatus.FAIL,
            f"{version} is too old",
            "Install Python 3.11 or newer, then re-run `uv sync`.",
        )
    return Check("Python version", CheckStatus.OK, version)


def _check_dependencies() -> list[Check]:
    checks: list[Check] = []
    missing = [name for name in _REQUIRED_IMPORTS if importlib.util.find_spec(name) is None]
    if missing:
        checks.append(
            Check(
                "Core dependencies",
                CheckStatus.FAIL,
                f"missing: {', '.join(missing)}",
                "Run `uv sync` (or `pip install -e .`) in the project directory.",
            )
        )
    else:
        checks.append(
            Check("Core dependencies", CheckStatus.OK, f"{len(_REQUIRED_IMPORTS)} packages present")
        )

    absent_optional = [n for n in _OPTIONAL_IMPORTS if importlib.util.find_spec(n) is None]
    if absent_optional:
        checks.append(
            Check(
                "Optional extras",
                CheckStatus.SKIP,
                f"not installed: {', '.join(absent_optional)}",
                "Run `uv sync --extra extras` to let the agent use pandas/python-docx.",
            )
        )
    else:
        checks.append(Check("Optional extras", CheckStatus.OK, ", ".join(_OPTIONAL_IMPORTS)))
    return checks


def _check_data_dir(settings: Settings) -> Check:
    try:
        settings.ensure_data_dir()
        probe = settings.data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            "Data directory",
            CheckStatus.FAIL,
            f"not writable: {settings.data_dir} ({exc})",
            "Set SURTITLE_HOME to a writable directory.",
        )
    return Check("Data directory", CheckStatus.OK, str(settings.data_dir))


def _check_keys(settings: Settings) -> list[Check]:
    checks: list[Check] = []

    deepseek = settings.deepseek_key()
    if deepseek:
        checks.append(Check("DeepSeek key", CheckStatus.OK, f"present ({_mask(deepseek)})"))
    else:
        checks.append(
            Check(
                "DeepSeek key",
                CheckStatus.FAIL,
                "DEEPSEEK_API_KEY is not set",
                "Run `surtitle init`, or set it in Settings inside the app.",
            )
        )

    deepgram = settings.deepgram_key()
    if deepgram:
        checks.append(Check("Deepgram key", CheckStatus.OK, f"present ({_mask(deepgram)})"))
    elif settings.voice_enabled:
        checks.append(
            Check(
                "Deepgram key",
                CheckStatus.FAIL,
                "DEEPGRAM_API_KEY is not set",
                "Run `surtitle init`, or start with --text-only to skip voice.",
            )
        )
    else:
        checks.append(Check("Deepgram key", CheckStatus.SKIP, "voice disabled"))
    return checks


def _check_port(settings: Settings) -> Check:
    if port_is_free(settings.host, settings.port):
        return Check("Port", CheckStatus.OK, f"{settings.host}:{settings.port} is free")
    return Check(
        "Port",
        CheckStatus.WARN,
        f"{settings.host}:{settings.port} is in use",
        "Start with `surtitle run --port <n>`, or stop the other process.",
    )


def _mask(secret: str) -> str:
    """Show only enough of a key to identify it, never enough to use it."""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"


async def _probe_deepseek(settings: Settings) -> Check:
    key = settings.deepseek_key()
    if not key:
        return Check("DeepSeek API", CheckStatus.SKIP, "no key")

    url = f"{settings.safe_base_url}/models"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return Check(
            "DeepSeek API",
            CheckStatus.WARN,
            f"could not reach {settings.safe_base_url} ({type(exc).__name__})",
            "Check your network connection; key presence was verified locally.",
        )

    if response.status_code == 200:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        names = [m.get("id") for m in payload.get("data", []) if isinstance(m, dict)]
        note = f"reachable; {len(names)} models visible" if names else "reachable"
        if names and settings.deepseek_model not in names:
            return Check(
                "DeepSeek API",
                CheckStatus.WARN,
                f"{note}, but {settings.deepseek_model!r} was not listed",
                f"Set DEEPSEEK_MODEL to one of: {', '.join(sorted(filter(None, names))[:6])}",
            )
        return Check("DeepSeek API", CheckStatus.OK, f"{note} ({settings.deepseek_model})")

    if response.status_code in (401, 403):
        return Check(
            "DeepSeek API",
            CheckStatus.FAIL,
            f"key rejected (HTTP {response.status_code})",
            "Check DEEPSEEK_API_KEY at https://platform.deepseek.com/api_keys.",
        )
    return Check(
        "DeepSeek API",
        CheckStatus.WARN,
        f"unexpected HTTP {response.status_code}",
    )


async def _probe_deepgram(settings: Settings, *, modality: str, target: str) -> Check:
    """Open a Deepgram streaming socket briefly to prove the key works.

    A successful upgrade is the only cheap, side-effect-free way to validate the
    credential, so we connect and immediately close. ``target`` is the fully built
    URL from the client, so the probe cannot disagree with the application.
    """
    import websockets

    key = settings.deepgram_key()
    if not key:
        return Check(f"Deepgram {modality}", CheckStatus.SKIP, "no key")

    try:
        async with websockets.connect(
            target, additional_headers={"Authorization": f"Token {key}"}, open_timeout=15
        ):
            pass
    except Exception as exc:  # noqa: BLE001 - any failure here is reported, not raised
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)

        if status in (401, 403):
            return Check(
                f"Deepgram {modality}",
                CheckStatus.FAIL,
                f"key rejected (HTTP {status})",
                "Check DEEPGRAM_API_KEY at https://console.deepgram.com/.",
            )

        if status is not None:
            # A 400 means the request was refused, not the key. Report the
            # server's own words instead of implying a network problem.
            detail = ""
            with contextlib.suppress(Exception):
                detail = bytes(response.body or b"").decode("utf-8", errors="replace")
            message = ""
            with contextlib.suppress(Exception):
                message = str(json.loads(detail).get("err_msg") or "")
            return Check(
                f"Deepgram {modality}",
                CheckStatus.FAIL,
                f"request rejected (HTTP {status}: {message or detail[:120]})",
                "This is a configuration problem, not a network one. Check that "
                "SURTITLE_STT_API and DEEPGRAM_STT_MODEL agree: Flux models "
                "(three hyphenated parts) require v2, Nova models require v1.",
            )

        return Check(
            f"Deepgram {modality}",
            CheckStatus.WARN,
            f"could not connect ({type(exc).__name__})",
            "This may be a network issue; the key itself was not verified.",
        )
    return Check(f"Deepgram {modality}", CheckStatus.OK, "authenticated")


async def run_checks(settings: Settings, *, live: bool = True) -> Report:
    """Run every diagnostic, optionally including live API probes."""
    import logging

    checks: list[Check] = [_check_python()]
    checks.extend(_check_dependencies())
    checks.append(_check_data_dir(settings))
    checks.extend(_check_keys(settings))
    checks.append(_check_port(settings))

    if live:
        # Silence the noisy websockets/httpx INFO logs during probes.
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        checks.append(await _probe_deepseek(settings))
        if settings.deepgram_key() and settings.voice_enabled:
            # Probe the exact URLs a session will use, built by the same code. A
            # hand-rolled parameter set here previously drifted from the client
            # and reported a healthy configuration as broken — a worse outcome
            # than having no diagnostic at all.
            checks.append(
                await _probe_deepgram(settings, modality="STT", target=listen_url(settings))
            )
            checks.append(
                await _probe_deepgram(settings, modality="TTS", target=speak_url(settings))
            )
    else:
        checks.append(Check("Live API probes", CheckStatus.SKIP, "disabled with --offline"))

    return Report(checks=checks)


_SYMBOLS = {
    CheckStatus.OK: "[ok]",
    CheckStatus.WARN: "[warn]",
    CheckStatus.FAIL: "[fail]",
    CheckStatus.SKIP: "[skip]",
}


def format_report(report: Report, *, color: bool | None = None) -> str:
    """Render the report as plain text.

    ``color`` defaults to whether stdout is a terminal, and is forced off when
    the caller needs stable output for tests.
    """
    if color is None:
        color = sys.stdout.isatty()
    palette = {
        CheckStatus.OK: "\033[32m",
        CheckStatus.WARN: "\033[33m",
        CheckStatus.FAIL: "\033[31m",
        CheckStatus.SKIP: "\033[90m",
    }
    reset = "\033[0m"

    buffer = io.StringIO()
    buffer.write(f"{APP_NAME} doctor\n")
    buffer.write("-" * 64 + "\n")
    for check in report.checks:
        label = _SYMBOLS[check.status]
        if color:
            label = f"{palette[check.status]}{label}{reset}"
        buffer.write(f"{label:<16} {check.name}: {check.detail}\n")
        if check.fix and check.status in {CheckStatus.FAIL, CheckStatus.WARN}:
            buffer.write(f"{'':<16} -> {check.fix}\n")
    buffer.write("-" * 64 + "\n")
    if report.ok:
        warnings = [c for c in report.checks if c.status is CheckStatus.WARN]
        summary = "All required checks passed."
        if warnings:
            summary += f" {len(warnings)} warning(s) above."
        buffer.write(f"{summary}\n")
    else:
        buffer.write(
            f"{len(report.failed)} check(s) failed. Fix the items marked [fail] and re-run.\n"
        )
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    """Run doctor as a standalone entry point."""
    from surtitle.config import get_settings

    settings = get_settings()
    report = asyncio.run(run_checks(settings, live="--offline" not in (argv or [])))
    sys.stdout.write(format_report(report))
    return 0 if report.ok else 1
