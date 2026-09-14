"""Model Context Protocol client.

MCP is the right way to reach systems that are not files: a Fusion 360 server, a
vendor CAD tool, or an in-house server for a sampling line. Rather than write a
connector per system, this mounts any MCP server's tools into the existing tool
registry, where they inherit approval gating, transcript rendering and the speak
layer for free.

This is a deliberately small implementation of the stdio transport covering the
part of the protocol a tool-using agent actually needs: ``initialize``,
``tools/list`` and ``tools/call``.

Why not the official SDK: its dependency chain needs a Rust build step, which
would break the "extract and run on Windows with no toolchain" requirement. The
protocol surface used here is stable and small enough to own, and owning it
means timeouts and process-group kills behave exactly like the rest of the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Any

from surtitle.tools.project_config import McpServerConfig

__all__ = [
    "McpClient",
    "McpError",
    "McpManager",
    "McpTool",
    "format_tool_result",
]

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "surtitle", "version": "0.1.0"}

# How much of a tool's text output is handed to the model.
_MAX_RESULT_CHARS = 24_000


class McpError(RuntimeError):
    """Raised when a server cannot be started or answers with an error."""


@dataclass(slots=True)
class McpTool:
    """A tool advertised by an MCP server."""

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        """Name as exposed to the model, namespaced by server."""
        return f"{self.server}__{self.name}"


class McpClient:
    """A single stdio MCP server connection."""

    def __init__(self, config: McpServerConfig, *, root: Any = None) -> None:
        self.config = config
        self.root = root
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._tools: list[McpTool] = []
        self._stderr_tail: list[str] = []
        self.started = False

    # --- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Launch the server and complete the MCP handshake."""
        if self.started:
            return

        # Resolve the command explicitly so a missing binary produces a clear
        # message instead of a raw FileNotFoundError from the subprocess call.
        executable = shutil.which(self.config.command) or self.config.command
        if os.path.sep in self.config.command:
            if not os.path.isfile(executable):
                raise McpError(
                    f"Cannot start MCP server {self.config.name!r}: "
                    f"{self.config.command!r} does not exist."
                )
        elif shutil.which(self.config.command) is None:
            raise McpError(
                f"Cannot start MCP server {self.config.name!r}: "
                f"{self.config.command!r} was not found on PATH."
            )

        cwd = self.config.cwd
        if cwd and self.root is not None:
            # A relative cwd is resolved against the project root so a project
            # config stays portable between machines.
            candidate = (self.root / cwd).resolve()
            cwd = str(candidate)

        environment = os.environ.copy()
        environment.update({str(k): str(v) for k, v in self.config.env.items()})

        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000200
        else:
            kwargs["start_new_session"] = True

        try:
            self._process = await asyncio.create_subprocess_exec(
                executable,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=environment,
                **kwargs,
            )
        except FileNotFoundError as exc:
            raise McpError(
                f"MCP server {self.config.name!r} could not be started: "
                f"{self.config.command!r} not found."
            ) from exc
        except OSError as exc:
            raise McpError(f"MCP server {self.config.name!r} failed to start: {exc}") from exc

        # Drain stderr, or a chatty server fills its pipe and blocks forever.
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"mcp-stderr-{self.config.name}"
        )

        try:
            await self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "clientInfo": _CLIENT_INFO,
                },
                timeout=self.config.startup_timeout,
            )
            await self._notify("notifications/initialized", {})
        except Exception as exc:
            await self.stop()
            detail = self._stderr_summary()
            raise McpError(
                f"MCP server {self.config.name!r} did not complete the handshake: {exc}"
                + (f" (server said: {detail})" if detail else "")
            ) from exc

        self.started = True
        log.info("MCP server %r connected (%s)", self.config.name, self.config.command)

    async def stop(self) -> None:
        """Shut the server down, killing the process group if it lingers."""
        self.started = False
        process, self._process = self._process, None

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None

        if process is None:
            return

        # Closing stdin is the polite shutdown signal for a stdio server.
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()

        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except TimeoutError:
            pass

        with contextlib.suppress(Exception):
            if os.name == "nt":
                import subprocess

                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            else:
                os.killpg(os.getpgid(process.pid), 15)
        with contextlib.suppress(Exception):
            process.kill()

    # --- tools -----------------------------------------------------------
    async def list_tools(self) -> list[McpTool]:
        """Fetch and cache this server's tool list."""
        if self._tools:
            return self._tools

        result = await self._request("tools/list", {}, timeout=self.config.startup_timeout)
        entries = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(entries, list):
            raise McpError(f"MCP server {self.config.name!r} returned no tool list")

        tools: list[McpTool] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            schema = entry.get("inputSchema")
            tools.append(
                McpTool(
                    server=self.config.tool_prefix,
                    name=name,
                    description=str(entry.get("description") or f"{name} (via MCP)"),
                    input_schema=schema if isinstance(schema, dict) else {"type": "object"},
                )
            )
        self._tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Invoke a tool, returning ``(ok, text)``.

        Errors are returned rather than raised so the model can react to a failed
        call the same way it reacts to a failed local tool.
        """
        try:
            result = await self._request(
                "tools/call",
                {"name": name, "arguments": arguments},
                timeout=self.config.call_timeout,
            )
        except McpError as exc:
            return False, str(exc)

        if not isinstance(result, dict):
            return False, "The MCP server returned an unexpected response."

        text = format_tool_result(result)
        is_error = bool(result.get("isError"))
        return (not is_error), text

    # --- transport -------------------------------------------------------
    async def _request(
        self, method: str, params: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """Send a request and wait for its matching response."""
        async with self._lock:
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise McpError(f"MCP server {self.config.name!r} is not running")

            request_id = self._next_id
            self._next_id += 1
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}

            try:
                process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise McpError(
                    f"MCP server {self.config.name!r} closed the connection. "
                    f"{self._stderr_summary()}"
                ) from exc

            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise McpError(
                        f"MCP server {self.config.name!r} did not answer {method} within "
                        f"{timeout:.0f}s."
                    )
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                except TimeoutError as exc:
                    raise McpError(
                        f"MCP server {self.config.name!r} did not answer {method} within "
                        f"{timeout:.0f}s."
                    ) from exc

                if not line:
                    raise McpError(
                        f"MCP server {self.config.name!r} exited while waiting for {method}. "
                        f"{self._stderr_summary()}"
                    )

                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded:
                    continue

                try:
                    message = json.loads(decoded)
                except json.JSONDecodeError:
                    # Servers occasionally log to stdout; skip anything that is
                    # not a JSON-RPC frame rather than failing the call.
                    log.debug("MCP %s: ignoring non-JSON output", self.config.name)
                    continue

                if not isinstance(message, dict):
                    continue
                # Notifications and other requests may interleave; only the
                # response carrying our id is ours.
                if message.get("id") != request_id:
                    continue

                error = message.get("error")
                if isinstance(error, dict):
                    raise McpError(f"{method} failed: {error.get('message') or error}")
                result = message.get("result")
                return result if isinstance(result, dict) else {}

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification (no response expected)."""
        process = self._process
        if process is None or process.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        with contextlib.suppress(Exception):
            process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await process.stdin.drain()

    async def _drain_stderr(self) -> None:
        """Consume stderr, keeping only a short tail for error messages."""
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    del self._stderr_tail[:-20]
                    log.debug("MCP %s stderr: %s", self.config.name, text)
        except asyncio.CancelledError:
            raise
        except Exception:  # stderr draining must never raise
            return

    def _stderr_summary(self) -> str:
        """Last few stderr lines, for diagnosis."""
        if not self._stderr_tail:
            return ""
        return " | ".join(self._stderr_tail[-3:])[:400]


def format_tool_result(result: dict[str, Any]) -> str:
    """Render an MCP tool result as text for the model.

    Handles the content list shape (text, image, resource) so an image or a
    document reference is described rather than silently dropped.
    """
    content = result.get("content")
    if isinstance(content, str):
        return _truncate(content)
    if not isinstance(content, list):
        structured = result.get("structuredContent")
        if structured is not None:
            return _truncate(json.dumps(structured, ensure_ascii=False, default=str))
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        item_type = str(item.get("type") or "text")
        if item_type == "text":
            parts.append(str(item.get("text") or ""))
        elif item_type == "image":
            mime = item.get("mimeType") or "image"
            parts.append(f"[image returned: {mime}]")
        elif item_type == "resource":
            resource = item.get("resource") or {}
            uri = resource.get("uri") if isinstance(resource, dict) else None
            parts.append(f"[resource: {uri or 'unknown'}]")
        else:
            parts.append(json.dumps(item, ensure_ascii=False, default=str))

    return _truncate("\n".join(part for part in parts if part))


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n... [{len(text) - limit} characters omitted] ...\n{tail}"


class McpManager:
    """Owns the MCP connections for one project.

    Servers are started lazily and independently: one that fails to start must
    not stop the others, or a single broken config would make the whole project
    unusable.
    """

    def __init__(self, configs: list[McpServerConfig], *, root: Any = None) -> None:
        self._configs = [c for c in configs if c.enabled and c.command]
        self._clients: dict[str, McpClient] = {}
        self._tools: list[McpTool] = []
        self._failures: list[str] = []
        self.root = root
        self.connected = False

    @property
    def failures(self) -> list[str]:
        """Human-readable reasons servers could not be started."""
        return list(self._failures)

    @property
    def tools(self) -> list[McpTool]:
        return list(self._tools)

    @property
    def server_names(self) -> list[str]:
        return sorted(self._clients)

    async def connect_all(self) -> list[McpTool]:
        """Start every configured server and collect its tools."""
        if not self._configs:
            return []

        for config in self._configs:
            client = McpClient(config, root=self.root)
            try:
                await client.start()
                tools = await client.list_tools()
            except McpError as exc:
                # Record and continue: one bad server should not hide the rest.
                log.warning("MCP server %r unavailable: %s", config.name, exc)
                self._failures.append(str(exc))
                await client.stop()
                continue
            except Exception as exc:  # a server bug must not break startup
                log.exception("MCP server %r failed unexpectedly", config.name)
                self._failures.append(f"{config.name}: {type(exc).__name__}: {exc}")
                await client.stop()
                continue

            self._clients[config.name] = client
            self._tools.extend(tools)

        self.connected = True
        return list(self._tools)

    def client_for(self, namespace: str) -> McpClient | None:
        """Find the client owning a tool namespace prefix."""
        for client in self._clients.values():
            if client.config.tool_prefix == namespace:
                return client
        return None

    def make_handler(self, tool: McpTool):
        """Build an async handler that proxies to the owning server."""
        client = self.client_for(tool.server)
        if client is None:  # pragma: no cover - tool lists come from clients
            raise McpError(f"No MCP client owns namespace {tool.server!r}")

        async def handler(ctx: Any, **arguments: Any):
            from surtitle.tools.fs_tools import ToolResult

            ok, text = await client.call_tool(tool.name, arguments)
            if ok:
                return ToolResult(
                    ok=True,
                    data={"server": client.config.name, "tool": tool.name, "result": text},
                    display=f"{client.config.name}: {tool.name}",
                )
            return ToolResult(
                ok=False,
                error=text or f"{client.config.name}.{tool.name} failed",
                display=f"{client.config.name}: {tool.name} failed",
            )

        return handler

    async def close(self) -> None:
        """Stop every server."""
        for client in list(self._clients.values()):
            with contextlib.suppress(Exception):
                await client.stop()
        self._clients.clear()
        self._tools.clear()
        self.connected = False
