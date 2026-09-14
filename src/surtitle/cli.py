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
    ConfigError,
    get_settings,
    reset_settings_cache,
    setup_logging,
)
from surtitle.platform_utils import find_free_port, readable_path
from surtitle.store.settings_store import SettingsStore, SettingsValidationError

app = typer.Typer(
    name="surtitle",
    help="Voice-first agentic workbench: talk to an agent that reads your documents, "
    "writes code, and produces PDFs and spreadsheets.",
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
    from surtitle.doctor import format_report, run_checks

    store = _open_store()
    assert store is not None
    settings = store.effective()
    setup_logging(settings)

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
) -> None:
    """Start the server and open the UI."""
    import uvicorn

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

    try:
        settings.require_credentials(voice=settings.voice_enabled)
    except ConfigError as exc:
        console.print(Panel(str(exc), title="Cannot start", border_style="red"))
        console.print(
            "Open the app to add keys in Settings, or run [bold]surtitle init[/bold]."
        )
        raise typer.Exit(code=2) from exc

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

    uvicorn.run(
        "surtitle.server:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=reload,
        log_config=None,
        access_log=False,
    )


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
