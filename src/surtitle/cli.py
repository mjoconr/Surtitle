"""Command line interface: ``surtitle <command>``."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from surtitle import __version__
from surtitle.config import (
    Settings,
    get_settings,
    reset_settings_cache,
    setup_logging,
)
from surtitle.platform_utils import find_free_port, human_bytes, is_windows, readable_path
from surtitle.store.settings_store import SettingsStore, SettingsValidationError

app = typer.Typer(
    name="surtitle",
    help="Voice-first agentic workbench — talk to an agent that reads your documents, "
    "writes and runs code, and answers out loud. Hear the conclusion, not the log.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"surtitle {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    """Surtitle command line interface."""


def _open_store(*, fail_when_broken: bool = True) -> SettingsStore | None:
    """Build the settings store, reporting a broken credentials file clearly."""
    try:
        return SettingsStore(get_settings())
    except SettingsValidationError as exc:
        console.print(f"[red]Configuration problem:[/red] {exc}")
        if fail_when_broken:
            raise typer.Exit(code=2) from exc
        return None


@app.command()
def doctor(
    offline: bool = typer.Option(
        False, "--offline", help="Skip live API probes; check local state only."
    ),
) -> None:
    """Check that this machine can run Surtitle."""
    from surtitle.config import loaded_env_files
    from surtitle.doctor import format_report, run_checks

    store = _open_store()
    assert store is not None
    # `effective()` folds stored credentials into the live settings. Without it a
    # key saved through the app's own Settings screen is invisible here, and a
    # correctly configured install reports as broken.
    settings = store.effective()
    setup_logging(settings)

    # Say which files were read: a stray .env silently overrides defaults, and
    # that is otherwise impossible to see.
    sources = loaded_env_files()
    if sources:
        console.print(
            "[dim]Configuration read from: "
            + ", ".join(readable_path(path) for path in sources)
            + "[/dim]"
        )
    else:
        console.print("[dim]No .env file found; using defaults and saved settings.[/dim]")

    report = asyncio.run(run_checks(settings, live=not offline))
    sys.stdout.write(format_report(report))
    raise typer.Exit(code=0 if report.ok else 1)


@app.command()
def run(
    port: int = typer.Option(None, "--port", "-p", help="Port to serve on."),
    host: str = typer.Option(None, "--host", help="Host to bind (default 127.0.0.1)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
    text_only: bool = typer.Option(
        False, "--text-only", help="Disable voice; requires only a DeepSeek key."
    ),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload for development."),
    strict_port: bool = typer.Option(
        False, "--strict-port", help="Fail instead of probing for a free port."
    ),
    tray: bool = typer.Option(
        None,
        "--tray/--no-tray",
        help="Show a taskbar icon with status and a stop button (Windows; on by default).",
    ),
) -> None:
    """Start the server and open the UI."""
    import uvicorn

    from surtitle.server import create_app

    store = _open_store()
    assert store is not None
    settings = store.effective()

    if text_only:
        settings.voice_enabled = False
    if host:
        settings.host = host
    if no_browser:
        settings.open_browser = False
    if port is not None:
        settings.port = port

    setup_logging(settings)

    # A missing DeepSeek key must NOT stop startup. The key is added in the app's
    # own Settings screen, so refusing to serve would be a deadlock: the only way
    # to fix the problem would be hidden behind the problem. Start anyway, warn
    # clearly, and let the agent paths report the missing credential.
    missing = settings.missing_credentials()
    if missing:
        console.print(
            Panel(
                "Missing required credential(s): " + ", ".join(missing) + "\n\n"
                "The app will start so you can add them in Settings, but the agent "
                "cannot answer until they are set.",
                title="Not configured yet",
                border_style="yellow",
            )
        )
    elif not settings.voice_enabled:
        console.print("[yellow]Voice is disabled; running text-only.[/yellow]")

    if strict_port:
        from surtitle.platform_utils import port_is_free

        if not port_is_free(settings.host, settings.port):
            console.print(f"[red]Port {settings.port} on {settings.host} is already in use.[/red]")
            raise typer.Exit(code=2)
    else:
        try:
            chosen = find_free_port(settings.host, settings.port)
        except OSError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        if chosen != settings.port:
            console.print(f"[yellow]Port {settings.port} is busy; using {chosen} instead.[/yellow]")
            settings.port = chosen

    scheme_host = f"http://{settings.host}:{settings.port}"
    console.print(
        Panel.fit(
            f"[bold]{scheme_host}[/bold]\n"
            f"model: [cyan]{settings.deepseek_model}[/cyan]   "
            f"voice: {'[green]on[/green]' if settings.voice_enabled else '[yellow]off[/yellow]'}\n"
            f"data:  {readable_path(settings.data_dir)}\n\n"
            "Press [bold]Ctrl+C[/bold] to stop.",
            title=f"Surtitle {__version__}",
            border_style="cyan",
        )
    )

    if settings.open_browser:
        from surtitle.platform_utils import open_browser as _open

        _open(scheme_host)

    from surtitle.local_api import clear_instance, write_instance

    # Record where we landed, so `surtitle tray` and `surtitle status` can find
    # this server even when the port was probed. Written before serving so the
    # note is never missing while the socket is open.
    write_instance(settings, host=settings.host, port=settings.port, version=__version__)

    if reload:
        # The reloader owns a child process, and the tray would outlive the
        # thing it is watching every time a file changes, so this path is left
        # alone. `--reload` is a development flag applied to a development
        # server; the tray is for the people the app is for.
        console.print("[dim]--reload supervises a child process; the tray icon is off.[/dim]")
        try:
            uvicorn.run(
                "surtitle.server:create_app",
                factory=True,
                host=settings.host,
                port=settings.port,
                reload=True,
                log_config=None,
                access_log=False,
            )
        finally:
            clear_instance(settings)
        return

    want_tray = tray if tray is not None else is_windows()
    if want_tray and not is_windows():
        console.print("[yellow]A taskbar icon is only available on Windows.[/yellow]")
        want_tray = False

    # Uvicorn's own server object, rather than `uvicorn.run`, so the tray's Stop
    # item can flag a graceful shutdown: it drains in-flight requests and runs
    # the lifespan teardown that closes sessions and the database. Terminating
    # the process would skip all of that.
    server: uvicorn.Server | None = None

    def request_stop() -> None:
        if server is not None:
            server.should_exit = True

    app = create_app(settings, on_shutdown=request_stop)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_config=None,
            access_log=False,
        )
    )

    tray_icon = None
    if want_tray:
        from surtitle.tray import start_tray

        tray_icon = start_tray(scheme_host, version=__version__)
        if tray_icon is None:
            console.print(
                "[yellow]Could not add a taskbar icon; the server is running anyway.[/yellow]"
            )
        else:
            # Windows 11 files a newly registered notification icon under the
            # overflow arrow rather than on the taskbar itself, and the app has
            # no supported way to promote it — that is the user's setting. So the
            # one thing worth saying is where it went, once, at startup.
            console.print(
                "[dim]Surtitle is in the notification area. Windows puts a new icon "
                "under the ^ arrow — drag it onto the taskbar to keep it in view.[/dim]"
            )

    try:
        server.run()
    finally:
        if tray_icon is not None:
            tray_icon.stop()
        clear_instance(settings)


def _local_settings() -> Settings:
    """Settings for the commands that only need to find a running server.

    Deliberately tolerant of a broken credentials file: being unable to read a
    key is no reason to be unable to stop or inspect the process that is already
    running.
    """
    store = _open_store(fail_when_broken=False)
    return store.effective() if store is not None else get_settings()


@app.command()
def tray(
    url: str = typer.Option(None, "--url", help="Server to attach to, e.g. http://127.0.0.1:8765."),
) -> None:
    """Show a taskbar icon for a running Surtitle (Windows).

    `surtitle run` already shows one. This command exists for the case where the
    server was started some other way — a script, a shortcut, another user's
    session — and a status icon is still wanted.
    """
    if not is_windows():
        console.print("[red]A taskbar icon is only available on Windows.[/red]")
        raise typer.Exit(code=2)

    from surtitle.local_api import find_instance
    from surtitle.tray import start_tray

    settings = _local_settings()
    instance = find_instance(settings, url=url)
    if instance is None:
        console.print(
            "[red]No running Surtitle found.[/red] Start one with [bold]surtitle run[/bold], "
            "or name it with [bold]--url[/bold]."
        )
        raise typer.Exit(code=1)

    icon = start_tray(instance.url, version=instance.version or __version__, live=True)
    if icon is None:
        console.print("[red]Windows refused the taskbar icon.[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"Tray icon attached to [bold]{instance.url}[/bold]. "
        "Use its menu to stop the server, or press Ctrl+C to close just the icon."
    )
    try:
        while not icon.server_gone:
            # A timed wait rather than a blocking one so Ctrl+C is acted on
            # immediately rather than after the server next changes state.
            icon.wait(timeout=1.0)
    except KeyboardInterrupt:
        console.print("\n[dim]Tray icon closed. The server is still running.[/dim]")
    finally:
        icon.stop()


@app.command()
def status(
    url: str = typer.Option(None, "--url", help="Server to ask, e.g. http://127.0.0.1:8765."),
    as_json: bool = typer.Option(False, "--json", help="Print the raw payload for scripting."),
) -> None:
    """Report what a running Surtitle is doing, or say that none is running."""
    import json as _json

    from surtitle.local_api import fetch_status, find_instance

    settings = _local_settings()
    instance = find_instance(settings, url=url)
    if instance is None:
        console.print("[yellow]No running Surtitle was found.[/yellow]")
        raise typer.Exit(code=1)

    payload = fetch_status(instance.url)
    if payload is None:
        console.print(f"[red]{instance.url} stopped answering.[/red]")
        raise typer.Exit(code=1)

    if as_json:
        sys.stdout.write(_json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return

    from surtitle.tray import format_status, format_usage

    console.print(Panel(format_status(payload), title="Status", border_style="cyan"))
    console.print(Panel(format_usage(payload), title="Usage", border_style="cyan"))


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing .env file."),
    show: bool = typer.Option(False, "--show", help="Print where config lives, then exit."),
) -> None:
    """Scaffold a .env file so keys can be set without the UI."""
    settings = get_settings()
    store = _open_store()
    assert store is not None

    if show:
        table = Table(show_header=False, box=None)
        table.add_row("data directory", str(settings.data_dir))
        table.add_row("settings file", str(store.settings_path))
        table.add_row("credentials file", str(store.credentials_path))
        table.add_row("database", str(settings.db_path))
        table.add_row("log file", str(settings.log_path))
        console.print(table)
        return

    target = Path.cwd() / ".env"
    template = Path(__file__).resolve().parent.parent.parent / ".env.example"
    if target.exists() and not force:
        console.print(
            f"[yellow]{target} already exists.[/yellow] Edit it directly, or pass --force to reset."
        )
        raise typer.Exit(code=1)

    # pragma: no cover applies to the fallback branch, used only when the repo
    # file is absent from a packaged build.
    content = template.read_text(encoding="utf-8") if template.exists() else _FALLBACK_ENV_TEMPLATE

    target.write_text(content, encoding="utf-8")
    console.print(
        f"Wrote [bold]{target}[/bold]. Add your API keys, then run [bold]surtitle run[/bold]."
    )
    console.print(
        "Keys can also be added in the app's Settings screen, which stores them "
        f"separately at {readable_path(store.credentials_path)}."
    )


_FALLBACK_ENV_TEMPLATE = """\
# Surtitle configuration. Only the two keys below are required.
DEEPSEEK_API_KEY=
DEEPGRAM_API_KEY=
"""


# --- local speech models -------------------------------------------------

models_app = typer.Typer(
    name="models",
    help="Download and verify the local speech models (no API key needed).",
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")


def _model_table(rows: list) -> Table:
    table = Table(box=None)
    table.add_column("Model", style="cyan")
    table.add_column("Kind")
    table.add_column("Size", justify="right")
    table.add_column("Status")
    for item in rows:
        status = "[green]installed[/green]" if item.present else "[yellow]missing[/yellow]"
        table.add_row(item.key, item.kind, human_bytes(item.total_bytes), status)
    return table


@models_app.command("list")
def models_list() -> None:
    """Show every registered local model and whether it is installed."""
    from surtitle.voice import models

    store = _open_store()
    assert store is not None
    settings = store.effective()
    rows = models.status(settings)
    console.print(_model_table(rows))
    for item in rows:
        if not item.present:
            console.print(f"[dim]{item.key}: {item.summary}[/dim]")
    console.print(f"[dim]Model cache: {readable_path(models.models_dir(settings))}[/dim]")


@models_app.command("status")
def models_status() -> None:
    """Alias for `models list`."""
    models_list()


@models_app.command("download")
def models_download(
    keys: list[str] = typer.Argument(
        None, help="Model keys to install. Default: everything registered."
    ),
    kind: str = typer.Option(None, "--kind", help="Install only this kind: 'stt' or 'tts'."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    force: bool = typer.Option(
        False, "--force", help="Re-download even when the model is already installed."
    ),
) -> None:
    """Download local speech models into the app data directory."""
    from surtitle.voice import models
    from surtitle.voice.models import ModelUnavailable

    if kind is not None and kind not in {"stt", "tts"}:
        console.print("[red]--kind must be 'stt' or 'tts'[/red]")
        raise typer.Exit(code=2)

    store = _open_store()
    assert store is not None
    settings = store.effective()

    wanted = tuple(keys) if keys else ()
    if wanted:
        unknown = [key for key in wanted if key not in models.MODEL_REGISTRY]
        if unknown:
            console.print(
                f"[red]Unknown model(s):[/red] {', '.join(unknown)}\n"
                f"Available: {', '.join(models.model_keys())}"
            )
            raise typer.Exit(code=2)
        selected = [models.MODEL_REGISTRY[key] for key in wanted]
    else:
        selected = list(models.iter_assets(kind))

    todo = [
        asset
        for asset in selected
        if force or models.missing_files(asset, models.model_root(settings, asset))
    ]
    where = readable_path(models.models_dir(settings))

    if not todo:
        console.print(f"[green]Nothing to do[/green] — every selected model is in {where}.")
        return

    total = sum(asset.total_bytes for asset in todo)
    console.print(f"Installing {len(todo)} model(s), {human_bytes(total)} on disk, into {where}:")
    for asset in todo:
        console.print(f"  · {asset.key} — {asset.label}")

    if not yes and not typer.confirm("Continue?", default=True):
        raise typer.Exit(code=1)

    failures = 0
    for asset in todo:
        with console.status(f"Downloading {asset.key}…") as status:
            last = {"line": ""}
            key = asset.key

            def on_progress(update, _status=status, _last=last, _key=key) -> None:
                if update.stage == "download" and update.total:
                    pct = 100 * update.received / update.total
                    line = f"{_key}: {pct:5.1f}% ({human_bytes(update.received)})"
                else:
                    line = f"{_key}: {update.message or update.stage}"
                if line != _last["line"]:
                    _last["line"] = line
                    _status.update(line)

            try:
                models.download(settings, keys=(key,), progress=on_progress, force=force)
            except ModelUnavailable as exc:
                failures += 1
                console.print(f"[red]{asset.key} failed:[/red] {exc.reason}")
                if exc.fix:
                    console.print(f"  [dim]{exc.fix}[/dim]")
    if failures:
        raise typer.Exit(code=1)
    console.print("[green]Done.[/green] Restart the app to pick up the new engines.")


@models_app.command("verify")
def models_verify() -> None:
    """Re-check every installed model's checksum.

    Only models that are *partly* present or corrupt cause a failure: a model you
    chose not to install is not a problem, and treating it as one would make this
    command useless on a machine that installed one voice out of three.
    """
    from surtitle.voice import models

    store = _open_store()
    assert store is not None
    settings = store.effective()
    rows = models.verify(settings)
    console.print(_model_table(rows))

    # A model that is absent is a choice; a model that is partly present or whose
    # files fail their checksum is damage, and it is the only thing worth failing
    # over. Otherwise this command is unusable on a machine that installed one
    # voice out of three.
    broken = [item for item in rows if item.partial]
    for item in broken:
        console.print(f"[red]{item.key}[/red]: {item.summary}")
    if broken:
        console.print("Run `surtitle models download` to repair the affected models.")
        raise typer.Exit(code=1)
    console.print("[green]Every installed model verified.[/green]")


@models_app.command("path")
def models_path() -> None:
    """Print where models are stored, for scripting."""
    from surtitle.voice import models

    store = _open_store()
    assert store is not None
    sys.stdout.write(str(models.models_dir(store.effective())) + "\n")


# --- local voice engines -------------------------------------------------

voice_app = typer.Typer(
    name="voice",
    help="Install and inspect the offline speech engines (no API key needed).",
    no_args_is_help=True,
)
app.add_typer(voice_app, name="voice")


def _progress_line(update) -> str:
    """One line of install progress, for the status spinner."""
    if update.stage == "download" and update.total:
        percent = 100 * update.received / update.total
        return f"{update.asset}: {percent:5.1f}% ({human_bytes(update.received)})"
    return f"{update.asset}: {update.message or update.stage}"


@voice_app.command("install")
def voice_install(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Install the offline speech engines and download their models.

    The same work the tray's "Install local voice…" item asks the server to do,
    available in a terminal and from a script. It is also the reason a user who
    installed with `--no-voice` can change their mind without re-running the
    whole installer.
    """
    from surtitle.voice import install as voice
    from surtitle.voice import models

    store = _open_store()
    assert store is not None
    settings = store.effective()

    before = voice.state(settings)
    plan = voice.runtime_plan()
    console.print(f"Local speech: last checked — {before.detail}")
    console.print(f"Engines: [bold]{plan.label}[/bold]")
    # The real figure, not a remembered one: the registry's models run to
    # hundreds of megabytes, and a stale round number would be a small lie before
    # a long download.
    if before.missing_bytes:
        size = f"about {human_bytes(before.missing_bytes)}"
    else:
        size = "nothing to download"
    console.print(f"Models:  {size} into {readable_path(models.models_dir(settings))}")

    if not yes and not typer.confirm("Continue?", default=True):
        raise typer.Exit(code=1)

    with console.status("Installing the local speech engines…") as status:
        last = {"line": ""}

        def on_progress(update) -> None:
            line = _progress_line(update)
            if line != last["line"]:
                last["line"] = line
                status.update(line)

        result = voice.install(settings, progress=on_progress)

    style = "[green]Done.[/green]" if result.ok else "[red]Failed.[/red]"
    console.print(f"{style} {result.message}")
    if not result.ok:
        raise typer.Exit(code=1)


@voice_app.command("status")
def voice_status() -> None:
    """Report whether offline speech is available, and what is missing.

    Exits non-zero when it is not ready, so a script can branch on it.
    """
    from surtitle.voice import install as voice

    store = _open_store()
    assert store is not None
    snapshot = voice.state(store.effective())

    table = Table(box=None, show_header=False)
    engines = "[green]installed[/green]" if snapshot.runtime else "[red]missing[/red]"
    models_state = "[green]installed[/green]" if snapshot.models else "[yellow]missing[/yellow]"
    table.add_row("Engines", engines)
    table.add_row("Models", models_state)
    console.print(table)
    console.print(f"[dim]{snapshot.detail}[/dim]")

    if not snapshot.ready:
        console.print("Run [bold]surtitle voice install[/bold] to add offline speech.")
        raise typer.Exit(code=1)


@app.command()
def settings_show() -> None:
    """Print the effective settings and credential status (no secret values)."""
    store = _open_store()
    assert store is not None
    described = store.describe()

    table = Table(title="Preferences", box=None)
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    table.add_column("Source", style="dim")
    for _section, fields in described["sections"].items():
        for spec in fields:
            source = "env" if spec["env_locked"] else ("file" if spec["stored"] else "default")
            table.add_row(spec["name"], str(spec["value"]), source)
    console.print(table)

    creds = Table(title="Credentials", box=None)
    creds.add_column("Reference", style="cyan")
    creds.add_column("Status")
    creds.add_column("Source", style="dim")
    for provider in described["providers"]:
        state = provider["credential"]
        status = "[green]configured[/green]" if state["configured"] else "[red]missing[/red]"
        creds.add_row(provider["api_key_env"], status, state["source"] or "-")
    console.print(creds)
    console.print(f"[dim]Stored at {described['data_dir']}[/dim]")


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - thin wrapper
    """Console-script entry point."""
    reset_settings_cache()
    app(args=argv)
