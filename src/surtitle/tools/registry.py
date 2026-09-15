"""The tool registry: what the agent can do, and how it is gated.

Each tool carries a JSON Schema for the model, a human-readable summary for the
approval prompt, and an approval policy. Keeping the policy next to the schema is
deliberate: a new tool cannot be added without someone deciding whether it may
run without asking.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from surtitle.tools import artifacts, documents, environment, fs_tools, shell_tools
from surtitle.tools.fs_tools import ToolContext, ToolResult

__all__ = [
    "Tool",
    "ToolRegistry",
    "build_registry_for_project",
    "default_registry",
]

log = logging.getLogger(__name__)

# Set for the duration of a dispatch so an argument-aware approval hook can see
# which project is being asked about, without threading the context through the
# approval API.
_APPROVAL_ROOT: ContextVar[Any] = ContextVar("surtitle_approval_root", default=None)

ApprovalPolicy = Literal["never", "ask"]

Handler = Callable[..., ToolResult | Awaitable[ToolResult]]


@dataclass(slots=True, frozen=True)
class Tool:
    """One callable capability exposed to the model."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    # "ask" tools require explicit user approval before every call unless the
    # user has chosen to trust that tool for this project.
    approval: ApprovalPolicy = "ask"
    # One-line explanation shown in the approval dialog.
    summary: str = ""
    # Whether the call can change anything on disk.
    mutating: bool = False
    # Set when the tool lives on an MCP server rather than in this process, so
    # the descriptor can tell the user which server is being asked.
    mcp_server: str | None = None
    # Optional argument-aware gate. Returns True when this *particular* call
    # still needs approval, given the arguments. Used by install_packages so an
    # already-approved package is not asked about a second time.
    needs_approval: Callable[[dict[str, Any]], bool] | None = None

    def to_openai_schema(self) -> dict[str, Any]:
        """The function schema in the OpenAI-compatible tool format DeepSeek uses."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _integer(description: str, *, default: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def _boolean(description: str, *, default: bool = False) -> dict[str, Any]:
    return {"type": "boolean", "description": description, "default": default}


_LIST_DIR = Tool(
    name="list_dir",
    description=(
        "List the files and subdirectories in a directory inside the project. "
        "Use this to discover what documents are available before reading them."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": _string("Directory path relative to the project root. Defaults to the root."),
            "limit": _integer("Maximum number of entries to return.", default=200),
        },
        "required": [],
    },
    handler=fs_tools.list_dir,
    approval="never",
    summary="List a directory",
)

_READ_FILE = Tool(
    name="read_file",
    description=(
        "Read the contents of a file in the project. Supports plain text files, "
        "CSV/JSON/Markdown, PDFs, and Office documents — Word, Excel, PowerPoint "
        "and OpenDocument — whose text is extracted automatically, so no converter "
        "is needed to read them. Large files are returned in windows: check "
        "'has_more' and 'next_start_line' and call again to continue reading."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": _string("File path relative to the project root."),
            "start_line": _integer("First line to read, 1-based.", default=1),
            "max_lines": _integer("How many lines to read.", default=fs_tools.DEFAULT_READ_LINES),
        },
        "required": ["path"],
    },
    handler=fs_tools.read_file,
    approval="never",
    summary="Read a file",
)

_SEARCH_FILES = Tool(
    name="search_files",
    description=(
        "Search the text of files in the project for a regular expression. "
        "Use this to find where something is mentioned without reading every file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": _string("Regular expression to search for."),
            "path": _string("Directory to search in. Defaults to the project root."),
            "glob": _string("Optional filename filter, for example '*.md'."),
            "max_results": _integer("Maximum number of matches to return.", default=60),
            "case_sensitive": _boolean("Match case exactly.", default=False),
        },
        "required": ["pattern"],
    },
    handler=fs_tools.search_files,
    approval="never",
    summary="Search file contents",
)

_WRITE_FILE = Tool(
    name="write_file",
    description=(
        "Create a new file or replace an existing one in the project. Use this for "
        "notes, code, Markdown, CSV and other text output. For spreadsheets and PDFs "
        "use make_spreadsheet and make_pdf instead so the formatting is correct."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": _string("File path relative to the project root."),
            "content": _string("Full contents to write."),
            "overwrite": _boolean("Replace the file if it already exists.", default=True),
        },
        "required": ["path", "content"],
    },
    handler=fs_tools.write_file,
    approval="ask",
    summary="Write a file",
    mutating=True,
)

_EDIT_FILE = Tool(
    name="edit_file",
    description=(
        "Replace an exact string inside an existing file. Read the file first and copy "
        "the text exactly, including indentation. The old text must be unique in the "
        "file unless replace_all is true."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": _string("File path relative to the project root."),
            "old_string": _string("Exact existing text to replace."),
            "new_string": _string("Replacement text."),
            "replace_all": _boolean("Replace every occurrence.", default=False),
        },
        "required": ["path", "old_string", "new_string"],
    },
    handler=fs_tools.edit_file,
    approval="ask",
    summary="Edit a file",
    mutating=True,
)

_RUN_PYTHON = Tool(
    name="run_python",
    description=(
        "Run Python code in the project directory and return its stdout and stderr. "
        "reportlab, openpyxl, pypdf and matplotlib are installed. Use this for custom "
        "analysis or file processing. Prefer make_pdf, make_spreadsheet and make_chart "
        "for standard documents."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": _string("Python source to execute."),
            "timeout": {
                "type": "number",
                "description": "Seconds before the run is stopped.",
                "default": 120,
            },
        },
        "required": ["code"],
    },
    handler=shell_tools.run_python,
    approval="ask",
    summary="Run Python code",
    mutating=True,
)

_RUN_SHELL = Tool(
    name="run_shell",
    description=(
        "Run a shell command inside the project directory. Use sparingly; prefer the "
        "dedicated file tools. The command runs in the project root and is stopped "
        "after the timeout."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": _string("Shell command to run."),
            "timeout": {
                "type": "number",
                "description": "Seconds before the command is stopped.",
                "default": 60,
            },
        },
        "required": ["command"],
    },
    handler=shell_tools.run_shell,
    approval="ask",
    summary="Run a shell command",
    mutating=True,
)

_MAKE_PDF = Tool(
    name="make_pdf",
    description=(
        "Create a formatted PDF document in the project. Build it from structured "
        "blocks: headings, paragraphs, bullet lists and tables. This is the "
        "recommended way to produce a PDF report."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": _string("Output path ending in .pdf, relative to the project root."),
            "title": _string("Document title."),
            "subtitle": _string("Optional subtitle shown under the title."),
            "author": _string("Optional author name."),
            "page_size": {
                "type": "string",
                "enum": ["LETTER", "A4"],
                "default": "LETTER",
                "description": "Paper size.",
            },
            "blocks": {
                "type": "array",
                "description": "Ordered content blocks.",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "heading",
                                "paragraph",
                                "bullet_list",
                                "table",
                                "image",
                                "page_break",
                                "spacer",
                            ],
                        },
                        "text": _string("Text for a heading or paragraph."),
                        "level": _integer("Heading level, 1 to 4.", default=2),
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Bullet items.",
                        },
                        "headers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Table header cells.",
                        },
                        "rows": {
                            "type": "array",
                            "items": {"type": "array"},
                            "description": "Table body rows, each a list of cell values.",
                        },
                        "path": _string("Image path, for type 'image'."),
                        "caption": _string("Image caption."),
                        "height": {"type": "number", "description": "Spacer height in points."},
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["path", "title", "blocks"],
    },
    handler=artifacts.make_pdf,
    approval="ask",
    summary="Create a PDF",
    mutating=True,
)

_MAKE_SPREADSHEET = Tool(
    name="make_spreadsheet",
    description=(
        "Create an Excel .xlsx workbook in the project. Provide one or more sheets, "
        "each with optional headers and a list of rows."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": _string("Output path ending in .xlsx, relative to the project root."),
            "sheets": {
                "type": "array",
                "description": "Workbook sheets.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": _string("Sheet name."),
                        "headers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Column headers.",
                        },
                        "rows": {
                            "type": "array",
                            "items": {"type": "array"},
                            "description": "Rows, each a list of cell values.",
                        },
                        "freeze_header": _boolean("Freeze the header row.", default=True),
                    },
                    "required": ["rows"],
                },
            },
        },
        "required": ["path", "sheets"],
    },
    handler=artifacts.make_spreadsheet,
    approval="ask",
    summary="Create a spreadsheet",
    mutating=True,
)

_MAKE_CHART = Tool(
    name="make_chart",
    description=(
        "Render a chart to a PNG or SVG file in the project. The saved image can then "
        "be embedded into a PDF with make_pdf using an 'image' block."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": _string("Output path ending in .png or .svg."),
            "chart_type": {
                "type": "string",
                "enum": ["line", "bar", "hbar", "pie", "scatter", "area", "hist"],
            },
            "title": _string("Chart title."),
            "x_label": _string("X axis label."),
            "y_label": _string("Y axis label."),
            "width": {"type": "number", "description": "Figure width in inches.", "default": 10},
            "height": {"type": "number", "description": "Figure height in inches.", "default": 6},
            "series": {
                "type": "array",
                "description": "One or more data series.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": _string("Series label, used in the legend."),
                        "x": {
                            "type": "array",
                            "description": "X values (category labels or numbers).",
                        },
                        "y": {
                            "type": "array",
                            "description": "Y values.",
                        },
                    },
                    "required": ["y"],
                },
            },
        },
        "required": ["path", "chart_type", "series"],
    },
    handler=artifacts.make_chart,
    approval="ask",
    summary="Create a chart",
    mutating=True,
)


_CONVERT_DOCUMENT = Tool(
    name="convert_document",
    description=(
        "Convert a document to another format. Works with nothing installed: the "
        "built-in converter reads Word, Excel, PowerPoint, OpenDocument, PDF and "
        "text files, and writes PDF, txt, csv, html and xlsx. If LibreOffice is "
        "present it is used for the formats the built-in converter cannot write "
        "(docx, odt, rtf, ods, odp, pptx, png, jpg) and as a fallback that keeps "
        "layout. Pass backend='builtin' to guarantee no external program runs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "source": _string("Existing file to convert, relative to the project root."),
            "target": {
                "type": "string",
                "enum": documents.list_supported_targets(),
                "description": "Output format.",
            },
            "output": _string(
                "Output path relative to the project root. Defaults to the source "
                "name with the new extension."
            ),
            "overwrite": _boolean("Replace the output if it already exists.", default=True),
            "backend": {
                "type": "string",
                "enum": ["auto", "builtin", "libreoffice"],
                "description": (
                    "Which engine to use. 'auto' (default) prefers the built-in "
                    "converter and falls back to LibreOffice when it is installed; "
                    "'builtin' never runs an external program; 'libreoffice' "
                    "requires it."
                ),
                "default": "auto",
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Seconds before LibreOffice is stopped. Ignored by the built-in "
                    "converter. A cold start is slow."
                ),
                "default": 180,
            },
        },
        "required": ["source", "target"],
    },
    handler=documents.convert_document,
    approval="ask",
    summary="Convert a document",
    mutating=True,
)


def _install_needs_approval(arguments: dict[str, Any]) -> bool:
    """Ask only about packages this project has not already approved.

    Approving a set of packages once records it in the project's requirements, so
    re-installing the same set (after a fresh clone, say) does not prompt again.
    """
    packages = arguments.get("packages")
    if not isinstance(packages, list) or not packages:
        return True
    root = _APPROVAL_ROOT.get()
    if root is None:
        return True
    try:
        pending = environment.new_requirements(root, [str(p) for p in packages])
    except Exception:  # an unreadable config should not grant approval
        return True
    return bool(pending)


class ToolRegistry:
    """Holds the available tools and dispatches calls to them."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        # all_tools() is resolved lazily so the registry can be constructed
        # before the trailing tool declarations have run.
        selected = tools if tools is not None else all_tools()
        for tool in selected:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """All tool schemas, for the ``tools`` field of a chat completion."""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def requires_approval(self, name: str, arguments: dict[str, Any] | None = None) -> bool:
        """Whether this call needs approval.

        A tool may narrow the decision with its own hook, so the same tool can be
        pre-approved for some arguments and not others.
        """
        tool = self._tools.get(name)
        if tool is None or tool.approval != "ask":
            return False
        if tool.needs_approval is not None and arguments is not None:
            try:
                return bool(tool.needs_approval(arguments))
            except Exception:  # fail closed: an error in the hook must not skip approval
                return True
        return True

    def is_mutating(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.mutating)

    def summary_for(self, name: str) -> str:
        tool = self._tools.get(name)
        return tool.summary if tool else name

    async def dispatch(self, name: str, ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        """Call a tool, converting any failure into a result the model can see.

        An unknown tool name or a bad argument is reported back to the model as a
        normal tool error rather than raising, because the model can often fix a
        malformed call itself.
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                error=(f"Unknown tool {name!r}. Available tools: {', '.join(self.names())}."),
            )

        try:
            outcome = tool.handler(ctx, **arguments)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except TypeError as exc:
            return ToolResult(
                ok=False,
                error=(
                    f"Invalid arguments for {name}: {exc}. "
                    "Check the parameter names and types in the tool schema."
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("tool %s raised", name)
            return ToolResult(
                ok=False,
                error=f"{name} failed: {type(exc).__name__}: {exc}",
                display=f"{name} crashed",
            )

        if not isinstance(outcome, ToolResult):  # pragma: no cover - handler contract
            return ToolResult(
                ok=False,
                error=f"{name} returned an unexpected result type ({type(outcome).__name__}).",
            )
        return outcome


def default_tool_list() -> list[Tool]:
    """Every tool the agent may use."""
    return [
        _LIST_DIR,
        _READ_FILE,
        _SEARCH_FILES,
        _WRITE_FILE,
        _EDIT_FILE,
        _RUN_PYTHON,
        _RUN_SHELL,
        _MAKE_PDF,
        _MAKE_SPREADSHEET,
        _MAKE_CHART,
        _CONVERT_DOCUMENT,
    ]


_DEFAULT_REGISTRY: ToolRegistry | None = None


def default_registry() -> ToolRegistry:
    """Return the shared registry, building it on first use.

    Built lazily because the tool list includes definitions that appear at the
    end of this module; constructing it at import time would race those.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ToolRegistry()
    return _DEFAULT_REGISTRY


async def _search_packages_handler(ctx: ToolContext, query: str, limit: int = 8) -> ToolResult:
    """Look up packages on PyPI."""
    try:
        results = await environment.search_pypi(query, limit=max(1, min(int(limit), 20)))
    except Exception as exc:  # a search failure is reported, not raised
        return ToolResult(
            ok=False,
            error=f"Could not reach the package index: {type(exc).__name__}: {exc}",
        )

    if not results:
        return ToolResult(
            ok=True,
            data={
                "query": query,
                "results": [],
                "note": "No packages matched. The exact name may still work.",
            },
            display=f"No package results for {query!r}",
        )

    return ToolResult(
        ok=True,
        data={"query": query, "results": [r.to_dict() for r in results]},
        display=f"Found {len(results)} package(s) for {query!r}",
    )


async def _install_packages_handler(
    ctx: ToolContext, packages: list[str], upgrade: bool = False
) -> ToolResult:
    """Install packages into the project's isolated environment."""
    if not isinstance(packages, list) or not packages:
        return ToolResult(
            ok=False, error="Provide a list of package names, for example ['pandas']."
        )

    valid, refused = environment.validate_requirements([str(p) for p in packages])
    if refused:
        return ToolResult(ok=False, error=" ".join(refused))
    if not valid:
        return ToolResult(ok=False, error="No valid package names were supplied.")

    ok, output, installed = await environment.install_packages(
        ctx.root, valid, upgrade=bool(upgrade)
    )
    if not ok:
        return ToolResult(
            ok=False,
            error=output,
            display=f"Could not install {', '.join(valid)}",
        )

    # Report the interpreter so the model knows its code will see these imports.
    status = await environment.project_env_status(ctx.root)
    return ToolResult(
        ok=True,
        data={
            "installed": installed,
            "requirements": valid,
            "environment": status.to_dict(),
            "output": output,
        },
        display=(
            f"Installed {', '.join(installed or valid)} into the project environment "
            f"({status.package_count} packages)"
        ),
    )


async def _search_history_handler(ctx: ToolContext, query: str, limit: int = 5) -> ToolResult:
    """Search earlier conversations in this project.

    The retrieval half of durable memory. The notebook holds what the agent chose
    to record; this finds what it did not, so knowledge from a previous session is
    reachable rather than lost.
    """
    if ctx.store is None:
        return ToolResult(ok=False, error="Conversation history is not available here.")
    needle = (query or "").strip()
    if not needle:
        return ToolResult(ok=False, error="Provide something to search for.")

    results = ctx.store.search_conversations(
        needle, limit=max(1, min(int(limit), 20)), exclude_session=ctx.session_id or None
    )
    if not results:
        return ToolResult(
            ok=True,
            data={"query": needle, "results": []},
            display=f"Nothing in earlier conversations mentions {needle!r}",
        )
    return ToolResult(
        ok=True,
        data={"query": needle, "results": results},
        display=f"Found {len(results)} earlier mention(s) of {needle!r}",
    )


async def _remember_handler(ctx: ToolContext, note: str, *, replace: bool = False) -> ToolResult:
    """Append to the project notebook, which is injected into every future turn."""
    cleaned = (note or "").strip()
    if not cleaned:
        return ToolResult(ok=False, error="Provide something to record.")

    ok, error = environment.write_notes(ctx.root, cleaned, append=not replace)
    if not ok:
        return ToolResult(ok=False, error=error)

    stored = environment.read_notes(ctx.root)
    return ToolResult(
        ok=True,
        data={"recorded": cleaned, "notebook_chars": len(stored), "replaced": bool(replace)},
        display=(
            f"{'Replaced' if replace else 'Added to'} the project notebook "
            f"({len(stored)} characters total)"
        ),
    )


async def _read_notes_handler(ctx: ToolContext) -> ToolResult:
    """Read back the project notebook."""
    text = environment.read_notes(ctx.root)
    if not text:
        return ToolResult(
            ok=True,
            data={"notes": "", "exists": False},
            display="The project notebook is empty",
        )
    return ToolResult(
        ok=True,
        data={"notes": text, "exists": True, "characters": len(text)},
        display=f"Read {len(text)} characters of project notes",
    )


async def _environment_info_handler(ctx: ToolContext) -> ToolResult:
    """Describe the interpreter run_python will use."""
    status = await environment.project_env_status(ctx.root)
    where = "the project environment" if status.exists else "the application environment"
    return ToolResult(
        ok=True,
        data=status.to_dict(),
        display=(
            f"Using {where}: {status.package_count} package(s) installed"
            if status.exists
            else "Using the application environment; no project packages installed yet"
        ),
    )


def build_registry_for_project(
    root: Any, config: Any, *, manager: Any = None
) -> tuple[ToolRegistry, Any]:
    """Build a registry that includes this project's MCP tools.

    MCP tools are mounted under a ``<server>__<tool>`` name so two servers cannot
    collide, and they default to requiring approval: a remote server is exactly
    the place where an unapproved side effect is least visible.

    Returns ``(registry, manager)``. The manager may be ``None`` when the project
    configures no servers, and the caller owns closing it either way.
    """
    from surtitle.tools.mcp import McpManager

    registry = ToolRegistry()
    enabled = list(getattr(config, "enabled_mcp_servers", []) or [])
    if not enabled:
        return registry, None

    manager = manager or McpManager(enabled, root=root)
    if manager.connected:
        return registry, manager

    # connect_all is async, so it cannot run here; the caller awaits this
    # function's companion. See mount_mcp_tools.
    return registry, manager


async def mount_mcp_tools(registry: ToolRegistry, manager: Any) -> ToolRegistry:
    """Connect a project's MCP servers and register their tools.

    Failures are collected rather than raised: a broken server config should cost
    that server's tools, not the whole session.
    """
    if manager is None:
        return registry

    tools = await manager.connect_all()
    for mcp_tool in tools:
        trusted = False
        client = manager.client_for(mcp_tool.server)
        if client is not None:
            trusted = mcp_tool.name in client.config.trusted_tools
        registry.register(
            Tool(
                name=mcp_tool.qualified_name,
                description=(
                    f"[MCP: {mcp_tool.server}] {mcp_tool.description}"
                    if mcp_tool.description
                    else f"[MCP: {mcp_tool.server}] {mcp_tool.name}"
                ),
                parameters=mcp_tool.input_schema or {"type": "object", "properties": {}},
                handler=manager.make_handler(mcp_tool),
                # Remote tools ask first unless the project explicitly trusts them.
                approval="never" if trusted else "ask",
                summary=f"{mcp_tool.server}: {mcp_tool.name}",
                mutating=True,
                mcp_server=mcp_tool.server,
            )
        )
    return registry


# These are declared after their handlers so the module reads top to bottom
# without forward references.
_SEARCH_PACKAGES = Tool(
    name="search_packages",
    description=(
        "Search PyPI for Python packages by name or keyword. Use this before "
        "install_packages when you are unsure of the exact package name."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": _string("Package name or search terms."),
            "limit": _integer("Maximum number of results.", default=8),
        },
        "required": ["query"],
    },
    handler=_search_packages_handler,
    approval="never",
    summary="Search PyPI",
)

_INSTALL_PACKAGES = Tool(
    name="install_packages",
    description=(
        "Install Python packages into this project's own isolated environment so "
        "run_python can import them. It does not modify the application itself. "
        "Each new package needs the user's approval once; approved packages are "
        "remembered for this project. After installing, call run_python to use the "
        "library."
    ),
    parameters={
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Package requirements, for example ['pandas>=2.2', 'openpyxl']. "
                    "Plain names with optional version specifiers only."
                ),
            },
            "upgrade": _boolean("Upgrade already-installed packages.", default=False),
        },
        "required": ["packages"],
    },
    handler=_install_packages_handler,
    approval="ask",
    summary="Install Python packages",
    mutating=True,
    needs_approval=_install_needs_approval,
)

_ENVIRONMENT_INFO = Tool(
    name="environment_info",
    description=(
        "Report the Python environment run_python executes against, and which "
        "packages are installed. Use it to check whether a library is already "
        "available before installing it."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_environment_info_handler,
    approval="never",
    summary="Show the Python environment",
)


_REMEMBER = Tool(
    name="remember",
    description=(
        "Record a durable fact about this project in its notebook. The notebook is "
        "shown to you at the start of every future conversation, so use it for what "
        "you would otherwise have to rediscover: which machine is down, where a "
        "file or command lives, what a term means, decisions already made. Record "
        "conclusions and locations, not a transcript of what you did."
    ),
    parameters={
        "type": "object",
        "properties": {
            "note": _string(
                "One short, self-contained fact. Include the detail needed to act on "
                "it later without re-deriving it."
            ),
            "replace": _boolean(
                "Replace the whole notebook instead of appending. Use only to correct "
                "notes that have become wrong.",
                default=False,
            ),
        },
        "required": ["note"],
    },
    handler=_remember_handler,
    approval="never",
    summary="Record a project note",
    mutating=True,
)

_READ_NOTES = Tool(
    name="read_notes",
    description=(
        "Read the project notebook. Its contents are already shown to you at the "
        "start of each conversation, so this is only needed to re-read it in full "
        "after it has grown."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_read_notes_handler,
    approval="never",
    summary="Read the project notebook",
)


_SEARCH_HISTORY = Tool(
    name="search_history",
    description=(
        "Search this project's earlier conversations. Use it before re-deriving "
        "something or asking the user to repeat themselves: if a previous session "
        "already established how to reach a system, why a machine is down, or where "
        "a file lives, it is probably recorded here. Distinct from the notebook, "
        "which holds only what was deliberately written down."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": _string("Words or an identifier to look for, e.g. '4C-120 iod'."),
            "limit": _integer("Maximum number of excerpts to return.", default=5),
        },
        "required": ["query"],
    },
    handler=_search_history_handler,
    approval="never",
    summary="Search earlier conversations",
)


def all_tools() -> list[Tool]:
    """Every tool the agent may use, including the ones declared last."""
    return [
        *default_tool_list(),
        _SEARCH_PACKAGES,
        _INSTALL_PACKAGES,
        _ENVIRONMENT_INFO,
        _REMEMBER,
        _READ_NOTES,
        _SEARCH_HISTORY,
    ]
