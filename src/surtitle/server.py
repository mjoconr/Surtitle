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

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from surtitle import __version__
from surtitle.config import Settings, get_settings, setup_logging
from surtitle.core.events import ClientCommand, CommandKind, EventKind
from surtitle.core.session import Session, SessionManager, decode_client_frame
from surtitle.llm.deepseek import DeepSeekClient
from surtitle.store.db import Store
from surtitle.store.settings_store import (
    PROVIDER_SPECS,
    SettingsStore,
    SettingsValidationError,
)
from surtitle.tools import environment
from surtitle.tools.fs_tools import ToolContext, list_dir
from surtitle.tools.path_guard import PathEscapeError, resolve_in_root

__all__ = ["SessionManager", "create_app"]

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

_OP_AUDIO_IN = 0x01
_OP_JSON = 0x02


class AppState:
    """Process-wide resources shared by the HTTP and WebSocket routes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.db_path)
        self.settings_store = SettingsStore(settings)
        self.deepseek = DeepSeekClient(settings)
        self.sessions = SessionManager()
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
            "deepseek_configured": bool(settings.deepseek_key()),
            "deepgram_configured": bool(settings.deepgram_key()),
            "sessions": state.sessions.count,
            "data_dir": str(settings.data_dir),
        }

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
        return {
            **project.to_dict(),
            "sessions": [s.to_dict() for s in state.store.list_sessions(project_id)],
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
    async def list_sessions(project_id: str) -> dict[str, Any]:
        _project_or_404(state, project_id)
        return {"sessions": [s.to_dict() for s in state.store.list_sessions(project_id)]}

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

    @api.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        if state.store.get_session(session_id) is None:
            return _error(404, "Session not found.")
        state.store.delete_session(session_id)
        await state.sessions.remove(session_id)
        return {"deleted": session_id}

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

            session = Session(
                session_id=session_id,
                project_id=project_id,
                root=Path(project.root),
                settings=state.settings_store.effective(),
                store=state.store,
                deepseek=state.deepseek,
                send=lambda payload: _safe_send(websocket, payload),
                send_audio=lambda audio: _safe_send_bytes(websocket, bytes([_OP_AUDIO_IN]) + audio),
            )
            state.sessions.add(session)
            await session.start()

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
            if session is not None:
                await state.sessions.remove(session.session_id)
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
        await session.cancel_turn()
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


def create_app() -> FastAPI:
    """Application factory. Uvicorn is pointed at this by name."""
    settings = get_settings()
    setup_logging(settings)
    settings.ensure_data_dir()

    state = AppState(settings)

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


def create_app_for(settings: Settings) -> FastAPI:
    """Factory used by tests, which need a scoped data directory."""
    app = FastAPI(title="Surtitle", version=__version__, docs_url=None, redoc_url=None)
    state = AppState(settings)
    app.state.app_state = state
    app.include_router(build_api(state))
    app.include_router(build_ws(state))

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return FileResponse(WEB_DIR / "index.html")

    return app
