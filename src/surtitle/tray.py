"""The Windows tray icon: what it says, what it offers, and when it goes away.

The split matters for testing. Everything in this module is ordinary Python —
the text, the menu, the polling — and runs anywhere, including on the machines
that run the test suite. Only :mod:`surtitle.win32_tray`, imported lazily and
only on Windows, touches the shell. So the wording and the choices a user makes
are covered by the offline suite, and the untestable part is reduced to the
handful of ctypes calls that put a window on the screen.

The icon reports the server over HTTP rather than reaching into it. That is not
indirection for its own sake: ``surtitle tray`` runs as a separate process, and
having one code path means the standalone icon and the one started by
``surtitle run`` cannot drift apart.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from surtitle.local_api import (
    fetch_status,
    request_shutdown,
    request_update,
    request_voice_install,
)
from surtitle.platform_utils import human_bytes, is_windows, open_browser
from surtitle.stats import PRICES_CHECKED, format_duration
from surtitle.update import RELEASES_PAGE

__all__ = [
    "ACTIONS",
    "MenuEntry",
    "SurtitleTray",
    "format_status",
    "format_usage",
    "human_count",
    "menu_entries",
    "summary_text",
    "tooltip_text",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MenuEntry:
    """One row of the tray menu.

    Rendered by :mod:`surtitle.win32_tray`. It lives here, on the portable side,
    so the menu a user sees can be built and asserted on any platform — the part
    that cannot be tested is the drawing, not the deciding.
    """

    label: str = ""
    action: str | None = None
    enabled: bool = True
    separator: bool = False


ACTIONS = (
    "open",
    "status",
    "usage",
    "voice",
    "update_release",
    "update_main",
    "update_page",
    "stop",
)

# How often the server is asked how it is doing. Every poll is a loopback
# request that reads a few row counts, so this is cheap; two seconds keeps the
# tooltip honest without being noticeable.
_POLL_SECONDS = 2.0
# Consecutive failed polls before the icon decides the server has gone. At two
# seconds apart this is roughly the time a browser takes to stop showing a
# spinner, which is the felt expectation.
_MISSES_BEFORE_GONE = 3
# How long to keep looking for a server that has never answered. A tray started
# by ``surtitle run`` is created moments before uvicorn binds its socket, so the
# first poll legitimately fails; a tray with nothing to attach to should not
# linger forever either.
_GRACE_SECONDS = 30.0


def human_count(value: int) -> str:
    """Compact token count: ``823``, ``12.4k``, ``1.8M``."""
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}k"
    return f"{value / 1_000_000:.1f}M"


def _thousands(value: int) -> str:
    """``132400`` → ``132,400``. Read in a dialog, not a chart."""
    return f"{value:,}"


def _money(usage: dict[str, Any]) -> str:
    """The estimated cost, or an honest refusal to invent one."""
    if not usage.get("priced", True):
        return "not priced — no published rate for this model"
    return f"${usage.get('cost_usd') or 0.0:.2f}"


def _credentials(snapshot: dict[str, Any]) -> str:
    deepseek = "set" if snapshot.get("deepseek_configured") else "missing"
    if snapshot.get("voice_enabled"):
        deepgram = "set" if snapshot.get("deepgram_configured") else "missing"
        return f"DeepSeek: {deepseek}   Deepgram: {deepgram}"
    return f"DeepSeek: {deepseek}   voice disabled"


def summary_text(snapshot: dict[str, Any] | None) -> str:
    """One line of run facts, for the menu and the tooltip."""
    if snapshot is None:
        return "no server is answering on this port"
    usage = snapshot.get("usage") or {}
    pieces = [
        f"{snapshot.get('sessions') or 0} live",
        f"{usage.get('turns') or 0} turns",
        f"{human_count(int(usage.get('total_tokens') or 0))} tok",
    ]
    # A model with no rate card shows tokens and no money: a $0.00 in the menu
    # reads as "free", which is a worse answer than silence.
    if usage.get("priced", True):
        pieces.append(f"${usage.get('cost_usd') or 0.0:.2f}")
    return " · ".join(pieces)


def tooltip_text(snapshot: dict[str, Any] | None, *, version: str = "") -> str:
    """The line shown when the pointer rests on the icon."""
    name = f"Surtitle {version}".strip()
    if snapshot is None:
        return f"{name} — starting…"
    usage = snapshot.get("usage") or {}
    return f"{name} — {summary_text(snapshot)} · {format_duration(_uptime(usage))}"


def _uptime(usage: dict[str, Any]) -> float:
    return float(usage.get("uptime_seconds") or 0)


def menu_entries(snapshot: dict[str, Any] | None) -> list[MenuEntry]:
    """The context menu, built fresh on every right-click.

    Fresh rather than cached, because the whole value of a tray menu here is
    that the state is visible without opening anything: a menu built once at
    startup would still be claiming two live conversations an hour after they
    ended.
    """
    live = snapshot is not None
    if live:
        header = f"Surtitle {snapshot.get('version') or ''}".strip() + " — running"
        summary = (
            f"{summary_text(snapshot)} · up {format_duration(_uptime(snapshot.get('usage') or {}))}"
        )
    else:
        header = "Surtitle — not responding"
        summary = summary_text(None)

    return [
        MenuEntry(label=header, enabled=False),
        MenuEntry(label=summary, enabled=False),
        MenuEntry(separator=True),
        MenuEntry(label="Open Surtitle", action="open", enabled=live),
        MenuEntry(label="Status…", action="status", enabled=live),
        MenuEntry(label="Usage…", action="usage", enabled=live),
        MenuEntry(separator=True),
        MenuEntry(**_voice_entry(snapshot if live else None)),
        *_update_entries(snapshot if live else None),
        MenuEntry(separator=True),
        MenuEntry(label="Stop Surtitle", action="stop", enabled=live),
    ]


def _update_entries(snapshot: dict[str, Any] | None) -> list[MenuEntry]:
    """The update rows, which differ with how Surtitle was installed.

    A git checkout can move itself, and there are two sensible destinations: the
    newest tagged release for most people, the development branch for someone
    following it. A release archive installs the release build itself and restarts.
    Only an unpacked source tree, which has neither history nor a build marker, is
    left with the download page.
    """
    if snapshot is None:
        return [MenuEntry(label="Update Surtitle…", action="update_release", enabled=False)]

    update = snapshot.get("update") or {}
    if (update.get("job") or {}).get("running"):
        return [MenuEntry(label="Updating…", action="update_release", enabled=False)]
    if update.get("kind") == "git":
        return [
            MenuEntry(label="Update to the latest release…", action="update_release"),
            MenuEntry(label="Update to current main…", action="update_main"),
        ]
    if update.get("self_update"):
        return [MenuEntry(label="Update to the latest release…", action="update_release")]
    return [MenuEntry(label="Get the latest release…", action="update_page")]


def _voice_entry(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """The local-voice row: install it, watch it install, or say it is there.

    The label is the status, so the menu answers "is offline speech available?"
    without a dialog. Installing is disabled while it runs — clicking it twice
    would only produce a 409 — and reports progress in the label instead.
    """
    if snapshot is None:
        return {"label": "Install local voice…", "action": "voice", "enabled": False}

    voice = snapshot.get("local_voice") or {}
    install = voice.get("install") or {}
    if install.get("running"):
        percent = float(install.get("percent") or 0.0)
        return {
            "label": f"Installing local voice… {percent:.0f}%",
            "action": "voice",
            "enabled": False,
        }
    if voice.get("ready"):
        return {"label": "Local voice is installed", "action": "voice", "enabled": False}
    return {"label": "Install local voice…", "action": "voice", "enabled": True}


def format_status(snapshot: dict[str, Any]) -> str:
    """The Status dialog: what is running, and what it is attached to."""
    usage = snapshot.get("usage") or {}
    storage = snapshot.get("storage") or {}
    backends = snapshot.get("voice_backends") or {}
    voice = (
        f"on — {backends.get('stt', '?')} → {backends.get('tts', '?')}"
        if snapshot.get("voice_enabled")
        else "disabled"
    )
    lines = [
        f"Surtitle {snapshot.get('version') or ''}".strip(),
        "",
        f"Address       {snapshot.get('url') or ''}",
        f"Uptime        {format_duration(float(usage.get('uptime_seconds') or 0))}"
        f"   (process {snapshot.get('pid') or '?'})",
        f"Model         {snapshot.get('model') or '?'}",
        f"Voice         {voice}",
        f"Credentials   {_credentials(snapshot)}",
        f"Live          {snapshot.get('sessions') or 0} conversation(s)",
        "",
        f"Stored        {storage.get('projects') or 0} project(s) · "
        f"{storage.get('conversations') or 0} conversation(s) · "
        f"{_thousands(int(storage.get('messages') or 0))} message(s)",
        f"Database      {human_bytes(int(storage.get('db_bytes') or 0))}",
        f"Data folder   {snapshot.get('data_dir') or ''}",
    ]
    return "\n".join(lines)


def format_usage(snapshot: dict[str, Any]) -> str:
    """The Usage dialog: what this run has spent, in tokens and in money."""
    usage = snapshot.get("usage") or {}
    uptime = format_duration(float(usage.get("uptime_seconds") or 0))
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(usage.get("cached_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    reasoning = int(usage.get("reasoning_tokens") or 0)

    lines = [
        f"This run — {uptime} since the server started",
        "",
        f"Model calls      {_thousands(int(usage.get('model_calls') or 0))}",
        f"Turns answered   {_thousands(int(usage.get('turns') or 0))}",
        f"Tool calls       {_thousands(int(usage.get('tool_calls') or 0))}",
    ]
    if usage.get("errors"):
        lines.append(f"Errors           {_thousands(int(usage.get('errors') or 0))}")

    lines += [
        "",
        f"Input tokens     {_thousands(prompt)}"
        + (f"   ({_thousands(cached)} cached)" if cached else ""),
        f"Output tokens    {_thousands(completion)}"
        + (f"   ({_thousands(reasoning)} reasoning)" if reasoning else ""),
        f"Total tokens     {_thousands(int(usage.get('total_tokens') or 0))}",
        "",
        f"Estimated cost   {_money(usage)}",
    ]
    if usage.get("priced", True):
        lines.append(f"Rates            DeepSeek published rates, checked {PRICES_CHECKED}")
        lines.append("                 set SURTITLE_PRICE_* to override")
    return "\n".join(lines)


class SurtitleTray:
    """Polls a local server and puts its state on the taskbar.

    Owns one poller thread and, on Windows, one icon thread. Nothing here raises
    at a caller: a machine with no interactive desktop, a shell that refuses the
    icon, or a server that stops answering are all normal outcomes that the
    caller reports and moves on from.
    """

    def __init__(
        self,
        url: str,
        *,
        version: str = "",
        icon_path: Path | None = None,
        interval: float = _POLL_SECONDS,
        live: bool = False,
        on_change: Callable[[dict[str, Any] | None], None] | None = None,
    ) -> None:
        self.url = url
        self.version = version
        self.icon_path = icon_path
        self.interval = interval
        self._snapshot: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._gone = threading.Event()
        self._seen = live
        self._misses = 0
        self._icon: Any = None
        self._poller: threading.Thread | None = None
        self._on_change = on_change

    # --- state -----------------------------------------------------------
    @property
    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return self._snapshot

    @property
    def server_gone(self) -> bool:
        return self._gone.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the server stops answering. True if it did."""
        return self._gone.wait(timeout)

    # --- lifecycle -------------------------------------------------------
    def start(self) -> bool:
        """Start polling and add the icon. False when the icon cannot appear."""
        if not is_windows():
            log.debug("tray icons are Windows-only; nothing to start")
            return False
        self._poller = threading.Thread(target=self._poll, name="surtitle-status", daemon=True)
        self._poller.start()

        from surtitle.win32_tray import TrayIcon

        self._icon = TrayIcon(
            tooltip=lambda: tooltip_text(self.snapshot, version=self.version),
            menu=lambda: menu_entries(self.snapshot),
            on_select=self._select,
            title=f"Surtitle {self.version}".strip(),
            icon_path=self.icon_path,
        )
        if self._icon.start():
            return True
        # No icon: stop polling rather than leaving a thread with no reader.
        self.stop()
        return False

    def stop(self) -> None:
        """Remove the icon and stop polling."""
        self._stop.set()
        if self._icon is not None:
            self._icon.stop()
            self._icon = None
        if self._poller is not None and self._poller.is_alive():
            self._poller.join(timeout=2.0)
        self._poller = None

    # --- actions ---------------------------------------------------------
    def _select(self, action: str) -> None:
        """Run a menu choice. Called on the icon's thread."""
        if action == "open":
            open_browser(self.url)
        elif action == "status":
            self._show(format_status, "Surtitle — status")
        elif action == "usage":
            self._show(format_usage, "Surtitle — usage")
        elif action == "voice":
            self._install_local_voice()
        elif action == "update_release":
            self._update("release")
        elif action == "update_main":
            self._update("main")
        elif action == "update_page":
            open_browser(RELEASES_PAGE)
        elif action == "stop":
            self._request_stop()

    def _show(self, render: Callable[[dict[str, Any]], str], title: str) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            self._message("Surtitle is not answering on this port.", title)
            return
        self._message(render(snapshot), title)

    def _request_stop(self) -> None:
        """Ask the server to shut down, and say so if it will not."""
        if request_shutdown(self.url):
            log.info("stop requested; waiting for the server to exit")
            return
        self._message(
            "Surtitle did not accept the stop request.\n\n"
            "It may already be shutting down. If it is still running, close its "
            "console window instead.",
            "Surtitle — stop failed",
        )

    def _install_local_voice(self) -> None:
        """Ask the server to add the offline engines and download their models.

        The server owns the work, not the tray: there are two trays (the one
        ``surtitle run`` starts and the standalone one), and a download that
        belongs to whichever icon happened to be clicked would be lost with it.
        """
        voice = (self.snapshot or {}).get("local_voice") or {}
        title = "Surtitle — local voice"
        if self.snapshot is None:
            self._message("Surtitle is not answering on this port.", title)
            return
        if voice.get("ready"):
            self._message("Local speech is already installed.", title)
            return
        if not self._confirm(
            "Download and install the local speech engines?\n\n"
            f"This fetches the sherpa-onnx runtime {_download_clause(voice)}, and needs "
            "an internet connection. It runs in the background, so the app can be used "
            "while it finishes.",
            title,
        ):
            return

        answer = request_voice_install(self.url)
        if answer is None:
            self._message("Surtitle did not accept the request.", title)
            return
        if not answer.get("started", True):
            self._message("An install is already running.", title)
            return
        self._message(
            "Installing the local speech engines in the background. The menu shows "
            "how it is going; restart Surtitle when it finishes to use them.",
            title,
        )

    def _update(self, target: str) -> None:
        """Ask the server to update, and say which kind of update it will be.

        Two very different things hide behind one word: a git checkout pulls code
        and needs restarting afterwards, while a release archive downloads and
        installs a new build and restarts itself. The question says which.
        """
        title = "Surtitle — update"
        update = (self.snapshot or {}).get("update") or {}
        in_place = update.get("kind") != "git" and bool(update.get("self_update"))

        if in_place:
            question = (
                "Update Surtitle to the latest release?\n\n"
                "The release is downloaded and checked against its published "
                "checksum, then installed. Surtitle closes and starts the new "
                "version; your settings, database and speech models are kept."
            )
        else:
            destination = (
                "the current development branch (main)"
                if target == "main"
                else "the latest release"
            )
            question = (
                f"Update Surtitle to {destination}?\n\n"
                "This pulls the new code into this checkout and re-installs the "
                "dependencies. Surtitle keeps running the version it started with "
                "until you restart it, and local changes stop the update rather than "
                "being overwritten."
            )

        if not self._confirm(question, title):
            return

        answer = request_update(self.url, target)
        if answer is None:
            self._message("Surtitle did not accept the request.", title)
            return
        if not answer.get("started", True):
            self._message("An update is already running.", title)
            return
        self._message(
            "Surtitle will close and start the new version when the download finishes."
            if in_place
            else "Updating in the background. The menu shows how it is going; restart "
            "Surtitle when it finishes.",
            title,
        )

    def _confirm(self, text: str, title: str) -> bool:
        """A yes/no box, and 'no' whenever the icon cannot ask."""
        if self._icon is None:
            return False
        confirm = getattr(self._icon, "confirm", None)
        if confirm is None:
            return False
        return bool(confirm(text, title=title))

    def _message(self, text: str, title: str) -> None:
        if self._icon is not None:
            self._icon.message(text, title=title)

    # --- polling ---------------------------------------------------------
    def _poll(self) -> None:
        deadline = time.monotonic() + _GRACE_SECONDS
        while not self._stop.is_set():
            snapshot = fetch_status(self.url)
            if snapshot is not None:
                self._publish(snapshot)
                self._misses = 0
                self._seen = True
            else:
                self._misses += 1
                gone = self._misses >= _MISSES_BEFORE_GONE and self._seen
                if not self._seen and time.monotonic() > deadline:
                    gone = True
                if gone:
                    self._publish(None)
                    self._finish()
                    return
            self._stop.wait(self.interval)
        self._publish(None)

    def _publish(self, snapshot: dict[str, Any] | None) -> None:
        with self._lock:
            self._snapshot = snapshot
        if self._on_change is not None:
            self._on_change(snapshot)

    def _finish(self) -> None:
        """The server is gone: take the icon down and let a waiter proceed."""
        self._gone.set()
        icon = self._icon
        self._icon = None
        if icon is not None:
            icon.stop()
        self._stop.set()


def _download_clause(voice: dict[str, Any]) -> str:
    """Name the size of the download, when the server knows it.

    The registry's models run to hundreds of megabytes, so quoting a stale round
    number in a "download?" prompt is worse than saying nothing.
    """
    size = int(voice.get("missing_bytes") or 0)
    if size <= 0:
        return "and the speech models"
    return f"and about {human_bytes(size)} of speech models"


def icon_file() -> Path:
    """Where the shipped ``.ico`` lives, whether or not it exists."""
    return Path(__file__).resolve().parent / "web" / "surtitle.ico"


def start_tray(
    url: str,
    *,
    version: str = "",
    live: bool = False,
) -> SurtitleTray | None:
    """Start a tray icon, or return ``None`` when one cannot be shown.

    Callers treat ``None`` as "carry on without it": a headless session, a
    locked-down desktop and a non-Windows platform all land here, and none of
    them is an error worth stopping the server over.
    """
    tray = SurtitleTray(
        url,
        version=version,
        icon_path=icon_file(),
        live=live,
    )
    if not tray.start():
        return None
    return tray
