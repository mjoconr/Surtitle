"""Filesystem tools, all confined to the project root.

Every function here returns a :class:`ToolResult` rather than raising for
ordinary problems (missing file, binary content, oversized read). Tool failures
are *information* the model should see and adapt to; only programmer errors
propagate. Each result also carries a short ``display`` string that the UI shows
in the transcript without involving the model.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from surtitle.tools.path_guard import (
    PathEscapeError,
    is_probably_binary,
    resolve_in_root,
)

__all__ = [
    "ARTIFACT_SUFFIXES",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_READ_LINES",
    "MAX_OUTPUT_CHARS",
    "ToolContext",
    "ToolResult",
    "edit_file",
    "list_dir",
    "read_file",
    "search_files",
    "write_file",
]

log = logging.getLogger(__name__)

DEFAULT_READ_LINES = 400
MAX_READ_LINES = 2000
DEFAULT_LIST_LIMIT = 200
MAX_SEARCH_RESULTS = 60
# Cap on any string handed back to the model, in characters. Roughly 4 chars per
# token, so this is about 6k tokens per tool result.
MAX_OUTPUT_CHARS = 24_000

# Directories that are never interesting and can be enormous.
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
    "dist",
    "build",
    ".uv-cache",
}

ARTIFACT_SUFFIXES = {".pdf", ".xlsx", ".xls", ".csv", ".docx", ".pptx", ".png", ".svg"}


@dataclass(slots=True)
class ToolContext:
    """Everything a tool needs to operate safely."""

    root: Path
    session_id: str = ""
    project_id: str = ""


@dataclass(slots=True)
class ToolResult:
    """Outcome of a tool call.

    ``data`` is returned to the model as JSON; ``display`` is shown to the user.
    ``artifacts`` lists files created or modified, so the UI can offer to open
    them without re-scanning the directory.
    """

    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    display: str = ""
    artifacts: list[str] | None = None
    truncated: bool = False

    def to_model_payload(self) -> str:
        """Serialise for the model, keeping the payload inside the budget."""
        if not self.ok:
            payload: dict[str, Any] = {"ok": False, "error": self.error or "unknown error"}
            if self.data:
                payload.update(self.data)
        else:
            payload = {"ok": True, **(self.data or {})}
        if self.truncated:
            payload["truncated"] = True

        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) <= MAX_OUTPUT_CHARS:
            return text

        # Trim any long string field until we fit, so the model still gets the
        # structure (paths, counts, line numbers) rather than a hard cut.
        payload["truncated"] = True
        longest = max(
            (k for k, v in payload.items() if isinstance(v, str)),
            key=lambda k: len(payload[k]),
            default=None,
        )
        if longest is not None:
            budget = max(0, len(payload[longest]) - (len(text) - MAX_OUTPUT_CHARS) - 200)
            payload[longest] = payload[longest][:budget] + "\n... [truncated]"
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > MAX_OUTPUT_CHARS:  # pragma: no cover - pathological case
            text = text[:MAX_OUTPUT_CHARS] + '"}'
        return text

    def to_dict(self) -> dict[str, Any]:
        """Wire form for the UI."""
        return {
            "ok": self.ok,
            "display": self.display,
            "error": self.error,
            "artifacts": self.artifacts or [],
            "truncated": self.truncated,
        }


def _resolve(ctx: ToolContext, path: str):
    """Resolve a path or return a ToolResult describing the refusal."""
    try:
        return resolve_in_root(ctx.root, path)
    except PathEscapeError as exc:
        return ToolResult(ok=False, error=str(exc), display=f"blocked: {path}")


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def list_dir(ctx: ToolContext, path: str = ".", limit: int = DEFAULT_LIST_LIMIT) -> ToolResult:
    """List a directory, marking subdirectories and skipping noise."""
    target = _resolve(ctx, path)
    if isinstance(target, ToolResult):
        return target

    if not target.absolute.exists():
        return ToolResult(ok=False, error=f"Directory not found: {target.relative or '.'}")
    if not target.absolute.is_dir():
        return ToolResult(ok=False, error=f"Not a directory: {target.relative}")

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(target.absolute.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return ToolResult(ok=False, error=f"Cannot list {target.relative}: {exc}")

    skipped = 0
    for child in children:
        if child.name in _SKIP_DIRS:
            skipped += 1
            continue
        if len(entries) >= max(1, min(limit, 2000)):
            skipped += 1
            continue
        try:
            is_dir = child.is_dir()
            stat = child.stat()
            size = 0 if is_dir else stat.st_size
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "type": "dir" if is_dir else "file",
                "size": size,
                **({"artifact": True} if child.suffix.lower() in ARTIFACT_SUFFIXES else {}),
            }
        )

    display = f"Listed {len(entries)} item(s) in {target.relative or '.'}"
    if skipped:
        display += f" ({skipped} hidden or skipped)"
    return ToolResult(
        ok=True,
        data={
            "path": target.relative or ".",
            "entries": entries,
            "entry_count": len(entries),
            "skipped": skipped,
        },
        display=display,
    )


def read_file(
    ctx: ToolContext,
    path: str,
    *,
    start_line: int = 1,
    max_lines: int = DEFAULT_READ_LINES,
) -> ToolResult:
    """Read a text file, a PDF's extracted text, or a CSV as text.

    Large files are returned in windows. The model is told the total line count
    and the range it received so it can ask for more instead of guessing.
    """
    target = _resolve(ctx, path)
    if isinstance(target, ToolResult):
        return target

    file_path = target.absolute
    if not file_path.exists():
        return ToolResult(ok=False, error=f"File not found: {target.relative}")
    if file_path.is_dir():
        return ToolResult(
            ok=False,
            error=f"{target.relative} is a directory. Use list_dir instead.",
        )

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return _read_pdf(target.relative, file_path)

    size = file_path.stat().st_size
    if size == 0:
        return ToolResult(
            ok=True,
            data={"path": target.relative, "content": "", "total_lines": 0},
            display=f"{target.relative} is empty",
        )

    if is_probably_binary(file_path):
        return ToolResult(
            ok=False,
            error=(
                f"{target.relative} appears to be a binary file ({_human_size(size)}). "
                "Only text files and PDFs can be read directly."
            ),
        )

    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult(ok=False, error=f"Cannot read {target.relative}: {exc}")

    lines = text.splitlines()
    total = len(lines)
    first = max(1, start_line)
    window = max(1, min(max_lines, MAX_READ_LINES))
    if first > total:
        return ToolResult(
            ok=False,
            error=f"start_line {first} is past the end of the file ({total} lines).",
        )
    last = min(total, first + window - 1)
    chunk = lines[first - 1 : last]

    truncated = last < total
    # Numbered so the model can refer to lines precisely and so a following
    # edit_file call can quote exact text.
    numbered = "\n".join(f"{first + i:>5}  {line}" for i, line in enumerate(chunk))
    display = f"Read {len(chunk)} line(s) from {target.relative}"
    if truncated:
        display += f" (lines {first}-{last} of {total})"

    return ToolResult(
        ok=True,
        data={
            "path": target.relative,
            "start_line": first,
            "end_line": last,
            "total_lines": total,
            "lines_returned": len(chunk),
            # Numbered so the model can refer to lines precisely and so that a
            # following edit_file call can quote exact text.
            "content": numbered,
            "has_more": truncated,
            "next_start_line": last + 1 if truncated else None,
        },
        display=display,
        truncated=truncated,
    )


def _read_pdf(relative: str, file_path: Path) -> ToolResult:
    """Extract text from a PDF using pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is a hard dependency
        return ToolResult(
            ok=False,
            error="PDF support is not installed. Run `uv sync` to add pypdf.",
        )

    try:
        reader = PdfReader(str(file_path))
        pages = len(reader.pages)
    except Exception as exc:
        return ToolResult(
            ok=False,
            error=f"Could not open {relative} as a PDF: {type(exc).__name__}: {exc}",
        )

    if pages == 0:
        return ToolResult(ok=False, error=f"{relative} has no pages.")

    chunks: list[str] = []
    budget = MAX_OUTPUT_CHARS
    pages_read = 0
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            text = f"[page {index + 1} could not be extracted: {type(exc).__name__}]"
        if text.strip():
            block = f"--- page {index + 1} ---\n{text.strip()}"
            if len(block) > budget:
                block = block[:budget] + "\n... [truncated]"
            chunks.append(block)
            budget -= len(block)
        pages_read = index + 1
        if budget <= 0:
            break

    if not chunks:
        return ToolResult(
            ok=False,
            error=(
                f"{relative} has {pages} page(s) but no extractable text. "
                "It is probably a scan and would need OCR."
            ),
        )

    combined = "\n\n".join(chunks)
    truncated = pages_read < pages or budget <= 0
    display = f"Read {pages_read} page(s) of {relative}"
    if truncated:
        display += f" of {pages}"
    return ToolResult(
        ok=True,
        data={
            "path": relative,
            "kind": "pdf",
            "pages": pages,
            "pages_returned": pages_read,
            "content": combined,
            "has_more": truncated,
        },
        display=display,
        truncated=truncated,
    )


def search_files(
    ctx: ToolContext,
    pattern: str,
    *,
    path: str = ".",
    glob: str | None = None,
    max_results: int = MAX_SEARCH_RESULTS,
    case_sensitive: bool = False,
) -> ToolResult:
    """Search file contents for a regular expression.

    Uses ripgrep when available because it respects ignore files and is far
    faster; otherwise falls back to a pure-Python walk so the tool always works
    on a machine with no extra binaries.
    """
    target = _resolve(ctx, path)
    if isinstance(target, ToolResult):
        return target
    if not target.absolute.exists():
        return ToolResult(ok=False, error=f"Path not found: {target.relative or '.'}")

    limit = max(1, min(max_results, MAX_SEARCH_RESULTS))

    if shutil.which("rg"):
        result = _search_with_ripgrep(
            target.absolute, target.relative, pattern, glob, limit, case_sensitive
        )
        if result is not None:
            return result

    return _search_python(target.absolute, ctx.root, pattern, glob, limit, case_sensitive)


def _search_with_ripgrep(
    search_root: Path,
    relative_prefix: str,
    pattern: str,
    glob: str | None,
    limit: int,
    case_sensitive: bool,
) -> ToolResult | None:
    """Run ripgrep. Returns ``None`` to fall back when rg is unusable."""
    command = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color=never",
        "--with-filename",
        "--max-count",
        str(limit),
    ]
    if not case_sensitive:
        command.append("--ignore-case")
    if glob:
        command += ["--glob", glob]
    for skip in _SKIP_DIRS:
        command += ["--glob", f"!**/{skip}/**"]
    command += ["--regexp", pattern, "."]

    try:
        completed = subprocess.run(
            command,
            cwd=str(search_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    # 1 == no matches, 2 == a real error such as a bad regex.
    if completed.returncode == 2:
        message = (completed.stderr or "").strip().splitlines()
        return ToolResult(
            ok=False,
            error=f"Search failed: {message[-1] if message else 'invalid pattern'}",
        )
    if completed.returncode == 1:
        return ToolResult(
            ok=True,
            data={"pattern": pattern, "matches": [], "match_count": 0},
            display=f"No matches for {pattern!r}",
        )

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    matches = _parse_rg_lines(lines, limit)
    # ripgrep reports paths relative to the directory it was given, but the
    # model should always see paths relative to the project root, so re-anchor
    # them here. Otherwise a scoped search appears to return files at the top
    # level and the model will read the wrong path.
    if relative_prefix:
        prefix = f"{relative_prefix.rstrip('/')}/"
        for match in matches:
            name = match["file"]
            if not name.startswith(prefix):
                match["file"] = f"{prefix}{name.lstrip('./')}"
    return ToolResult(
        ok=True,
        data={
            "pattern": pattern,
            "matches": matches,
            "match_count": len(matches),
            "has_more": len(lines) > len(matches),
        },
        display=f"Found {len(matches)} match(es) for {pattern!r}",
        truncated=len(lines) > len(matches),
    )


def _parse_rg_lines(lines: list[str], limit: int) -> list[dict[str, Any]]:
    """Parse ``path:line:content`` output, tolerating colons in paths."""
    matches: list[dict[str, Any]] = []
    for raw in lines:
        if len(matches) >= limit:
            break
        parts = raw.split(":", 2)
        if len(parts) < 3:
            continue
        file_name, line_no, content = parts
        if not line_no.isdigit():
            continue
        matches.append({"file": file_name, "line": int(line_no), "text": content.strip()[:300]})
    return matches


def _search_python(
    search_root: Path,
    project_root: Path,
    pattern: str,
    glob: str | None,
    limit: int,
    case_sensitive: bool,
) -> ToolResult:
    """Pure-Python fallback search, used when ripgrep is not installed."""
    import fnmatch

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return ToolResult(ok=False, error=f"Invalid regular expression: {exc}")

    matches: list[dict[str, Any]] = []
    scanned = 0
    capped = False

    for file_path in sorted(search_root.rglob("*")):
        if len(matches) >= limit:
            capped = True
            break
        if not file_path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in file_path.parts):
            continue
        if glob and not fnmatch.fnmatch(file_path.name, glob):
            continue
        suffix = file_path.suffix.lower()
        if suffix in {".pdf", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".zip", ".gz"}:
            continue
        if file_path.stat().st_size > 2_000_000:
            continue

        scanned += 1
        if scanned > 4000:
            capped = True
            break

        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    if regex.search(line):
                        relative = file_path.relative_to(project_root).as_posix()
                        matches.append(
                            {"file": relative, "line": line_no, "text": line.strip()[:300]}
                        )
                        if len(matches) >= limit:
                            break
        except OSError:
            continue

    return ToolResult(
        ok=True,
        data={
            "pattern": pattern,
            "matches": matches,
            "match_count": len(matches),
            "has_more": capped,
            "scanner": "python",
        },
        display=f"Found {len(matches)} match(es) for {pattern!r}",
        truncated=capped,
    )


def write_file(
    ctx: ToolContext,
    path: str,
    content: str,
    *,
    overwrite: bool = True,
) -> ToolResult:
    """Create or replace a file, creating parent directories as needed."""
    target = _resolve(ctx, path)
    if isinstance(target, ToolResult):
        return target

    file_path = target.absolute
    if file_path.is_dir():
        return ToolResult(ok=False, error=f"{target.relative} is a directory.")
    if not target.relative:
        return ToolResult(ok=False, error="A file path is required.")

    existed = file_path.exists()
    if existed and not overwrite:
        return ToolResult(
            ok=False,
            error=f"{target.relative} already exists. Pass overwrite=true to replace it.",
        )

    previous: str | None = None
    if existed:
        try:
            previous = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            previous = None

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        return ToolResult(ok=False, error=f"Cannot write {target.relative}: {exc}")

    lines = content.count("\n") + (1 if content else 0)
    if existed and previous is not None:
        delta = lines - (previous.count("\n") + (1 if previous else 0))
        display = (
            f"Updated {target.relative} ({delta:+d} lines)"
            if delta
            else (f"Updated {target.relative}")
        )
        action = "updated"
    else:
        display = f"Created {target.relative} ({lines} lines)"
        action = "created"

    return ToolResult(
        ok=True,
        data={
            "path": target.relative,
            "action": action,
            "bytes": len(content.encode("utf-8")),
            "lines": lines,
            "previous_bytes": len(previous.encode("utf-8")) if previous else 0,
        },
        display=display,
        artifacts=[target.relative] if file_path.suffix.lower() in ARTIFACT_SUFFIXES else [],
    )


def edit_file(
    ctx: ToolContext,
    path: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
) -> ToolResult:
    """Replace an exact string in a file.

    ``old_string`` must occur exactly once unless ``replace_all`` is set. This is
    what stops an ambiguous edit from silently corrupting the wrong part of a
    file — the failure mode that makes naive search-and-replace tools dangerous.
    """
    target = _resolve(ctx, path)
    if isinstance(target, ToolResult):
        return target

    file_path = target.absolute
    if not file_path.exists():
        return ToolResult(ok=False, error=f"File not found: {target.relative}")
    if file_path.is_dir():
        return ToolResult(ok=False, error=f"{target.relative} is a directory.")
    if not old_string:
        return ToolResult(
            ok=False,
            error="old_string must not be empty. Use write_file to replace a whole file.",
        )
    if old_string == new_string:
        return ToolResult(ok=False, error="old_string and new_string are identical.")

    try:
        original = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ToolResult(ok=False, error=f"Cannot read {target.relative}: {exc}")

    occurrences = original.count(old_string)
    if occurrences == 0:
        return ToolResult(
            ok=False,
            error=(
                f"old_string was not found in {target.relative}. "
                "Read the file first and copy the exact text, including indentation."
            ),
        )
    if occurrences > 1 and not replace_all:
        return ToolResult(
            ok=False,
            error=(
                f"old_string appears {occurrences} times in {target.relative}, so the edit is "
                "ambiguous. Include more surrounding context to make it unique, or pass "
                "replace_all=true."
            ),
        )

    updated = (
        original.replace(old_string, new_string)
        if replace_all
        else original.replace(old_string, new_string, 1)
    )
    try:
        file_path.write_text(updated, encoding="utf-8", newline="\n")
    except OSError as exc:
        return ToolResult(ok=False, error=f"Cannot write {target.relative}: {exc}")

    replaced = occurrences if replace_all else 1
    return ToolResult(
        ok=True,
        data={
            "path": target.relative,
            "replacements": replaced,
            "bytes_before": len(original.encode("utf-8")),
            "bytes_after": len(updated.encode("utf-8")),
        },
        display=f"Edited {target.relative} ({replaced} replacement(s))",
        artifacts=[target.relative] if file_path.suffix.lower() in ARTIFACT_SUFFIXES else [],
    )
