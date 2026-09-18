"""HTTP and WebSocket server.

One FastAPI app serves both the UI and the realtime channel, so there is no
second process to manage and no port to pair up. That also removes the macOS
``fork`` hazard that multi-process audio demos tend to hit.

Transport shape:

* **HTTP** for everything durable — projects, sessions, transcript pages,
  settings and credentials.
* **One WebSocket** per open conversation for the realtime channel: audio up,
  audio down, events out, commands in.

Audio travels as binary frames with a one-byte opcode (``0x01`` mic PCM,
``0x02`` JSON) rather than base64 inside JSON, because the microphone stream is
continuous and base64 would inflate it by a third for no benefit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import (
    APIRouter,
    Body,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from surtitle import __version__, dialogs, shell_integration
from surtitle.config import Settings, get_settings, setup_logging
from surtitle.core.events import ClientCommand, CommandKind, EventKind
from surtitle.core.session import Session, SessionManager, decode_client_frame
from surtitle.llm.deepseek import DeepSeekClient
from surtitle.stats import RunStats
from surtitle.store.db import Store
from surtitle.store.settings_store import (
    PROVIDER_SPECS,
    SettingsStore,
    SettingsValidationError,
)
from surtitle.tools import environment
from surtitle.tools.fs_tools import ToolContext, list_dir
from surtitle.tools.path_guard import PathEscapeError, resolve_in_root
from surtitle.update import TARGETS as update_targets
from surtitle.update import UpdateJob
from surtitle.update import kind as update_kind
from surtitle.voice.install import InstallJob
from surtitle.voice.install import state as local_voice_state

__all__ = ["SessionManager", "create_app"]

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

_OP_AUDIO_IN = 0x01
_OP_JSON = 0x02


# Upload limits. Generous enough for real documents, bounded so a mis-drag cannot
# fill the disk or wedge the turn.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_UPLOADS_PER_REQUEST = 12


# Attachments live in a visible folder at the project root, not inside
# `.surtitle/`. That directory is deliberately excluded from listings and
# content searches so tooling does not pollute them, which would have made
# uploaded documents both invisible to the user and undiscoverable by the agent's
# own `search_files`.
UPLOADS_DIR_NAME = "uploads"


def uploads_dir(root: Path) -> Path:
    """Where user attachments are stored for a project."""
    return root / UPLOADS_DIR_NAME


def _safe_filename(name: str) -> str:
    """Reduce an uploaded name to something safe to store inside the project.

    A filename arrives from the client and is written to disk, so it is treated as
    hostile: directory components are stripped, traversal sequences removed, and
    only a conservative character set survives. ``resolve_in_root`` would also
    catch an escape, but a name should never reach that test in the first place.
    """
    base = Path(name or "upload").name  # discard any directory part
    base = base.replace("\\", "/").split("/")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._ \[\]-]", "_", base).strip(" .")
    # Guard against names that are only dots, or reserved on Windows.
    if not cleaned or set(cleaned) <= {"."}:
        cleaned = "upload"
    stem, suffix = os.path.splitext(cleaned)
    if stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
        r"COM[1-9]|LPT[1-9]", stem.upper()
    ):
        stem = f"_{stem}"
    return f"{stem[:120]}{suffix[:20]}"


def _unique_path(directory: Path, filename: str) -> Path:
    """Avoid overwriting: add a numeric suffix rather than replacing silently."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(filename)
    for index in range(1, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("too many files with that name")


class AppState:
    """Process-wide resources shared by the HTTP and WebSocket routes."""

    def __init__(
        self, settings: Settings, *, on_shutdown: Callable[[], None] | None = None
    ) -> None:
        self.settings = settings
        self.store = Store(settings.db_path)
        self.settings_store = SettingsStore(settings)
        self.deepseek = DeepSeekClient(settings)
        self.sessions = SessionManager()
        # Usage counters for this process. Owned here rather than by a session
        # because they outlive any one conversation and are what ``/api/status``
        # and the tray icon report.
        self.stats = RunStats()
        # How ``POST /api/shutdown`` stops the server. Set by whoever owns the
        # uvicorn Server object; when it is unset the endpoint refuses, so a
        # create_app_for() test app can never signal a process it does not own.
        self.on_shutdown = on_shutdown
        # Installing the local speech engines and their models is a long download,
        # so it runs in a background thread and both the tray and the Settings
        # screen read this one job rather than starting their own.
        self.voice_install = InstallJob()
        # Pulling an update is the same shape of work: slow, network-bound, and
        # owned by the server so it survives whichever icon asked for it.
        self.update = UpdateJob(on_stop=on_shutdown)
        # Re-apply stored preferences and credentials onto the live settings.
        self.settings_store.effective()

    async def aclose(self) -> None:
        await self.sessions.close_all()
        await self.deepseek.aclose()
        self.store.close()


def _error(status: int, message: str, *, field: str | None = None) -> JSONResponse:
    """Consistent error envelope so the UI can render a field-level message."""
    payload: dict[str, Any] = {"error": message}
    if field:
        payload["field"] = field
    return JSONResponse(payload, status_code=status)


def _local_voice_payload(settings: Settings, job: InstallJob) -> dict[str, Any]:
    """Local-speech state, plus any install in flight.

    Read on every tray poll: it stats model files and asks whether
    ``sherpa_onnx`` imports, and loads neither.
    """
    snapshot = local_voice_state(settings)
    return {
        "runtime": snapshot.runtime,
        "models": snapshot.models,
        "ready": snapshot.ready,
        "detail": snapshot.detail,
        "missing_models": snapshot.missing_models,
        "missing_bytes": snapshot.missing_bytes,
        "install": job.snapshot(),
    }


def _update_payload(job: UpdateJob) -> dict[str, Any]:
    """How this installation can update, and how one in flight is going.

    Deliberately no network call: this rides on every tray poll. Deciding whether
    a newer release exists is a separate, on-demand request.
    """
    from surtitle import selfupdate

    return {
        "kind": update_kind(),
        "version": __version__,
        # Whether this install can replace itself in place (a release archive can;
        # an unpacked source tree cannot), which decides what the tray offers.
        "self_update": selfupdate.supported(),
        "job": job.snapshot(),
    }


def _project_or_404(state: AppState, project_id: str):
    project = state.store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def build_api(state: AppState) -> APIRouter:
    """Construct the HTTP API."""
    api = APIRouter(prefix="/api")

    # --- meta ------------------------------------------------------------
    @api.get("/health")
    async def health() -> dict[str, Any]:
        settings = state.settings
        return {
            "version": __version__,
            "model": settings.deepseek_model,
            "voice_enabled": settings.voice_enabled,
            # Which engine each direction uses, so a support question does not
            # have to start with "what is your configuration?".
            "voice_backends": {
                "stt": settings.stt_backend,
                "tts": settings.tts_backend,
            },
            "deepseek_configured": bool(settings.deepseek_key()),
            "deepgram_configured": bool(settings.deepgram_key()),
            "sessions": state.sessions.count,
            "data_dir": str(settings.data_dir),
            # Whether this machine can show a folder chooser, so the new-project
            # dialog offers a Browse button only where it would work.
            "folder_dialog": dialogs.available(),
        }

    @api.get("/status")
    async def status() -> dict[str, Any]:
        """Everything the tray icon and ``surtitle status`` need, in one call.

        A superset of ``/api/health``: that endpoint answers "is this configured
        and serving", which the UI polls, while this one also answers "what has
        this process done", which is what a taskbar tooltip can show. It is a
        separate route rather than more fields on ``/health`` because the two
        have different audiences and different costs — this one reads the
        database.

        No credential value may ever appear here. Only whether one is set.
        """
        settings = state.settings
        usage = state.stats.snapshot(model=settings.deepseek_model)
        return {
            "app": "Surtitle",
            "version": __version__,
            "pid": os.getpid(),
            "host": settings.host,
            "port": settings.port,
            "url": f"http://{settings.host}:{settings.port}",
            "started_at": usage["started_at"],
            "data_dir": str(settings.data_dir),
            "model": settings.deepseek_model,
            "voice_enabled": settings.voice_enabled,
            "voice_backends": {
                "stt": settings.stt_backend,
                "tts": settings.tts_backend,
            },
            "deepseek_configured": bool(settings.deepseek_key()),
            "deepgram_configured": bool(settings.deepgram_key()),
            "sessions": state.sessions.count,
            "local_voice": _local_voice_payload(settings, state.voice_install),
            "shell": shell_integration.state(),
            "update": _update_payload(state.update),
            "usage": usage,
            "storage": {
                **state.store.counts(),
                "db_bytes": state.store.size_bytes(),
            },
        }

    @api.post("/shutdown")
    async def shutdown(request: Request) -> JSONResponse:
        """Stop the server, at the request of the tray icon.

        Loopback only. The default binding is ``127.0.0.1`` and the UI has no
        authentication at all, so a server deliberately exposed to a network is
        already anyone's to drive; this endpoint must not be the thing that
        turns "someone can read your conversations" into "someone can stop your
        agent mid-turn". A request from anywhere but this machine is refused
        even though the app would otherwise have served it.

        Returns 503 when nothing is wired to stop: tests build the app without a
        uvicorn server behind it, and answering "stopped" there would be a lie.
        """
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "localhost"}:
            return _error(403, "shutdown is only allowed from this machine")
        if state.on_shutdown is None:
            return _error(503, "this server was not started with a stop handler")
        log.info("shutdown requested by %s", client)
        # Deferred by one loop turn so this response is flushed before the
        # server begins closing: the caller has to learn that it worked.
        asyncio.get_running_loop().call_later(0.05, state.on_shutdown)
        return JSONResponse({"stopping": True})

    @api.get("/models")
    async def local_models() -> dict[str, Any]:
        """Local model inventory, for the Settings screen.

        Read-only and cheap: it stats files rather than loading them, so the UI
        can show what is installed without a multi-second ONNX load.
        """
        from surtitle.voice import models

        return {"models": models.describe(state.settings)}

    @api.get("/voice/install")
    async def voice_install_status() -> dict[str, Any]:
        """What local speech still needs, and how an install is going."""
        return _local_voice_payload(state.settings, state.voice_install)

    @api.post("/voice/install")
    async def voice_install_start(request: Request) -> JSONResponse:
        """Install the local speech engines and their models, in the background.

        Loopback only, for the same reason as ``/api/shutdown``: a server bound to
        a LAN address is already readable by anyone on it, and this endpoint must
        not be what turns that into "someone can pull ~100 MB onto your disk and
        restart your voice engines".

        Answers 202 as soon as the work has started; the caller polls
        ``GET /api/voice/install`` (or ``/api/status``) for progress. A second
        request while one is running is 409, which is a state rather than a fault.
        """
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "localhost"}:
            return _error(403, "installing local voice is only allowed from this machine")
        if not state.voice_install.start(state.settings):
            return JSONResponse({"started": False, "reason": "already running"}, status_code=409)
        log.info("local voice install requested by %s", client)
        return JSONResponse({"started": True}, status_code=202)

    @api.get("/update")
    async def update_status() -> dict[str, Any]:
        """What an update would do. Network-bound, so it is asked for on demand.

        Separate from ``/api/status`` on purpose: that endpoint is polled every
        couple of seconds by the tray, and this one talks to GitHub and to git.
        """
        from surtitle import update

        status = await asyncio.to_thread(update.check)
        return {"status": status.as_dict(), "job": state.update.snapshot()}

    @api.post("/update")
    async def update_start(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
        """Pull ``main``, or check out the newest release tag, in the checkout.

        Loopback only, and for a stronger reason than the other local endpoints:
        this runs git in the user's working tree, so a remote caller must not be
        able to choose which code the machine runs next.
        """
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "localhost"}:
            return _error(403, "updating is only allowed from this machine")
        target = str(body.get("target") or "release").strip().lower()
        if target not in update_targets:
            return _error(400, f"unknown update target {target!r}", field="target")
        if not state.update.start(target, state.settings):
            return JSONResponse({"started": False, "reason": "already running"}, status_code=409)
        log.info("update to %s requested by %s", target, client)
        return JSONResponse({"started": True, "target": target}, status_code=202)

    @api.get("/shell")
    async def shell_state() -> dict[str, Any]:
        """Whether the launcher entry and the sign-in entry are in place."""
        return shell_integration.state()

    @api.post("/shell")
    async def shell_update(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
        """Create the launcher entry, or turn start-at-sign-in on or off.

        Loopback only: writing shortcuts into someone's profile on their behalf is
        not something a network caller may ask for. The work is an installer run,
        so it goes to a thread — it is quick, but it is not instant.
        """
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "localhost"}:
            return _error(403, "changing launcher entries is only allowed from this machine")

        menu = body.get("menu")
        startup = body.get("startup")
        ok, message = await asyncio.to_thread(
            shell_integration.apply,
            menu=None if menu is None else bool(menu),
            startup=None if startup is None else bool(startup),
        )
        if not ok:
            return _error(400, message)
        return JSONResponse({"ok": True, "message": message, **shell_integration.state()})

    @api.post("/dialog/folder")
    async def dialog_folder(request: Request) -> JSONResponse:
        """Open a native folder chooser on this machine (loopback only).

        The browser cannot supply a usable path: the File System Access API
        returns a handle, not a location the agent could be confined to and read.
        So the chooser opens in the process that owns the files. Loopback only,
        and for the obvious reason — a remote caller must not be able to put a
        modal window on someone else's desktop.

        Runs in a worker thread because the dialog is modal and blocking; the
        event loop has to stay free to serve the browser that is waiting for it.
        501 means this machine has no chooser at all (a headless server), which
        is a capability the UI checks before offering the button.
        """
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "localhost"}:
            return _error(403, "opening a folder dialog is only allowed from this machine")
        if not dialogs.available():
            return _error(501, "this machine cannot show a folder chooser")
        path = await asyncio.to_thread(dialogs.choose_folder)
        if not path:
            return JSONResponse({"path": None, "cancelled": True})
        return JSONResponse({"path": path})

    # --- settings --------------------------------------------------------
    @api.get("/settings")
    async def get_settings_view() -> dict[str, Any]:
        return state.settings_store.describe()

    @api.put("/settings")
    async def put_settings(patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            state.settings_store.save_settings(patch)
        except SettingsValidationError as exc:
            return _error(400, str(exc), field=exc.field_name)
        # Rebuild the client so a model change takes effect immediately.
        await state.deepseek.aclose()
        state.deepseek = DeepSeekClient(state.settings_store.effective())
        return state.settings_store.describe()

    @api.delete("/settings")
    async def reset_settings() -> dict[str, Any]:
        state.settings_store.reset_settings()
        state.settings_store.effective()
        return state.settings_store.describe()

    @api.put("/credentials/{ref}")
    async def put_credential(ref: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        value = str(body.get("value") or "")
        try:
            state_repr = state.settings_store.set_credential(ref, value)
        except SettingsValidationError as exc:
            return _error(400, str(exc), field=exc.field_name)
        state.settings_store.effective()
        return {"credential": state_repr.to_dict()}

    @api.delete("/credentials/{ref}")
    async def delete_credential(ref: str) -> dict[str, Any]:
        try:
            state_repr = state.settings_store.clear_credential(ref)
        except SettingsValidationError as exc:
            return _error(400, str(exc), field=exc.field_name)
        state.settings_store.effective()
        return {"credential": state_repr.to_dict()}

    @api.post("/credentials/{ref}/verify")
    async def verify_credential(
        ref: str, body: dict[str, Any] | None = Body(None)
    ) -> dict[str, Any]:
        """Probe a credential live.

        There is no persisted valid/invalid status: a key can be revoked at any
        time, so a stored "valid" flag would be a lie. The check happens on
        demand, which is also how the model list is refreshed.

        A ``draft`` value may be supplied to test a key *before* saving it, which
        is the point of a Test button next to an input. The draft is used for this
        request only and is never stored.
        """
        draft = (body or {}).get("draft")
        value = str(draft).strip() if isinstance(draft, str) and draft.strip() else None
        if value is None:
            value = state.settings_store.credential_value(ref)
        if not value:
            return _error(400, "That credential is not configured.", field=ref)

        spec = next((s for s in PROVIDER_SPECS.values() if s.api_key_env == ref), None)

        if spec is None or spec.discovery_path is None:
            return await _probe_streaming(ref, value)

        url = f"{spec.base_url.rstrip('/')}{spec.discovery_path}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers={"Authorization": f"Bearer {value}"})
        except httpx.HTTPError as exc:
            return _error(502, f"Could not reach {spec.label}: {type(exc).__name__}")

        if response.status_code in (401, 403):
            return _error(400, f"{spec.label} rejected that key.", field=ref)
        if response.status_code != 200:
            return _error(502, f"{spec.label} answered HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError:
            return _error(502, f"{spec.label} returned an unexpected response.")
        models = [
            str(m.get("id")) for m in payload.get("data", []) if isinstance(m, dict) and m.get("id")
        ]
        return {"ok": True, "provider": spec.label, "models": models}

    async def _probe_streaming(ref: str, value: str) -> JSONResponse:
        """Validate a streaming-only provider by opening a socket briefly."""
        from urllib.parse import urlencode

        import websockets

        from surtitle.config import DEEPGRAM_LISTEN_V2_URL

        if ref != "DEEPGRAM_API_KEY":
            return _error(400, "Unsupported credential.")
        params = {
            "model": state.settings.stt_model,
            "encoding": "linear16",
            "sample_rate": 16000,
            "channels": 1,
        }
        url = f"{DEEPGRAM_LISTEN_V2_URL}?{urlencode(params)}"
        try:
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Token {value}"}, open_timeout=15
            ):
                pass
        except Exception as exc:  # any failure is a verification report
            message = str(exc)
            if "401" in message or "403" in message:
                return _error(400, "Deepgram rejected that key.", field=ref)
            return _error(502, f"Could not reach Deepgram: {type(exc).__name__}")
        return {"ok": True, "provider": "Deepgram (voice)", "models": []}

    # --- projects --------------------------------------------------------
    @api.get("/projects")
    async def list_projects() -> dict[str, Any]:
        return {"projects": [p.to_dict() for p in state.store.list_projects()]}

    @api.post("/projects")
    async def create_project(body: dict[str, Any] = Body(...)) -> Any:
        name = str(body.get("name") or "").strip()
        if not name:
            return _error(400, "A project name is required.", field="name")

        raw_root = body.get("root")
        if raw_root:
            root = Path(str(raw_root)).expanduser()
            if not root.is_absolute():
                return _error(400, "The folder path must be absolute.", field="root")
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return _error(400, f"Cannot use that folder: {exc}", field="root")
        else:
            # Default to a managed folder so a new project always has a safe home.
            root = state.settings.default_workspace_dir / _slug(name)
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return _error(400, f"Cannot create the project folder: {exc}", field="root")

        try:
            root = root.resolve()
        except OSError as exc:
            return _error(400, f"Cannot resolve that folder: {exc}", field="root")
        if not root.is_dir():
            return _error(400, "That path is not a directory.", field="root")

        project = state.store.create_project(name, root)
        return project.to_dict()

    @api.get("/projects/{project_id}")
    async def get_project(project_id: str) -> Any:
        project = _project_or_404(state, project_id)
        state.store.touch_project(project_id)
        every = state.store.list_sessions(project_id, include_archived=True, limit=500)
        return {
            **project.to_dict(),
            # Live conversations only; the archive is fetched on demand.
            "sessions": [s.to_dict() for s in every if not s.archived],
            "session_counts": {
                "active": sum(1 for s in every if not s.archived),
                "archived": sum(1 for s in every if s.archived),
            },
        }

    @api.patch("/projects/{project_id}")
    async def rename_project(project_id: str, body: dict[str, Any] = Body(...)) -> Any:
        _project_or_404(state, project_id)
        name = str(body.get("name") or "").strip()
        if not name:
            return _error(400, "A project name is required.", field="name")
        state.store.rename_project(project_id, name)
        return _project_or_404(state, project_id).to_dict()

    @api.delete("/projects/{project_id}")
    async def delete_project(project_id: str) -> dict[str, Any]:
        """Forget a project. The user's files on disk are never deleted."""
        _project_or_404(state, project_id)
        state.store.delete_project(project_id)
        return {"deleted": project_id, "files_removed": False}

    @api.put("/projects/{project_id}/trusted-tools")
    async def set_trusted_tools(project_id: str, body: dict[str, Any] = Body(...)) -> Any:
        _project_or_404(state, project_id)
        tools = body.get("tools")
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            return _error(400, "tools must be a list of tool names.", field="tools")
        state.store.set_auto_approved(project_id, tools)
        return {"tools": sorted(set(tools))}

    # --- uploads ---------------------------------------------------------
    @api.post("/projects/{project_id}/uploads")
    async def upload_files(
        project_id: str,
        files: list[UploadFile] = File(...),
        session_id: str | None = Form(None),
    ) -> Any:
        """Accept files the user attached, storing them inside the project.

        Stored rather than kept in memory because the agent's tools need a real
        path: `read_file` takes a project-relative path, so giving the model one
        means an attached PDF is read with the same code path as any other file.

        They are stored in `uploads/` at the project root so the user can see them
        in their own file manager, and so `list_dir` and `search_files` find them
        alongside the rest of the project.
        """
        project = _project_or_404(state, project_id)
        root = Path(project.root)
        if not root.is_dir():
            return _error(410, "The project folder no longer exists.", field="root")
        if not files:
            return _error(400, "No files were uploaded.")
        if len(files) > MAX_UPLOADS_PER_REQUEST:
            return _error(
                400,
                f"Too many files at once ({len(files)}); the limit is {MAX_UPLOADS_PER_REQUEST}.",
                field="files",
            )

        upload_dir = uploads_dir(root)
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _error(500, f"Could not create the upload directory: {exc}")

        saved: list[dict[str, Any]] = []
        for upload in files:
            try:
                target = _unique_path(upload_dir, _safe_filename(upload.filename or "upload"))
            except OSError as exc:
                return _error(409, str(exc), field="files")

            total = 0
            try:
                with target.open("wb") as handle:
                    while chunk := await upload.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            handle.close()
                            target.unlink(missing_ok=True)
                            return _error(
                                413,
                                f"{upload.filename} is larger than "
                                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                                field="files",
                            )
                        handle.write(chunk)
            except OSError as exc:
                return _error(500, f"Could not save {upload.filename}: {exc}")
            finally:
                with contextlib.suppress(Exception):
                    await upload.close()

            saved.append(
                {
                    "name": upload.filename or target.name,
                    "path": target.relative_to(root.resolve()).as_posix(),
                    "bytes": total,
                }
            )

        if session_id:
            summary = ", ".join(f"{item['path']} ({item['bytes']} bytes)" for item in saved)
            state.store.add_message(
                session_id,
                "system",
                f"The user attached {len(saved)} file(s): {summary}",
            )
        return {"files": saved}

    @api.get("/projects/{project_id}/uploads")
    async def list_uploads(project_id: str) -> dict[str, Any]:
        """List previously uploaded files."""
        project = _project_or_404(state, project_id)
        root = Path(project.root).resolve()
        upload_dir = uploads_dir(root)
        if not upload_dir.is_dir():
            return {"files": []}
        entries = []
        for path in sorted(upload_dir.iterdir()):
            if not path.is_file():
                continue
            entries.append(
                {
                    "name": path.name,
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                }
            )
        return {"files": entries}

    @api.delete("/projects/{project_id}/uploads/{name}")
    async def delete_upload(project_id: str, name: str) -> Any:
        project = _project_or_404(state, project_id)
        root = Path(project.root).resolve()
        # Strip any directory component before joining: `name` arrives from the
        # URL, so "../.." must not be able to reach outside the uploads folder.
        safe = Path(name).name
        try:
            target = resolve_in_root(root, f"{UPLOADS_DIR_NAME}/{safe}")
        except PathEscapeError as exc:
            return _error(403, str(exc))
        if not target.absolute.is_file():
            return _error(404, "No such upload.")
        with contextlib.suppress(OSError):
            target.absolute.unlink()
        return {"deleted": target.relative}

    # --- project files ---------------------------------------------------
    @api.get("/projects/{project_id}/files")
    async def project_files(project_id: str, path: str = ".", limit: int = 200) -> Any:
        """List project files for the sidebar tree."""
        project = _project_or_404(state, project_id)
        root = Path(project.root)
        if not root.is_dir():
            return _error(410, "The project folder no longer exists.", field="root")
        result = list_dir(ToolContext(root=root, project_id=project_id), path, limit=limit)
        if not result.ok:
            return _error(400, result.error or "Cannot list that folder.")
        return result.data

    @api.get("/projects/{project_id}/file")
    async def project_file(project_id: str, path: str) -> Any:
        """Serve one project file for preview or download."""
        project = _project_or_404(state, project_id)
        root = Path(project.root).resolve()
        try:
            target = resolve_in_root(root, path)
        except PathEscapeError as exc:
            return _error(403, str(exc))
        if not target.absolute.is_file():
            return _error(404, "File not found.")
        media = _media_type(target.absolute.suffix)
        disposition = (
            "inline" if media.startswith(("text/", "image/", "application/pdf")) else "attachment"
        )
        return FileResponse(
            target.absolute,
            media_type=media,
            headers={"Content-Disposition": f'{disposition}; filename="{target.absolute.name}"'},
        )

    # --- sessions --------------------------------------------------------
    @api.get("/projects/{project_id}/sessions")
    async def list_sessions(project_id: str, archived: str = "active") -> dict[str, Any]:
        """List conversations.

        ``archived`` selects the view: ``active`` (default), ``archived``, or
        ``all``. The counts are always returned for both sides so the sidebar can
        label the archive without a second request.
        """
        _project_or_404(state, project_id)
        view = archived if archived in {"active", "archived", "all"} else "active"
        every = state.store.list_sessions(project_id, include_archived=True, limit=500)
        sessions = [s for s in every if view == "all" or (view == "archived") == bool(s.archived)]
        return {
            "sessions": [s.to_dict() for s in sessions],
            "counts": {
                "active": sum(1 for s in every if not s.archived),
                "archived": sum(1 for s in every if s.archived),
            },
        }

    @api.post("/projects/{project_id}/sessions")
    async def create_session(project_id: str, body: dict[str, Any] | None = Body(None)) -> Any:
        _project_or_404(state, project_id)
        title = str((body or {}).get("title") or "New conversation")
        return state.store.create_session(project_id, title).to_dict()

    @api.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> Any:
        session = state.store.get_session(session_id)
        if session is None:
            return _error(404, "Session not found.")
        return {
            **session.to_dict(),
            "messages": [m.to_dict() for m in state.store.list_messages(session_id)],
            "tool_calls": [t.to_dict() for t in state.store.list_tool_calls(session_id)],
        }

    @api.post("/sessions/{session_id}/archive")
    async def archive_session(session_id: str) -> Any:
        """File a conversation away. Keeps the transcript and every file on disk."""
        if state.store.get_session(session_id) is None:
            return _error(404, "Session not found.")
        session = state.store.set_session_archived(session_id, True)
        # Drop any live runtime session so an archived conversation stops
        # listening. Only the in-memory session goes away; the transcript stays.
        await state.sessions.remove(session_id)
        return {"archived": session_id, "session": session.to_dict() if session else None}

    @api.post("/sessions/{session_id}/unarchive")
    async def unarchive_session(session_id: str) -> Any:
        if state.store.get_session(session_id) is None:
            return _error(404, "Session not found.")
        session = state.store.set_session_archived(session_id, False)
        return {"unarchived": session_id, "session": session.to_dict() if session else None}

    @api.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        """Delete one conversation for good. Files in the project are untouched."""
        if state.store.get_session(session_id) is None:
            return _error(404, "Session not found.")
        state.store.delete_session(session_id)
        await state.sessions.remove(session_id)
        return {"deleted": session_id}

    @api.delete("/projects/{project_id}/sessions/archived")
    async def purge_archived(project_id: str, confirm: bool = False) -> Any:
        """Permanently delete every archived conversation in a project.

        This clears chat history only; the project folder is never touched.
        ``confirm=true`` is required so a stray request cannot wipe the archive.
        """
        project = _project_or_404(state, project_id)
        if not confirm:
            return _error(
                400,
                "Emptying the archive permanently deletes those conversations."
                " Repeat with confirm=true.",
                field="confirm",
            )
        archived = state.store.list_sessions(project_id, include_archived=True, limit=500)
        for session in archived:
            if session.archived:
                await state.sessions.remove(session.id)
        removed = state.store.purge_archived_sessions(project_id)
        log.info("purged %d archived session(s) from project %s", removed, project.name)
        return {"purged": removed, "project_id": project_id}

    # --- tools -----------------------------------------------------------
    @api.get("/projects/{project_id}/environment")
    async def project_environment(project_id: str, deep: bool = False) -> Any:
        """Report the project's isolated Python environment.

        ``deep=true`` shells out to list installed distributions, which is slower
        but exact; the default is the cheap directory-based summary.
        """
        project = _project_or_404(state, project_id)
        root = Path(project.root)
        if not root.is_dir():
            return _error(410, "The project folder no longer exists.", field="root")
        if deep:
            status = await environment.project_env_status(root)
            return {**status.to_dict(), "approved": status.approved}
        return environment.environment_summary(root)

    @api.get("/tools")
    async def list_tools() -> dict[str, Any]:
        from surtitle.tools.registry import default_registry

        registry = default_registry()
        return {
            "tools": [
                {
                    "name": tool.name,
                    "summary": tool.summary,
                    "approval": tool.approval,
                    "mutating": tool.mutating,
                }
                for tool in (registry.get(name) for name in registry.names())
                if tool is not None
            ]
        }

    return api


def _slug(name: str) -> str:
    """Turn a project name into a safe folder name."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_ " else "-" for ch in name).strip()
    slug = "-".join(cleaned.split()).lower()
    return slug or "project"


def _media_type(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".csv": "text/csv",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".json": "application/json",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".py": "text/plain",
        ".ps1": "text/plain",
        ".html": "text/html",
        ".log": "text/plain",
    }.get(suffix.lower(), "application/octet-stream")


def build_ws(state: AppState) -> APIRouter:
    """Construct the WebSocket routes."""
    router = APIRouter()

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        # Accept first, then complete the handshake over the socket. Deferring
        # accept() until a hello message arrives makes a slow client look like a
        # failed connection, so it is accepted immediately and the session is
        # only created once the client says which project it wants.
        await websocket.accept()

        session: Session | None = None
        try:
            hello = await _await_hello(websocket)
            if hello is None:
                return
            project_id = hello["project_id"]
            session_id = hello["session_id"]

            project = state.store.get_project(project_id)
            if project is None:
                await _safe_send(
                    websocket, {"kind": "error", "data": {"message": "Project not found"}}
                )
                return
            if state.store.get_session(session_id) is None:
                await _safe_send(
                    websocket, {"kind": "error", "data": {"message": "Session not found"}}
                )
                return

            missing = state.settings_store.effective().missing_credentials()

            connection_token = uuid.uuid4().hex
            candidate = Session(
                session_id=session_id,
                project_id=project_id,
                root=Path(project.root),
                settings=state.settings_store.effective(),
                store=state.store,
                deepseek=state.deepseek,
                stats=state.stats,
                send=lambda payload: _safe_send(websocket, payload),
                send_audio=lambda audio: _safe_send_bytes(websocket, bytes([_OP_AUDIO_IN]) + audio),
            )
            # A reconnect for a conversation that is already live reuses that
            # session and rebinds it here, so a turn in progress is not thrown away
            # with its answer.
            session, started_now = await state.sessions.acquire(candidate, connection_token)
            if started_now:
                await session.start()
            else:
                log.info("reusing the live session for %s (reconnected)", session_id)
                # The browser lost any events sent while it was away, so restate
                # where things stand rather than leaving the UI showing "thinking".
                await session.announce()

            if missing:
                # Surfaced as a recoverable notice, not a failure: the UI routes
                # the user to Settings and everything else still works.
                await session.emit(
                    EventKind.ERROR,
                    message=(
                        "No API key configured yet, so the agent cannot answer. "
                        "Open Settings and add your credentials."
                    ),
                    kind_detail="not_configured",
                    recoverable=True,
                )

            await _pump(websocket, session)

        except WebSocketDisconnect:
            log.debug("client disconnected")
        except Exception:
            log.exception("websocket handler failed")
        finally:
            if session is not None and connection_token is not None:
                # Only the connection that still owns this session may close it; a
                # superseded tab finishing its handler must leave it alone.
                await state.sessions.release(session.session_id, connection_token)
            with contextlib.suppress(Exception):
                await websocket.close()

    return router


async def _await_hello(websocket: WebSocket) -> dict[str, Any] | None:
    """Wait for the client's hello message and validate it."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return None
        raw = message.get("bytes")
        text = message.get("text")

        payload: dict[str, Any] | None = None
        if text:
            with contextlib.suppress(ValueError):
                payload = json.loads(text)
        elif raw:
            with contextlib.suppress(Exception):
                opcode, body = decode_client_frame(raw)
                if opcode == "json":
                    payload = body

        if payload is None:
            continue
        if payload.get("kind") != CommandKind.HELLO.value:
            await _safe_send(
                websocket,
                {"kind": "error", "data": {"message": "Expected a hello message first."}},
            )
            continue

        data = payload.get("data") or {}
        project_id = str(data.get("project_id") or "")
        session_id = str(data.get("session_id") or "")
        if not project_id or not session_id:
            await _safe_send(
                websocket,
                {"kind": "error", "data": {"message": "hello requires project_id and session_id"}},
            )
            return None
        return {"project_id": project_id, "session_id": session_id}


async def _pump(websocket: WebSocket, session: Session) -> None:
    """Dispatch client commands until the socket closes."""
    while True:
        message = await websocket.receive()
        kind = message.get("type")
        if kind == "websocket.disconnect":
            return

        raw = message.get("bytes")
        if raw:
            try:
                opcode, body = decode_client_frame(raw)
            except Exception as exc:
                log.debug("bad binary frame: %s", exc)
                continue
            if opcode == "audio":
                await session.handle_audio(body)
            elif opcode == "json":
                await _dispatch(session, body)
            continue

        text = message.get("text")
        if text:
            try:
                await _dispatch(session, json.loads(text))
            except ValueError:
                log.debug("ignoring malformed JSON frame")


async def _dispatch(session: Session, payload: dict[str, Any]) -> None:
    """Route one decoded command to the session."""
    try:
        command = ClientCommand.parse(payload)
    except ValueError as exc:
        await session.emit(EventKind.ERROR, message=str(exc))
        return

    data = command.data

    if command.kind is CommandKind.PING:
        await session.emit(EventKind.READY, pong=True)
    elif command.kind is CommandKind.AUDIO:
        # Base64 fallback for clients that cannot send binary frames.
        import base64

        encoded = data.get("audio")
        if isinstance(encoded, str):
            with contextlib.suppress(Exception):
                await session.handle_audio(base64.b64decode(encoded))
    elif command.kind is CommandKind.MIC:
        await session.handle_mic(bool(data.get("open")))
    elif command.kind is CommandKind.TEXT:
        await session.handle_text(str(data.get("text") or ""))
    elif command.kind is CommandKind.BARGE_IN:
        # The client detects loudness, which cannot tell the user from the
        # speakers. It requests; the server decides, using transcribed speech.
        await session.cancel_turn(require_speech=True)
    elif command.kind is CommandKind.APPROVAL:
        await session.handle_approval(
            str(data.get("call_id") or ""),
            allowed=bool(data.get("allowed")),
            remember=bool(data.get("remember")),
        )
    elif command.kind is CommandKind.SET_MODE:
        voice = data.get("voice_enabled")
        if isinstance(voice, bool):
            session.settings.voice_enabled = voice
    # HELLO is consumed during the handshake, so it is ignored here.


async def _safe_send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send JSON, ignoring a socket that has already gone away."""
    with contextlib.suppress(Exception):
        await websocket.send_text(json.dumps(payload, default=str))


async def _safe_send_bytes(websocket: WebSocket, data: bytes) -> None:
    """Send a binary frame, ignoring a socket that has already gone away."""
    with contextlib.suppress(Exception):
        await websocket.send_bytes(data)


def create_app(
    settings: Settings | None = None,
    *,
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Application factory. Uvicorn is pointed at this by name.

    Called with no arguments by ``uvicorn --reload``, which resolves it from a
    string; the command line calls it directly so the app it builds is the one
    whose settings were already adjusted (port, voice) and so ``on_shutdown``
    can stop the very server object that is running it.
    """
    settings = settings or get_settings()
    setup_logging(settings)
    settings.ensure_data_dir()

    state = AppState(settings, on_shutdown=on_shutdown)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Resources are created eagerly so a configuration problem surfaces at
        # startup rather than on the first request.
        try:
            yield
        finally:
            await state.aclose()

    app = FastAPI(
        title="Surtitle",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.app_state = state

    app.include_router(build_api(state))
    app.include_router(build_ws(state))

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return FileResponse(WEB_DIR / "index.html")

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.exception_handler(SettingsValidationError)
    async def _settings_error(_request: Any, exc: SettingsValidationError) -> JSONResponse:
        return _error(400, str(exc), field=exc.field_name)

    return app


def create_app_for(
    settings: Settings,
    *,
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Factory used by tests, which need a scoped data directory."""
    app = FastAPI(title="Surtitle", version=__version__, docs_url=None, redoc_url=None)
    state = AppState(settings, on_shutdown=on_shutdown)
    app.state.app_state = state
    app.include_router(build_api(state))
    app.include_router(build_ws(state))

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return FileResponse(WEB_DIR / "index.html")

    return app
