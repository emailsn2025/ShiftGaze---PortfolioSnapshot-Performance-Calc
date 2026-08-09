"""
report_export.py
-----------------
Builds the two "download everything" artifacts the app offers:
  - build_excel(): one .xlsx workbook, one sheet per table currently available
    (skips sheets for data that isn't loaded, e.g. XIRR tables when no
    transaction history has been imported yet).
  - build_pdf(): a single print-friendly PDF - summary metrics up top, then
    every table in its own section. Tables only (no charts) - kept
    dependency-light and reliable rather than pulling in a headless-browser
    or chart-rasterising toolchain for a report that's meant to be read as
    numbers, not visuals.

Both take the same shape of input: a dict of {sheet/section title: DataFrame},
so app.py decides what's included based on what's actually loaded, and this
module just formats whatever it's handed.
"""

from __future__ import annotations

import os
from io import BytesIO

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
)

# Bundled in the repo (assets/) rather than relying on system fonts, since a
# fresh Streamlit Cloud container won't have any - and reportlab's built-in
# fonts don't include the ₹ glyph at all (renders as a black box otherwise).
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_FONT_REGULAR = "DejaVuSans"
_FONT_BOLD = "DejaVuSans-Bold"
_fonts_ready = False
_rupee_capable = False  # whether the active font can actually render ₹


def _ensure_fonts():
    """
    Tries to register the bundled Unicode font. If the .ttf files aren't
    present (e.g. they got skipped during a manual file-by-file copy - .ttf
    is binary and can't be pasted as text, unlike every other file in this
    repo), falls back to reportlab's built-in Helvetica rather than crashing
    the whole PDF export. The only visible difference in that fallback is
    that ₹ becomes "Rs." in the PDF, since Helvetica has no ₹ glyph at all.
    """
    global _fonts_ready, _rupee_capable, _FONT_REGULAR, _FONT_BOLD
    if _fonts_ready:
        return
    try:
        pdfmetrics.registerFont(TTFont("DejaVuSans", os.path.join(_ASSETS_DIR, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", os.path.join(_ASSETS_DIR, "DejaVuSans-Bold.ttf")))
        _FONT_REGULAR, _FONT_BOLD = "DejaVuSans", "DejaVuSans-Bold"
        _rupee_capable = True
    except Exception:
        _FONT_REGULAR, _FONT_BOLD = "Helvetica", "Helvetica-Bold"
        _rupee_capable = False
    _fonts_ready = True


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------

def _is_rupee_col(colname: str) -> bool:
    return "₹" in str(colname)


def _indian_grouping(n: int) -> str:
    """3,12,57,114 style grouping (last 3 digits, then pairs) - no ₹ prefix,
    the column header already carries that."""
    neg = n < 0
    s = str(abs(int(n)))
    if len(s) <= 3:
        return f"-{s}" if neg else s
    last3, rest = s[-3:], s[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    out = ",".join(groups + [last3])
    return f"-{out}" if neg else out


# Excel custom number format that renders Indian grouping natively in the
# cell (so it still sorts/computes as a real number, just displays grouped).
_INDIAN_EXCEL_FORMAT = "[>=10000000]##\\,##\\,##\\,##0;[>=100000]##\\,##\\,##0;##,##0"


def build_excel(sections: dict[str, pd.DataFrame]) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sections.items():
            if df is None or df.empty:
                continue
            df = df.copy()
            # Round rupee-denominated columns to the nearest whole rupee -
            # paise-level precision isn't meaningful at portfolio scale.
            # Percentage and other numeric columns (units, XIRR %) keep
            # their original precision.
            rupee_col_idx = []
            for i, col in enumerate(df.columns):
                if _is_rupee_col(col) and pd.api.types.is_numeric_dtype(df[col]):
                    df[col] = df[col].round(0).astype("Int64")
                    rupee_col_idx.append(i)
            # Excel sheet names cap at 31 chars and can't contain some punctuation.
            safe_name = "".join(c for c in name if c not in r'[]:*?/\\')[:31]
            df.to_excel(writer, sheet_name=safe_name, index=False)
            ws = writer.sheets[safe_name]
            # Apply Indian-grouping display format to rupee columns - the
            # underlying cell value stays a real number (sortable, usable in
            # formulas), only how it's displayed changes.
            for i in rupee_col_idx:
                col_letter = ws.cell(row=1, column=i + 1).column_letter
                for row in range(2, len(df) + 2):
                    ws[f"{col_letter}{row}"].number_format = _INDIAN_EXCEL_FORMAT
            # Auto-width every column so grouped numbers and long headers
            # never truncate to "###" or get clipped.
            for i, col in enumerate(df.columns):
                col_letter = ws.cell(row=1, column=i + 1).column_letter
                longest = max(
                    [len(str(col))] + [len(_indian_grouping(v)) if i in rupee_col_idx and pd.notna(v) else len(str(v)) for v in df[col]]
                )
                ws.column_dimensions[col_letter].width = min(max(longest + 2, 10), 45)
    return buf.getvalue()


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def _sanitize(s: str) -> str:
    """Swaps ₹ for 'Rs.' when the active font can't render it (Helvetica
    fallback) - only touches text, never touches the Excel export."""
    if _rupee_capable:
        return s
    return s.replace("₹", "Rs.")


def _fmt_cell(v, is_rupee_col: bool = False) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    if isinstance(v, float) or isinstance(v, int):
        if is_rupee_col:
            return _indian_grouping(round(v))
        return f"{v:,.2f}" if isinstance(v, float) else str(v)
    return _sanitize(str(v))


def _df_to_table(df: pd.DataFrame, max_cols_before_landscape_needed: int = 6) -> Table:
    header = [_sanitize(str(c)) for c in df.columns]
    rupee_cols = [_is_rupee_col(c) for c in df.columns]
    rows = [[_fmt_cell(v, rupee_cols[i]) for i, v in enumerate(row)] for row in df.itertuples(index=False)]
    data = [header] + rows
    table = Table(data, repeatRows=1)
    style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E86AB")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
            ("FONTNAME", (0, 1), (-1, -1), _FONT_REGULAR),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FA")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )
    table.setStyle(style)
    return table


def build_pdf(
    title: str,
    generated_note: str,
    summary_metrics: dict[str, str],
    sections: list[tuple[str, pd.DataFrame]],
) -> bytes:
    """
    summary_metrics: label -> formatted value, shown as a small table up top.
    sections: ordered list of (heading, DataFrame) - one table per section,
    each starting on its own page so wide tables have room to breathe.
    """
    _ensure_fonts()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=1.2 * cm, rightMargin=1.2 * cm, topMargin=1.2 * cm, bottomMargin=1.2 * cm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleBig", parent=styles["Title"], fontName=_FONT_BOLD, fontSize=18, spaceAfter=4)
    note_style = ParagraphStyle("Note", parent=styles["Normal"], fontName=_FONT_REGULAR, textColor=colors.grey, fontSize=8)
    heading_style = ParagraphStyle("Heading", parent=styles["Heading2"], fontName=_FONT_BOLD, spaceBefore=6, spaceAfter=6)

    story = [Paragraph(_sanitize(title), title_style), Paragraph(_sanitize(generated_note), note_style), Spacer(1, 10)]

    if summary_metrics:
        metric_rows = [[_sanitize(str(k)), _sanitize(str(v))] for k, v in summary_metrics.items()]
        metric_table = Table(metric_rows, colWidths=[6 * cm, 6 * cm])
        metric_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (0, -1), _FONT_BOLD),
                    ("FONTNAME", (1, 0), (1, -1), _FONT_REGULAR),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(metric_table)
        story.append(Spacer(1, 16))

    if not _rupee_capable:
        story.append(Paragraph(
            "Note: the bundled Unicode font wasn't found in this deployment, so the rupee "
            "symbol is shown as 'Rs.' in this PDF. See the app's README for how to fix this.",
            note_style,
        ))
        story.append(Spacer(1, 10))

    first = True
    for heading, df in sections:
        if df is None or df.empty:
            continue
        if not first:
            story.append(PageBreak())
        first = False
        story.append(Paragraph(_sanitize(heading), heading_style))
        story.append(_df_to_table(df))

    doc.build(story)
    return buf.getvalue()
