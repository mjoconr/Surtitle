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

from surtitle.tools import (
    artifacts,
    documents,
    environment,
    fs_tools,
    jobs,
    shell_tools,
    skills,
    web_tools,
)
from surtitle.tools.fs_tools import ToolContext, ToolResult
from surtitle.vcs.guide import DETAIL_LEVELS, detail_menu

__all__ = [
    "GOAL_TOOL",
    "SKILL_TOOL",
    "SUBAGENT_TOOL",
    "TODO_TOOL",
    "WEB_FETCH_TOOL",
    "Tool",
    "ToolRegistry",
    "build_registry_for_project",
    "default_registry",
]

# Tools about the conversation's own background jobs. Not offered to a sub-agent:
# the jobs belong to the parent, and a child that could read or kill them would be
# acting on processes it cannot see started.
_JOB_TOOLS = ("run_background", "job_output", "job_kill")

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

    def read_only(self) -> ToolRegistry:
        """The tools that can look but not change: no side effects, no approvals.

        Taken from this registry's own policy rather than from a list of names, so
        a tool that later gains a way to write drops out of a sub-agent's reach
        without anyone having to remember to remove it here.

        A tool that needs the conversation's own state is excluded as well: a
        sub-agent has no conversation — nothing it did is stored — so a plan it
        tried to write would have nowhere to go, and a search of past
        conversations would be a search of somebody else's. So is the tool that
        starts a sub-agent: one level of delegation, not a tree.
        """
        return ToolRegistry(
            [
                tool
                for tool in self._tools.values()
                if not tool.mutating
                and tool.approval == "never"
                and tool.name not in _NOT_FOR_A_SUBAGENT
            ]
        )

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


TODO_TOOL = "todo_write"
GOAL_TOOL = "goal_write"
SUBAGENT_TOOL = "subagent"
SKILL_TOOL = "skill"
WEB_FETCH_TOOL = "web_fetch"

# How much of a fetched page the model is given. A page is mostly navigation, and
# the answer to a question about one is usually near the top; the rest is said to
# have been left out rather than silently dropped.
_WEB_FETCH_CHARS = 12_000

# Read-only tools that are still not for a sub-agent. Two need the conversation
# they belong to — `todo_write` writes its plan, `search_history` reads other
# conversations, and a sub-agent has neither — and the third starts another
# sub-agent, which is how a delegation becomes a fork bomb.
_NOT_FOR_A_SUBAGENT = frozenset(
    {TODO_TOOL, GOAL_TOOL, "search_history", SUBAGENT_TOOL, *_JOB_TOOLS}
)


def _todo_write_handler(ctx: ToolContext, todos: list[dict[str, Any]] | None = None) -> ToolResult:
    """Record the agent's plan for the conversation.

    The plan is not a file and nothing about it is destructive: it is state the
    user watches to see what the agent believes it is doing and how far it has
    got. That is why it needs no approval — and why it is worth the model
    updating as it goes rather than only when asked.
    """
    if ctx.store is None or not ctx.session_id:
        return ToolResult(
            ok=False,
            error="There is nowhere to record a plan in this session.",
        )
    if not isinstance(todos, list) or not todos:
        return ToolResult(
            ok=False,
            error=(
                "Provide the full plan as a non-empty 'todos' array. To clear the plan, "
                "send every item with status 'completed'."
            ),
        )
    stored = ctx.store.set_todos(ctx.session_id, todos)
    if not stored:
        return ToolResult(
            ok=False,
            error="Every item needs non-empty 'content'. Nothing was recorded.",
        )
    done = sum(1 for item in stored if item["status"] == "completed")
    active = next((item for item in stored if item["status"] == "in_progress"), None)
    lines = [
        f"[{item['status']}] {item['content']}"
        + (f" — {item['activeForm']}" if item["activeForm"] else "")
        for item in stored
    ]
    head = f"Plan recorded: {done}/{len(stored)} complete"
    if active:
        head += f", now on: {active['content']}"
    return ToolResult(
        ok=True,
        data={"todos": stored, "completed": done, "total": len(stored)},
        display=head + "\n" + "\n".join(lines),
    )


_TODO_WRITE = Tool(
    name=TODO_TOOL,
    description=(
        "Record and update your plan for the current piece of work, so the user can "
        "see what you are doing and how far you have got. Call it once at the start "
        "of anything that takes more than a couple of steps, and again whenever an "
        "item starts, finishes or turns out to be unnecessary. Send the *whole* "
        "list every time, not just the item that changed: it replaces the previous "
        "plan. Exactly one item may be in_progress at a time. Keep items short and "
        "concrete ('Index the sampler logs', not 'Investigate the problem'). This "
        "tool only records the plan; it does not do the work."
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The complete plan, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": _string("Imperative form: what is to be done."),
                        "activeForm": _string(
                            "Present continuous form, shown while it is in progress."
                        ),
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "Where this item stands.",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
    handler=_todo_write_handler,
    approval="never",
    summary="Record a plan",
)


def _web_fetch_handler(ctx: ToolContext, url: str = "", **_ignored: Any) -> ToolResult:
    """Fetch a page and hand back its readable text.

    Everything that makes this safe lives in `tools/web_tools.py` — the address
    checks, the manual redirects, the size and content-type limits — so that the
    same guards apply to any caller and can be tested without a tool dispatch. What
    is here is the part that belongs to a tool: turning a failure into something the
    model can act on, because a raised exception would end the turn.
    """
    if not str(url or "").strip():
        return ToolResult(ok=False, error="Give the URL to fetch.")
    try:
        page = web_tools.fetch_page(str(url))
    except web_tools.FetchError as exc:
        return ToolResult(ok=False, error=str(exc))
    except Exception as exc:  # a broken site must not break the turn
        return ToolResult(ok=False, error=f"Could not fetch {url}: {type(exc).__name__}: {exc}")

    text = page.text
    clipped = len(text) > _WEB_FETCH_CHARS
    if clipped:
        text = text[:_WEB_FETCH_CHARS]
    if page.truncated or clipped:
        text += (
            f"\n\n[This is the beginning of {page.url}. Fetch a more specific page if "
            "what you need is further down.]"
        )
    return ToolResult(
        ok=True,
        data={
            "url": page.url,
            "status": page.status,
            "content_type": page.content_type,
            "text": text,
        },
        display=f"fetched {page.url} ({page.status}, {len(text)} chars)",
    )


async def _subagent_handler(
    ctx: ToolContext, task: str = "", label: str = "", **_ignored: Any
) -> ToolResult:
    """Hand one part of the work to an agent that only reads.

    Answering a question is mostly reading, and reading is what fills a
    conversation with detail that has already been used and will never be needed
    again. A sub-agent does that reading in its own context and returns the answer,
    so the conversation keeps the answer and loses the archaeology.
    """
    if ctx.subagent is None:
        return ToolResult(
            ok=False,
            error="Sub-agents are not available in this session; do this part yourself.",
        )
    if not str(task or "").strip():
        return ToolResult(ok=False, error="Give the sub-agent the task to carry out.")
    return await ctx.subagent(str(task), str(label or ""))


_SUBAGENT = Tool(
    name=SUBAGENT_TOOL,
    description=(
        "Hand one self-contained piece of reading to a sub-agent and get back what "
        "it found. Use it when answering needs several files read, or when there "
        "are independent questions that can be looked into at once: the reading "
        "happens in the sub-agent's own context, so it does not fill this "
        "conversation. The sub-agent can read, list and search, and nothing else — "
        "it cannot write, run commands, install, or ask the user anything — so work "
        "needing those is yours. Put everything it needs in the task: it has no "
        "memory of this conversation and cannot ask you a follow-up question. "
        "Several calls in one turn run at the same time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": _string(
                "The whole question, self-contained: what to find out and what to "
                "report back. Include the file names or areas you already suspect, "
                "and say what would settle it."
            ),
            "label": _string(
                "A few words naming this part of the work, for the user to hear — "
                "'the retry configuration'. Short: it is spoken aloud."
            ),
        },
        "required": ["task"],
    },
    handler=_subagent_handler,
    approval="never",
    summary="Read files in the background",
)


_WEB_FETCH = Tool(
    name=WEB_FETCH_TOOL,
    description=(
        "Fetch a URL and read it — documentation, a changelog, a release note, an "
        "error page, a specification. Use it when the answer is published somewhere "
        "and this project does not contain it; do not use it for anything the "
        "project's own files answer, because reading them is free and this is not. "
        "It reads http and https, follows redirects, refuses addresses on the local "
        "network, and returns the beginning of a long page. It cannot search: you "
        "need the URL, or one you can construct from something you have read."
    ),
    parameters={
        "type": "object",
        "properties": {"url": _string("The full URL to fetch, including the scheme.")},
        "required": ["url"],
    },
    handler=_web_fetch_handler,
    # Not `never`: reading a page is harmless, *requesting* one is not. The URL is
    # the part of a request that can carry project data out, and it is what the
    # approval prompt shows. `run_shell` is gated for the same reason — it could
    # always have done this — and a project that trusts this tool stops being asked.
    approval="ask",
    summary="Fetch a page from the internet",
)


_RUN_BACKGROUND = Tool(
    name="run_background",
    description=(
        "Start a shell command and return immediately, naming the job. Use it for "
        "anything slow — a build, a test suite, a long search, a command on a remote "
        "machine — so the turn is not blocked while it runs. Read it later with "
        "job_output; it keeps running whether or not you do. Same working directory "
        "and permissions as run_shell, and stopped when the conversation closes."
    ),
    parameters={
        "type": "object",
        "properties": {"command": _string("Shell command to run in the project root.")},
        "required": ["command"],
    },
    handler=jobs.run_background,
    approval="ask",
    summary="Start a command in the background",
    mutating=True,
)

_JOB_OUTPUT = Tool(
    name="job_output",
    description=(
        "Read the end of a background job's output and whether it has finished. "
        "Give `wait_seconds` to wait for it to finish rather than asking again in a "
        "loop — that is one step instead of several, and it is bounded."
    ),
    parameters={
        "type": "object",
        "properties": {
            "job": _string("The job name, as run_background returned it."),
            "wait_seconds": {
                "type": "number",
                "description": "Wait this long for the job to finish before reporting.",
                "default": 0,
            },
        },
        "required": ["job"],
    },
    handler=jobs.job_output,
    approval="never",
    summary="Read a background job's output",
)

_JOB_KILL = Tool(
    name="job_kill",
    description=(
        "Stop a background job and everything it started. Use it when a command is "
        "wrong, stuck, or no longer needed — a job nobody wants should not keep "
        "running quietly."
    ),
    parameters={
        "type": "object",
        "properties": {"job": _string("The job name, as run_background returned it.")},
        "required": ["job"],
    },
    handler=jobs.job_kill,
    approval="never",
    summary="Stop a background job",
)


def _goal_write_handler(
    ctx: ToolContext, goal: str = "", achieved: bool = False, **_ignored: Any
) -> ToolResult:
    """Record what the conversation is for, or that it has been reached.

    The plan is what is being done; this is what it is for. Three calls, decided by
    the arguments rather than by three tools, because they are one idea: a goal
    stated, a goal reached, a goal abandoned.
    """
    if ctx.store is None or not ctx.session_id:
        return ToolResult(ok=False, error="There is nowhere to record a goal in this session.")

    text = (goal or "").strip()
    if not text and not achieved:
        ctx.store.set_goal(ctx.session_id, None)
        return ToolResult(
            ok=True,
            data={"goal": None, "goal_achieved": False},
            display="Goal cleared.",
        )

    stored = ctx.store.set_goal(ctx.session_id, text or None, achieved=achieved)
    if stored is None:  # pragma: no cover - the session is checked above
        return ToolResult(ok=False, error="That conversation no longer exists.")

    if achieved:
        return ToolResult(
            ok=True,
            data={"goal": stored.goal, "goal_achieved": True},
            display=f"Goal reached: {stored.goal or '(none recorded)'}",
        )
    return ToolResult(
        ok=True,
        data={"goal": stored.goal, "goal_achieved": False},
        display=f"Goal recorded: {stored.goal}",
    )


_GOAL_WRITE = Tool(
    name=GOAL_TOOL,
    description=(
        "Record what this conversation is for, in one sentence, so it survives past "
        "the turn that has it in mind — and mark it reached when it is. Set it when "
        "the user asks for something that will take more than a turn, or says what "
        "the objective is; a goal is not a plan, it is the thing the plan is for "
        "('get the handoff cap into 122 without lowering the scales throw', not "
        "'read Conveyor.lpc'). Mark it achieved the moment it is done, so a finished "
        "conversation stops being nudged onward. Send no arguments to clear a goal "
        "that is no longer what you are doing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": _string(
                "The objective, one sentence, in the user's terms. Omit to mark the "
                "current goal achieved, or to clear it."
            ),
            "achieved": _boolean("Mark the recorded goal as reached.", default=False),
        },
        "required": [],
    },
    handler=_goal_write_handler,
    approval="never",
    summary="Record the objective",
)


def _skill_handler(ctx: ToolContext, name: str = "", **_ignored: Any) -> ToolResult:
    """Read one of the project's written-down procedures, in full."""
    personal = skills.personal_dir(ctx)
    found = skills.read_skill(str(name), ctx.root, personal_dir=personal)
    if found is None:
        available = (
            ", ".join(item.name for item in skills.discover(ctx.root, personal_dir=personal))
            or "none"
        )
        return ToolResult(
            ok=False,
            error=f"There is no skill called {name!r} here. Available: {available}.",
        )
    skill, body, companions = found
    return ToolResult(
        ok=True,
        data={
            "name": skill.name,
            "description": skill.description,
            "source": skill.source,
            "body": body,
            "files": companions,
        },
        display=f"skill {skill.name}: {len(body)} characters"
        + (f", {len(companions)} file(s) beside it" if companions else ""),
    )


_SKILL = Tool(
    name=SKILL_TOOL,
    description=(
        "Read one of this project's skills in full — a procedure somebody wrote down "
        "for work like this. The notes you are given each turn list them with a line "
        "about each; when a task matches one, read it before you start and follow it "
        "rather than improvising. A skill may have files beside it (a template, a "
        "script); those are named in the result, and read_file reaches them. This "
        "tool only reads: it does not do what the skill describes."
    ),
    parameters={
        "type": "object",
        "properties": {"name": _string("The skill's name, exactly as it is listed in your notes.")},
        "required": ["name"],
    },
    handler=_skill_handler,
    approval="never",
    summary="Read a project skill",
)


def default_tool_list() -> list[Tool]:
    """Every tool the agent may use."""
    return [
        _LIST_DIR,
        _READ_FILE,
        _SEARCH_FILES,
        _WEB_FETCH,
        _WRITE_FILE,
        _EDIT_FILE,
        _RUN_PYTHON,
        _RUN_SHELL,
        _RUN_BACKGROUND,
        _JOB_OUTPUT,
        _JOB_KILL,
        _MAKE_PDF,
        _MAKE_SPREADSHEET,
        _MAKE_CHART,
        _CONVERT_DOCUMENT,
        _TODO_WRITE,
        _GOAL_WRITE,
        _SUBAGENT,
        _SKILL,
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
    """Search this project's conversations, including the one in progress.

    The retrieval half of durable memory. The notebook holds what the agent chose
    to record; this finds what it did not, so knowledge from a previous session is
    reachable rather than lost.

    The current conversation is deliberately included. Excluding it looked tidy —
    "history" reads as "earlier sessions" — but it took away the only way to
    recover what had just been done and said, in exactly the situation that needs
    it: a long session whose earlier turns have aged out of the replay window. An
    agent that has lost its own record and cannot search it will re-derive the
    work, or attribute it to somebody else.
    """
    if ctx.store is None:
        return ToolResult(ok=False, error="Conversation history is not available here.")
    needle = (query or "").strip()
    if not needle:
        return ToolResult(ok=False, error="Provide something to search for.")

    results = ctx.store.search_conversations(needle, limit=max(1, min(int(limit), 20)))
    # Say which conversation each hit came from. Including the current session is
    # the point — it is how the agent recovers work that has aged out of its
    # context — but a hit from its own last utterance must not read as established
    # fact from somewhere else. The label is the difference.
    for item in results:
        item["from"] = (
            "this conversation"
            if ctx.session_id and item["session_id"] == ctx.session_id
            else "an earlier conversation"
        )
    if not results:
        return ToolResult(
            ok=True,
            data={"query": needle, "results": []},
            display=f"Nothing in this project's conversations mentions {needle!r}",
        )
    mine = sum(1 for item in results if item["from"] == "this conversation")
    elsewhere = len(results) - mine
    where = []
    if mine:
        where.append(f"{mine} in this conversation")
    if elsewhere:
        where.append(f"{elsewhere} in earlier conversations")
    return ToolResult(
        ok=True,
        data={"query": needle, "results": results},
        display=f"Found {len(results)} mention(s) of {needle!r} (" + ", ".join(where) + ")",
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
        "Search this project's conversations, including the current one. Use it "
        "before re-deriving something or asking the user to repeat themselves: if "
        "an earlier turn — or a previous session — already established how to reach "
        "a system, why a machine is down, or where a file lives, it is probably "
        "recorded here. This is also how you recover your own earlier work when it "
        "has aged out of your context, so reach for it before concluding that "
        "somebody else must have done something. Distinct from the notebook, which "
        "holds only what was deliberately written down."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": _string("Words or an identifier to look for, e.g. 'batch job'."),
            "limit": _integer("Maximum number of excerpts to return.", default=5),
        },
        "required": ["query"],
    },
    handler=_search_history_handler,
    approval="never",
    summary="Search conversations",
)


async def _vcs_status_handler(ctx: ToolContext) -> ToolResult:
    """What version control says about this project, and what is installed here."""
    from surtitle.vcs import provision as vcs_provision
    from surtitle.vcs import repo as vcs_repo

    rows = vcs_provision.status()
    state = vcs_repo.detect(ctx.root)
    return ToolResult(
        ok=True,
        data={
            "tools": [row.to_dict() for row in rows],
            "repository": state.to_dict(),
            "summary": state.describe(),
        },
        display=state.describe(),
    )


async def _vcs_guide_handler(ctx: ToolContext, system: str = "") -> ToolResult:
    """The usage notes for the system in play, read on demand rather than always."""
    from surtitle.vcs import guide as vcs_guide_text
    from surtitle.vcs import repo as vcs_repo

    chosen = (system or "").strip()
    if not chosen:
        chosen = vcs_repo.detect(ctx.root).system
    text = vcs_guide_text.guide_for(chosen)
    return ToolResult(
        ok=True,
        data={"system": chosen or "both", "guide": text},
        display=f"git and svn usage notes ({len(text)} characters)",
    )


async def _vcs_commit_handler(
    ctx: ToolContext,
    message: str,
    detail: str = "summary",
    paths: list[str] | None = None,
    push: bool = False,
) -> ToolResult:
    """Commit the project, after the user has been asked.

    The level of detail is checked rather than merely recorded: a body attached to
    a message the user asked to be one line is the agent not having listened, and
    it is cheaper to say so here than to leave a verbose entry in their history.
    """
    from surtitle.vcs import commit as vcs_commit
    from surtitle.vcs import repo as vcs_repo

    level = (detail or "").strip().lower()
    if level not in DETAIL_LEVELS:
        return ToolResult(
            ok=False,
            error=(
                f"detail must be one of {', '.join(sorted(DETAIL_LEVELS))}. Ask the "
                "user which they want before committing."
            ),
        )
    text = (message or "").strip()
    if not text:
        return ToolResult(ok=False, error="A commit needs a message.")
    if level == "one-line" and "\n" in text:
        return ToolResult(
            ok=False,
            error=(
                "The user asked for a one-line message but this one has a body. Send "
                "the subject on its own, or ask whether they would rather have more."
            ),
        )

    state = vcs_repo.detect(ctx.root)
    if state.system == "none":
        return ToolResult(
            ok=False,
            error=(
                "This project is not under version control yet, so there is nothing "
                "to commit into. Whether to create a repository here is the user's "
                "decision — ask them."
            ),
        )

    result = await asyncio.to_thread(
        vcs_commit.commit,
        ctx.root,
        system=state.system,
        message=text,
        paths=list(paths) if paths else None,
        include_all=not paths,
        push=bool(push),
    )
    if not result.ok:
        return ToolResult(ok=False, error=result.error or "The commit failed.")
    trailing = f" (left out of the commit: {', '.join(result.excluded)})" if result.excluded else ""
    return ToolResult(
        ok=True,
        data={**result.to_dict(), "detail": level},
        display=(
            f"Committed {result.revision or 'the change'} with {state.system}"
            + (" and pushed" if result.pushed else "")
            + trailing
        ),
    )


_VCS_STATUS = Tool(
    name="vcs_status",
    description=(
        "Report the project's version-control state: whether it is a git or svn "
        "working copy, which branch or revision it is on, what is uncommitted, and "
        "which git and svn are installed on this machine. Read it before doing "
        "version-control work, and before telling the user there is or is not "
        "anything to commit — do not infer any of this from file names."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_vcs_status_handler,
    approval="never",
    summary="Show version-control state",
)


_VCS_GUIDE = Tool(
    name="vcs_guide",
    description=(
        "Read the correct usage notes for git or svn in this environment: the "
        "commands that matter, how to undo at each level of destruction, the rules "
        "about what is never committed, and the commit-message levels to offer the "
        "user. Read it once before your first version-control action in a "
        "conversation, rather than working from memory."
    ),
    parameters={
        "type": "object",
        "properties": {
            "system": _string("'git', 'svn', or 'both'. Leave empty for the one this project uses.")
        },
        "required": [],
    },
    handler=_vcs_guide_handler,
    approval="never",
    summary="Show how to use git and svn",
)


_VCS_COMMIT = Tool(
    name="vcs_commit",
    description=(
        "Stage and commit the project's changes with git or svn, optionally "
        "pushing. Call this only after the user has agreed to a commit and told you "
        "how detailed the message should be — ask first, in one short question, "
        "once the work is done. Put the subject on the first line of `message` "
        "(imperative, under about 72 characters) and the body, at the level they "
        "chose, after a blank line. Surtitle's own .surtitle state is never "
        "committed, and an empty change is refused rather than committed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": _string(
                "The commit message. First line is the subject; the rest is the body "
                "at the level the user chose."
            ),
            "detail": {
                "type": "string",
                "enum": sorted(DETAIL_LEVELS),
                "description": f"How much detail the user asked for: {detail_menu()}.",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Specific paths to stage. Omit to include everything that changed."
                ),
            },
            "push": _boolean(
                "Also publish the commit (git push). Subversion publishes on commit.",
                default=False,
            ),
        },
        "required": ["message", "detail"],
    },
    handler=_vcs_commit_handler,
    approval="ask",
    summary="Commit the project's changes",
    mutating=True,
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
        _VCS_STATUS,
        _VCS_GUIDE,
        _VCS_COMMIT,
    ]
