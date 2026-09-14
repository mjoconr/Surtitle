"""Artifact generation: PDFs, spreadsheets and charts.

These exist so the common cases are one tool call with a typed schema, rather
than the model hand-rolling reportlab boilerplate on every request (which is
slow, token-expensive and easy to get subtly wrong). ``run_python`` remains the
escape hatch for anything these do not cover.

Nothing here is allowed to exceed the project root: paths go through the same
guard as the filesystem tools.
"""

from __future__ import annotations

import logging
import tempfile
import textwrap
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from surtitle.tools.fs_tools import ToolContext, ToolResult
from surtitle.tools.path_guard import PathEscapeError, resolve_in_root

__all__ = ["MAX_SPREADSHEET_ROWS", "make_chart", "make_pdf", "make_spreadsheet"]

log = logging.getLogger(__name__)

MAX_SPREADSHEET_ROWS = 20_000
MAX_PDF_TABLE_ROWS = 2000

# ReportLab's built-in fonts are Latin-1 only. Rather than crash on a smart
# quote or an em dash, substitute characters that would otherwise appear as
# black boxes in the generated PDF.
_LATIN1_FIXES = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2026": "...",
    "\u2022": "*",
    "\u00a0": " ",
    "\u2192": "->",
    "\u2264": "<=",
    "\u2265": ">=",
}


def _sanitise(text: Any) -> str:
    """Coerce a value to a Latin-1-safe string for PDF output."""
    cleaned = str(text)
    for source, replacement in _LATIN1_FIXES.items():
        cleaned = cleaned.replace(source, replacement)
    return cleaned.encode("latin-1", errors="replace").decode("latin-1")


def _resolve_output(
    ctx: ToolContext, path: str, expected_suffix: str
) -> tuple[Path, str] | ToolResult:
    """Resolve an output path and normalise its extension."""
    if not path.lower().endswith(expected_suffix):
        path = f"{path}{expected_suffix}"
    try:
        target = resolve_in_root(ctx.root, path)
    except PathEscapeError as exc:
        return ToolResult(ok=False, error=str(exc), display=f"blocked: {path}")
    if not target.relative:
        return ToolResult(ok=False, error="An output file path is required.")
    return target.absolute, target.relative


@contextmanager
def _temp_png():
    """Yield a temporary PNG path that is always cleaned up."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def make_pdf(
    ctx: ToolContext,
    path: str,
    *,
    title: str,
    blocks: list[dict[str, Any]],
    subtitle: str | None = None,
    author: str | None = None,
    page_size: str = "LETTER",
) -> ToolResult:
    """Build a formatted PDF from structured blocks.

    Supported block types: ``heading``, ``paragraph``, ``bullet_list``,
    ``table`` (with ``headers`` and ``rows``), ``page_break`` and ``spacer``.
    """
    resolved = _resolve_output(ctx, path, ".pdf")
    if isinstance(resolved, ToolResult):
        return resolved
    output_path, relative = resolved

    if not title.strip():
        return ToolResult(ok=False, error="title must not be empty.")
    if not isinstance(blocks, list) or not blocks:
        return ToolResult(ok=False, error="blocks must be a non-empty list.")

    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4, LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Image,
            ListFlowable,
            ListItem,
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:  # pragma: no cover - reportlab is a hard dependency
        return ToolResult(ok=False, error=f"PDF generation is unavailable: {exc}")

    pagesize = A4 if str(page_size).upper() == "A4" else LETTER
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontSize=10,
        leading=14,
        alignment=TA_LEFT,
        spaceAfter=8,
    )
    heading_styles = {
        level: ParagraphStyle(
            f"Heading{level}",
            parent=styles[f"Heading{level}"],
            textColor=colors.HexColor("#111827"),
            spaceBefore=12 if level > 1 else 0,
            spaceAfter=6,
        )
        for level in (1, 2, 3, 4)
    }
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Title"],
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#111827"),
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#4b5563"),
        spaceAfter=14,
    )

    story: list[Any] = [Paragraph(_sanitise(title), title_style)]
    if subtitle:
        story.append(Paragraph(_sanitise(subtitle), subtitle_style))
    story.append(Spacer(1, 6))

    skipped: list[str] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            skipped.append(f"block {index} is not an object")
            continue
        block_type = str(block.get("type", "")).strip().lower()

        if block_type == "heading":
            text = _sanitise(block.get("text", ""))
            if not text:
                continue
            try:
                level = int(block.get("level", 2))
            except (TypeError, ValueError):
                level = 2
            story.append(Paragraph(text, heading_styles.get(level, heading_styles[2])))

        elif block_type in {"paragraph", "text"}:
            text = _sanitise(block.get("text", ""))
            if text:
                story.append(Paragraph(text, body_style))

        elif block_type == "bullet_list":
            items = block.get("items") or []
            if not isinstance(items, list):
                skipped.append(f"block {index}: items must be a list")
                continue
            flowables = [
                ListItem(Paragraph(_sanitise(item), body_style), leftIndent=12)
                for item in items
                if str(item).strip()
            ]
            if flowables:
                story.append(ListFlowable(flowables, bulletType="bullet", start="•", leftIndent=14))
                story.append(Spacer(1, 6))

        elif block_type == "table":
            headers = block.get("headers")
            rows = block.get("rows")
            if not isinstance(rows, list) or not rows:
                skipped.append(f"block {index}: table needs a non-empty 'rows' list")
                continue
            if len(rows) > MAX_PDF_TABLE_ROWS:
                skipped.append(f"block {index}: table truncated to {MAX_PDF_TABLE_ROWS} rows")
                rows = rows[:MAX_PDF_TABLE_ROWS]

            normalised: list[list[Any]] = []
            if isinstance(headers, list) and headers:
                normalised.append([_sanitise(h) for h in headers])
            for row in rows:
                if isinstance(row, (list, tuple)):
                    normalised.append([_sanitise(cell) for cell in row])
                else:
                    normalised.append([_sanitise(row)])

            column_count = max(len(r) for r in normalised)
            for row in normalised:
                row.extend([""] * (column_count - len(row)))

            available = pagesize[0] - 1.4 * inch
            # Keep narrow tables readable instead of stretching them to the page.
            width = min(available, max(2.2 * inch, column_count * 1.5 * inch))
            table = Table(normalised, colWidths=[width / column_count] * column_count)
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("LEFTPADDING", (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        (
                            "ROWBACKGROUNDS",
                            (0, 1),
                            (-1, -1),
                            [colors.white, colors.HexColor("#f9fafb")],
                        ),
                    ]
                    + (
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ]
                        if isinstance(headers, list) and headers
                        else []
                    )
                )
            )
            story.append(table)
            story.append(Spacer(1, 10))

        elif block_type == "image":
            source = block.get("path")
            if not source:
                skipped.append(f"block {index}: image needs a 'path'")
                continue
            try:
                image_target = resolve_in_root(ctx.root, str(source))
            except PathEscapeError as exc:
                skipped.append(f"block {index}: {exc}")
                continue
            if not image_target.absolute.exists():
                skipped.append(f"block {index}: image not found ({image_target.relative})")
                continue
            try:
                from reportlab.lib.utils import ImageReader

                reader = ImageReader(str(image_target.absolute))
                pixel_width, pixel_height = reader.getSize()
                max_width = pagesize[0] - 1.4 * inch
                scale = min(1.0, max_width / pixel_width)
                story.append(
                    Image(
                        str(image_target.absolute),
                        width=pixel_width * scale,
                        height=pixel_height * scale,
                    )
                )
                caption = block.get("caption")
                if caption:
                    story.append(Spacer(1, 4))
                    story.append(Paragraph(_sanitise(caption), subtitle_style))
                story.append(Spacer(1, 10))
            except Exception as exc:
                skipped.append(f"block {index}: image failed ({type(exc).__name__})")

        elif block_type == "page_break":
            story.append(PageBreak())

        elif block_type == "spacer":
            story.append(Spacer(1, float(block.get("height", 12) or 12)))

        else:
            skipped.append(f"block {index}: unknown type {block_type!r}")

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        document = SimpleDocTemplate(
            str(output_path),
            pagesize=pagesize,
            title=_sanitise(title),
            author=_sanitise(author) if author else None,
            leftMargin=0.7 * inch,
            rightMargin=0.7 * inch,
            topMargin=0.8 * inch,
            bottomMargin=0.8 * inch,
        )
        document.build(story)
    except Exception as exc:
        return ToolResult(ok=False, error=f"Could not build the PDF: {type(exc).__name__}: {exc}")

    size = output_path.stat().st_size
    if size == 0:
        return ToolResult(ok=False, error="The PDF was created but is empty.")

    return ToolResult(
        ok=True,
        data={
            "path": relative,
            "kind": "pdf",
            "blocks_written": len(story),
            "bytes": size,
            "warnings": skipped or None,
        },
        display=f"Created {relative}",
        artifacts=[relative],
    )


# --------------------------------------------------------------------------- #
# Spreadsheet
# --------------------------------------------------------------------------- #
def make_spreadsheet(
    ctx: ToolContext,
    path: str,
    *,
    sheets: list[dict[str, Any]],
    header_bold: bool = True,
    auto_width: bool = True,
) -> ToolResult:
    """Build an .xlsx workbook from one or more sheets of tabular data."""
    resolved = _resolve_output(ctx, path, ".xlsx")
    if isinstance(resolved, ToolResult):
        return resolved
    output_path, relative = resolved

    if not isinstance(sheets, list) or not sheets:
        return ToolResult(ok=False, error="sheets must be a non-empty list.")

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover - openpyxl is a hard dependency
        return ToolResult(ok=False, error=f"Spreadsheet generation is unavailable: {exc}")

    workbook = Workbook()
    workbook.remove(workbook.active)  # remove the default blank sheet

    total_rows = 0
    written_sheets: list[str] = []
    skipped: list[str] = []

    for index, sheet in enumerate(sheets):
        if not isinstance(sheet, dict):
            skipped.append(f"sheet {index} is not an object")
            continue

        name = str(sheet.get("name") or f"Sheet{index + 1}")[:31]
        rows = sheet.get("rows")
        if not isinstance(rows, list):
            skipped.append(f"sheet {name!r}: 'rows' must be a list")
            continue
        if len(rows) > MAX_SPREADSHEET_ROWS:
            skipped.append(f"sheet {name!r}: truncated to {MAX_SPREADSHEET_ROWS} rows")
            rows = rows[:MAX_SPREADSHEET_ROWS]

        # Excel rejects these characters in a sheet name.
        for bad in "[]:*?/\\":
            name = name.replace(bad, "-")
        if not name:
            name = f"Sheet{index + 1}"

        worksheet = workbook.create_sheet(title=name)
        headers = sheet.get("headers")
        header_row: list[Any] = []
        if isinstance(headers, list) and headers:
            header_row = [headers]
            total_rows += 1

        body_rows: list[list[Any]] = []
        for row in rows:
            if isinstance(row, (list, tuple)):
                body_rows.append(list(row))
            else:
                body_rows.append([row])
        total_rows += len(body_rows)

        for row_index, row in enumerate(header_row + body_rows, start=1):
            for column_index, value in enumerate(row, start=1):
                cell = worksheet.cell(row=row_index, column=column_index)
                # Convert unserialisable values rather than letting openpyxl
                # raise deep inside the save call.
                cell.value = (
                    value if isinstance(value, (int, float, str, bool, type(None))) else str(value)
                )
                if row_index == 1 and header_row and header_bold:
                    cell.font = Font(bold=True, color="FFFFFFFF")
                    cell.fill = PatternFill("solid", start_color="FF374151")
                    cell.alignment = Alignment(vertical="center")
                else:
                    cell.alignment = Alignment(vertical="top", wrap_text=False)

        if sheet.get("freeze_header") is not False and header_row:
            worksheet.freeze_panes = "A2"

        if auto_width:
            widest: dict[int, int] = {}
            for row in (header_row + body_rows)[:500]:
                for column_index, value in enumerate(row, start=1):
                    text = "" if value is None else str(value)
                    widest[column_index] = max(widest.get(column_index, 0), len(text))
            for column_index, width in widest.items():
                worksheet.column_dimensions[get_column_letter(column_index)].width = min(
                    60, max(9, width + 2)
                )

        written_sheets.append(name)

    if not written_sheets:
        return ToolResult(
            ok=False,
            error=f"No sheets could be written. {skipped or 'Check the sheet definitions.'}",
        )

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(str(output_path))
    except Exception as exc:
        return ToolResult(
            ok=False, error=f"Could not save the spreadsheet: {type(exc).__name__}: {exc}"
        )

    size = output_path.stat().st_size
    return ToolResult(
        ok=True,
        data={
            "path": relative,
            "kind": "xlsx",
            "sheets": written_sheets,
            "rows": total_rows,
            "bytes": size,
            "warnings": skipped or None,
        },
        display=f"Created {relative} ({len(written_sheets)} sheet(s), {total_rows} rows)",
        artifacts=[relative],
    )


# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #
def make_chart(
    ctx: ToolContext,
    path: str,
    *,
    chart_type: str,
    series: list[dict[str, Any]],
    title: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
    width: float = 10.0,
    height: float = 6.0,
    dpi: int = 150,
) -> ToolResult:
    """Render a chart to PNG or SVG for embedding in a PDF or viewing directly."""
    suffix = ".svg" if path.lower().endswith(".svg") else ".png"
    resolved = _resolve_output(ctx, path, suffix)
    if isinstance(resolved, ToolResult):
        return resolved
    output_path, relative = resolved

    kind = str(chart_type).strip().lower()
    supported = {"line", "bar", "hbar", "pie", "scatter", "area", "hist"}
    if kind not in supported:
        return ToolResult(
            ok=False,
            error=f"chart_type must be one of: {', '.join(sorted(supported))}",
        )
    if not isinstance(series, list) or not series:
        return ToolResult(ok=False, error="series must be a non-empty list.")

    try:
        import matplotlib

        matplotlib.use("Agg")  # headless: no display needed on a server
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover - matplotlib is a hard dependency
        return ToolResult(
            ok=False,
            error=(
                "Charting requires matplotlib, which is not installed. "
                "Run `uv sync` to add it, or build the file with run_python."
            ),
        )

    figure, axes = plt.subplots(
        figsize=(max(3.0, width), max(2.0, height)), dpi=max(50, min(dpi, 400))
    )
    try:
        for entry in series:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("name") or entry.get("label") or "")
            x_values = entry.get("x")
            y_values = entry.get("y") or entry.get("values")

            if kind == "pie":
                values = y_values if y_values is not None else entry.get("values")
                labels = x_values or [str(i) for i in range(len(values or []))]
                if values:
                    axes.pie(
                        [float(v) for v in values],
                        labels=[str(v) for v in labels],
                        autopct="%1.1f%%",
                    )
                continue

            if y_values is None:
                continue
            y_float = [float(v) for v in y_values]
            if x_values is not None and len(x_values) == len(y_float):
                if all(isinstance(v, str) for v in x_values):
                    x_axis: Any = list(x_values)
                else:
                    x_axis = [float(v) for v in x_values]
            else:
                x_axis = list(range(len(y_float)))

            if kind == "line":
                axes.plot(x_axis, y_float, marker="o", label=label or None)
            elif kind == "bar":
                axes.bar(x_axis, y_float, label=label or None)
            elif kind == "hbar":
                axes.barh(x_axis, y_float, label=label or None)
            elif kind == "scatter":
                axes.scatter(x_axis, y_float, label=label or None)
            elif kind == "area":
                axes.fill_between(range(len(y_float)), y_float, alpha=0.4, label=label or None)
                axes.plot(range(len(y_float)), y_float, label=None)
            elif kind == "hist":
                axes.hist(y_float, bins=min(30, max(5, len(y_float) // 2)), label=label or None)

        if title:
            axes.set_title(str(title))
        if x_label and kind not in {"pie", "hbar"}:
            axes.set_xlabel(str(x_label))
        if y_label and kind not in {"pie", "hbar"}:
            axes.set_ylabel(str(y_label))
        if kind in {"bar", "hbar"}:
            # Avoid overlapping category labels on a crowded axis.
            for tick in axes.get_xticklabels() if kind == "bar" else axes.get_yticklabels():
                tick.set_rotation(45 if kind == "bar" else 0)
        if kind != "pie" and any(entry.get("name") for entry in series if isinstance(entry, dict)):
            axes.legend()
        if kind != "pie":
            axes.grid(True, alpha=0.25, linestyle="--")
        figure.tight_layout()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(str(output_path), bbox_inches="tight")
    except (TypeError, ValueError) as exc:
        return ToolResult(ok=False, error=f"Could not build the chart: {exc}")
    except OSError as exc:
        return ToolResult(ok=False, error=f"Could not write {relative}: {exc}")
    finally:
        plt.close(figure)

    size = output_path.stat().st_size
    return ToolResult(
        ok=True,
        data={
            "path": relative,
            "kind": suffix.lstrip("."),
            "chart_type": kind,
            "series_count": len(series),
            "bytes": size,
        },
        display=f"Created chart {relative}",
        artifacts=[relative],
    )


def wrap_for_display(text: str, width: int = 78) -> str:
    """Small helper used in prompts and CLI output."""
    return textwrap.fill(text, width=width)
