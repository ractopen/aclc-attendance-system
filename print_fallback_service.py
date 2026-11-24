"""
Printable PDF Fallback & Report Suite for ACLC College Seminar System.

Generates four clean, print-ready A4 PDFs using ReportLab:
1. Blank Attendance Sheet (Landscape A4) with pre-numbered Ref # (001, 002, ...)
   and Student ID / USN column for effortless cross-referencing.
2. Blank Evaluation Form (Portrait A4) dynamically rendered from the active quiz
   schema, featuring a prominent "Student ID or Attendance Ref #" box at the top
   so attendees do not need to rewrite their name, section, or email.
3. Filled Attendance Master Report (Landscape A4) with summary KPIs.
4. Filled Evaluation Answers & Tally Summary (Portrait A4) with criteria averages
   and per-attendee responses.
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import datetime
from typing import Dict, Any, List, Optional

import qrcode
from jinja2 import Environment, FileSystemLoader
from reportlab.graphics.shapes import Drawing, Polygon, Rect
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
    Image,
)

from database import (
    get_all_settings,
    get_all_attendees,
    get_statistics,
    get_eval_quiz_schema,
    get_all_evaluations_for_export,
    format_time_str,
)

# Pure Black & White / Ink-Saver palette (zero solid dark/colored fills)
ACLC_BLUE = colors.black
ACLC_DARK = colors.black
HIV_CRIMSON = colors.black
ACLC_GOLD = colors.black
LIGHT_BG = colors.white
ALT_ROW_BG = colors.white
BORDER_GRAY = HexColor("#475569")
TEXT_DARK = colors.black
TEXT_MUTED = HexColor("#334155")


def _winansi_safe(text: Any) -> str:
    """Ensures text is safely representable in ReportLab's built-in WinAnsi fonts."""
    if text is None:
        return ""
    s = str(text)
    if not s:
        return ""
    try:
        s.encode("cp1252")
        return s
    except UnicodeEncodeError:
        normalized = unicodedata.normalize("NFKD", s)
        return normalized.encode("cp1252", errors="replace").decode("cp1252")


def _xml_safe(text: Any) -> str:
    """Escapes XML/HTML characters for ReportLab Paragraph markup."""
    s = _winansi_safe(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _draw_footer(canvas_obj, doc, label_left: str):
    """Draws a subtle, professional footer with page numbering on every page."""
    canvas_obj.saveState()
    page_w, _ = doc.pagesize
    canvas_obj.setStrokeColor(BORDER_GRAY)
    canvas_obj.setLineWidth(0.5)
    canvas_obj.line(doc.leftMargin, 11 * mm, page_w - doc.rightMargin, 11 * mm)
    canvas_obj.setFont("Helvetica", 7.5)
    canvas_obj.setFillColor(TEXT_MUTED)
    canvas_obj.drawString(doc.leftMargin, 7.5 * mm, _winansi_safe(label_left))
    canvas_obj.drawRightString(page_w - doc.rightMargin, 7.5 * mm, f"Page {doc.page}")
    canvas_obj.restoreState()


def generate_blank_attendance_pdf(
    pages: int = 5,
    rows_per_page: int = 20,
    session_label: str = "",
) -> bytes:
    """
    Generates a clean, print-ready A4 Landscape Attendance Sign-In Sheet.
    Each row has a pre-printed Ref # (001, 002, ...) next to Student ID / USN
    so attendees can write the exact same ID/Ref # on their Evaluation Form.
    """
    pages = max(1, min(30, int(pages or 5)))
    rows_per_page = max(10, min(25, int(rows_per_page or 20)))

    settings = get_all_settings()
    event_title = settings.get("event_title", "HIV Awareness Seminar")
    event_date = settings.get("event_date", datetime.now().strftime("%B %d, %Y"))
    venue = settings.get("venue", "ACLC College of Mandaue")

    buf = io.BytesIO()
    page_w, page_h = landscape(A4)
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=14 * mm,
        title=f"{event_title} - Printable Attendance Sheet",
    )
    usable_w = page_w - doc.leftMargin - doc.rightMargin

    styles = getSampleStyleSheet()
    st_school = ParagraphStyle(
        "School",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        textColor=HIV_CRIMSON,
        leading=11,
    )
    st_title = ParagraphStyle(
        "Title",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=14,
        textColor=ACLC_BLUE,
        leading=17,
    )
    st_meta = ParagraphStyle(
        "Meta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        textColor=TEXT_DARK,
        leading=11,
    )
    st_inst = ParagraphStyle(
        "Inst",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        textColor=ACLC_DARK,
        leading=11,
    )
    st_cell = ParagraphStyle(
        "Cell",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        textColor=TEXT_DARK,
        alignment=1,
    )

    col_widths = [
        usable_w * 0.045,  # Ref #
        usable_w * 0.115,  # Student ID / USN
        usable_w * 0.220,  # Full Name
        usable_w * 0.095,  # Section / Role
        usable_w * 0.045,  # Sex
        usable_w * 0.040,  # Age
        usable_w * 0.200,  # Email Address
        usable_w * 0.075,  # Time In
        usable_w * 0.075,  # Time Out
        usable_w * 0.090,  # Signature
    ]

    headers = [
        "Ref #",
        "Student ID / USN",
        "Full Name (Last, First, M.I.)",
        "Sec / Role",
        "Sex",
        "Age",
        "Email Address (for Certificate)",
        "Time In",
        "Time Out",
        "Signature",
    ]

    elements: List[Any] = []
    sess_display = session_label.strip() or "[  ] Morning   [  ] Afternoon"

    for p_idx in range(pages):
        # Header block
        hdr_table = Table(
            [
                [
                    Paragraph("ACLC COLLEGE OF MANDAUE &middot; ATTENDANCE SHEET", st_school),
                    Paragraph(f"<b>Session:</b> {_xml_safe(sess_display)}", st_meta),
                ],
                [
                    Paragraph(_xml_safe(event_title), st_title),
                    Paragraph(
                        f"<b>Date:</b> {_xml_safe(event_date)} &nbsp;|&nbsp; <b>Venue:</b> {_xml_safe(venue)}",
                        st_meta,
                    ),
                ],
            ],
            colWidths=[usable_w * 0.62, usable_w * 0.38],
        )
        hdr_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        elements.append(hdr_table)
        elements.append(Spacer(1, 2.5 * mm))

        table_data = [headers]
        start_num = p_idx * rows_per_page + 1
        for r_idx in range(rows_per_page):
            ref_code = f"{start_num + r_idx:03d}"
            table_data.append([Paragraph(ref_code, st_cell), "", "", "", "", "", "", "", "", ""])

        row_heights = [7.5 * mm] + [7.3 * mm] * rows_per_page
        sheet_table = Table(table_data, colWidths=col_widths, rowHeights=row_heights, repeatRows=1)
        sheet_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                    ("BOX", (0, 0), (-1, -1), 1.0, colors.black),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        elements.append(sheet_table)

        if p_idx < pages - 1:
            elements.append(PageBreak())

    footer_left = f"{event_title} | Attendance Sheet"
    doc.build(
        elements,
        onFirstPage=lambda c, d: _draw_footer(c, d, footer_left),
        onLaterPages=lambda c, d: _draw_footer(c, d, footer_left),
    )
    return buf.getvalue()


def generate_blank_evaluation_pdf() -> bytes:
    """
    Generates a clean, professional Portrait A4 Evaluation Form PDF from the live
    quiz schema. Prominently asks only for 'Student ID or Attendance Ref #' at the
    top so attendees can match their Attendance Sheet row without rewriting details.
    """
    settings = get_all_settings()
    event_title = settings.get("event_title", "HIV Awareness Seminar")
    event_date = settings.get("event_date", datetime.now().strftime("%B %d, %Y"))
    venue = settings.get("venue", "ACLC College of Mandaue")
    schema = get_eval_quiz_schema()

    buf = io.BytesIO()
    page_w, _ = A4
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=13 * mm,
        rightMargin=13 * mm,
        topMargin=11 * mm,
        bottomMargin=14 * mm,
        title=f"{event_title} - Printable Evaluation Form",
    )
    usable_w = page_w - doc.leftMargin - doc.rightMargin

    styles = getSampleStyleSheet()
    st_sub = ParagraphStyle(
        "Sub", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8.5, textColor=HIV_CRIMSON
    )
    st_title = ParagraphStyle(
        "Title", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=14, textColor=ACLC_BLUE, leading=17
    )
    st_meta = ParagraphStyle(
        "Meta", parent=styles["Normal"], fontName="Helvetica", fontSize=8, textColor=TEXT_MUTED, leading=10
    )
    st_sec = ParagraphStyle(
        "Sec", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=10, textColor=HIV_CRIMSON, leading=13
    )
    st_q = ParagraphStyle(
        "Q", parent=styles["Normal"], fontName="Helvetica", fontSize=8.3, textColor=TEXT_DARK, leading=10.5
    )
    st_q_bold = ParagraphStyle(
        "QBold", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8.5, textColor=TEXT_DARK, leading=11
    )
    st_scale = ParagraphStyle(
        "Scale", parent=styles["Normal"], fontName="Helvetica", fontSize=7.8, textColor=TEXT_DARK, leading=10
    )

    elements: List[Any] = []

    # Header
    elements.append(Paragraph("ACLC COLLEGE OF MANDAUE &middot; SEMINAR EVALUATION FORM", st_sub))
    elements.append(Paragraph(_xml_safe(event_title), st_title))
    elements.append(
        Paragraph(f"Date: {_xml_safe(event_date)} &nbsp;|&nbsp; Venue: {_xml_safe(venue)}", st_meta)
    )
    elements.append(Spacer(1, 3 * mm))

    # Simple ID / Ref # Box
    id_box = Table(
        [
            [
                Paragraph("<b>Student ID / Ref #:</b>", st_q_bold),
                "",
                Paragraph("<b>Name (Optional):</b>", st_q_bold),
                "",
            ]
        ],
        colWidths=[usable_w * 0.18, usable_w * 0.27, usable_w * 0.17, usable_w * 0.38],
        rowHeights=[9 * mm],
    )
    id_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.9, colors.black),
                ("LINEAFTER", (1, 0), (1, 0), 0.6, BORDER_GRAY),
                ("LINEBELOW", (1, 0), (1, 0), 0.6, colors.black),
                ("LINEBELOW", (3, 0), (3, 0), 0.6, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    elements.append(id_box)
    elements.append(Spacer(1, 3 * mm))

    # Part I: Rating Questions
    part1 = [q for q in (schema.get("part1") or []) if isinstance(q, dict) and q.get("enabled", True)]
    if part1:
        elements.append(Paragraph("I. Evaluation of the Seminar", st_sec))
        elements.append(
            Paragraph(
                "5 Strongly Agree &nbsp;&middot;&nbsp; 4 Agree &nbsp;&middot;&nbsp; 3 Neutral &nbsp;&middot;&nbsp; 2 Disagree &nbsp;&middot;&nbsp; 1 Strongly Disagree",
                st_scale,
            )
        )
        elements.append(Spacer(1, 1.5 * mm))

        p1_rows: List[List[Any]] = [["Statement / Evaluation Criteria", "5", "4", "3", "2", "1"]]
        for q in part1:
            if q.get("type") == "rating":
                p1_rows.append([Paragraph(_xml_safe(q.get("text", "")), st_q), "[  ]", "[  ]", "[  ]", "[  ]", "[  ]"])
            else:
                p1_rows.append(
                    [
                        Paragraph(f"{_xml_safe(q.get('text', ''))}<br/>____________________________________________________________", st_q),
                        "-",
                        "-",
                        "-",
                        "-",
                        "-",
                    ]
                )

        score_w = 10.5 * mm
        p1_table = Table(
            p1_rows,
            colWidths=[usable_w - (5 * score_w), score_w, score_w, score_w, score_w, score_w],
            repeatRows=1,
        )
        p1_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("FONTNAME", (1, 1), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (1, 1), (-1, -1), 8.5),
                    ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                    ("BOX", (0, 0), (-1, -1), 0.9, colors.black),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        elements.append(p1_table)
        elements.append(Spacer(1, 3.5 * mm))

    # Part II & Part III: Open-Ended Questions
    def _add_open_section(sec_title: str, items: List[Dict[str, Any]]):
        enabled = [q for q in items if isinstance(q, dict) and q.get("enabled", True)]
        if not enabled:
            return
        elements.append(Paragraph(sec_title, st_sec))
        elements.append(Spacer(1, 1 * mm))
        for q in enabled:
            q_block: List[Any] = [Paragraph(_xml_safe(q.get("text", "")), st_q_bold)]
            if q.get("type") == "rating":
                q_block.append(
                    Paragraph("Rating (circle one): &nbsp; [ 5 ] &nbsp;&nbsp; [ 4 ] &nbsp;&nbsp; [ 3 ] &nbsp;&nbsp; [ 2 ] &nbsp;&nbsp; [ 1 ]", st_q)
                )
            else:
                line_table = Table(
                    [[""], [""]],
                    colWidths=[usable_w],
                    rowHeights=[6.5 * mm, 6.5 * mm],
                )
                line_table.setStyle(
                    TableStyle(
                        [
                            ("LINEBELOW", (0, 0), (-1, -1), 0.6, HexColor("#94A3B8")),
                            ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ]
                    )
                )
                q_block.append(line_table)
            q_block.append(Spacer(1, 2.5 * mm))
            elements.append(KeepTogether(q_block))

    _add_open_section("II. Learning Outcomes", schema.get("part2") or [])
    _add_open_section("III. Overall Assessment", schema.get("part3") or [])

    footer_left = f"{event_title} | Seminar Evaluation Form"
    doc.build(
        elements,
        onFirstPage=lambda c, d: _draw_footer(c, d, footer_left),
        onLaterPages=lambda c, d: _draw_footer(c, d, footer_left),
    )
    return buf.getvalue()


def generate_filled_attendance_pdf() -> bytes:
    """Generates a print-ready A4 Landscape Master Attendance Report from SQLite."""
    settings = get_all_settings()
    event_title = settings.get("event_title", "HIV Awareness Seminar")
    event_date = settings.get("event_date", datetime.now().strftime("%B %d, %Y"))
    venue = settings.get("venue", "ACLC College of Mandaue")
    stats = get_statistics()
    attendees = get_all_attendees()

    buf = io.BytesIO()
    page_w, _ = landscape(A4)
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=11 * mm,
        rightMargin=11 * mm,
        topMargin=10 * mm,
        bottomMargin=14 * mm,
        title=f"{event_title} - Attendance Master Report",
    )
    usable_w = page_w - doc.leftMargin - doc.rightMargin

    styles = getSampleStyleSheet()
    st_sub = ParagraphStyle("Sub", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8.5, textColor=HIV_CRIMSON)
    st_title = ParagraphStyle("Title", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=14, textColor=ACLC_BLUE, leading=17)
    st_meta = ParagraphStyle("Meta", parent=styles["Normal"], fontName="Helvetica", fontSize=8.2, textColor=TEXT_DARK)
    st_cell = ParagraphStyle("Cell", parent=styles["Normal"], fontName="Helvetica", fontSize=7.5, textColor=TEXT_DARK, leading=9.5)

    elements: List[Any] = [
        Paragraph("ACLC COLLEGE OF MANDAUE &middot; ATTENDANCE MASTER PRINTOUT", st_sub),
        Paragraph(_xml_safe(event_title), st_title),
        Paragraph(
            f"Date: {_xml_safe(event_date)} &nbsp;|&nbsp; Venue: {_xml_safe(venue)} &nbsp;|&nbsp; "
            f"Total: <b>{stats['total']}</b> &nbsp;|&nbsp; "
            f"Morning In/Out: <b>{stats['morning_in']}/{stats['morning_out']}</b> &nbsp;|&nbsp; "
            f"Afternoon In/Out: <b>{stats['afternoon_in']}/{stats['afternoon_out']}</b> &nbsp;|&nbsp; "
            f"Cert Eligible: <b>{stats['eligible']}</b>",
            st_meta,
        ),
        Spacer(1, 3 * mm),
    ]

    col_widths = [
        usable_w * 0.04,   # #
        usable_w * 0.12,   # ID
        usable_w * 0.22,   # Full Name
        usable_w * 0.10,   # Section
        usable_w * 0.07,   # Level
        usable_w * 0.06,   # Sex/Age
        usable_w * 0.12,   # Time In
        usable_w * 0.12,   # Time Out
        usable_w * 0.06,   # Mins
        usable_w * 0.09,   # Status
    ]

    rows: List[List[Any]] = [
        ["#", "Student ID", "Full Name", "Section", "Level", "Sex/Age", "Time In", "Time Out", "Mins", "Cert"]
    ]
    for idx, a in enumerate(attendees, start=1):
        sex_short = (a.get("sex") or "")[:1].upper() or "-"
        age_str = str(a.get("age") or "") if (a.get("age") or 0) > 0 else "-"
        cert_lbl = "ELIGIBLE" if a.get("is_eligible") else (a.get("cert_status") or "PENDING")
        rows.append(
            [
                str(idx),
                Paragraph(_xml_safe(a.get("student_id", "")), st_cell),
                Paragraph(_xml_safe(a.get("full_name", "")), st_cell),
                Paragraph(_xml_safe(a.get("section", "")), st_cell),
                _winansi_safe(a.get("level", "")),
                f"{sex_short}/{age_str}",
                Paragraph(_xml_safe(a.get("primary_in_fmt", "-")), st_cell),
                Paragraph(_xml_safe(a.get("primary_out_fmt", "-")), st_cell),
                str(a.get("primary_duration_min", 0)),
                Paragraph(_xml_safe(cert_lbl), st_cell),
            ]
        )

    if len(rows) == 1:
        rows.append(["-", "", "No attendees recorded yet.", "", "", "", "", "", "", ""])

    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("FONTSIZE", (0, 1), (-1, -1), 7.5),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (8, 0), (8, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                ("BOX", (0, 0), (-1, -1), 0.9, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    elements.append(tbl)

    footer_left = f"{event_title} | Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    doc.build(
        elements,
        onFirstPage=lambda c, d: _draw_footer(c, d, footer_left),
        onLaterPages=lambda c, d: _draw_footer(c, d, footer_left),
    )
    return buf.getvalue()


def generate_filled_evaluations_pdf() -> bytes:
    """
    Generates a Portrait A4 Evaluation Summary & Individual Answers PDF:
    - Page 1: Executive Tally (Overall Avg, Part I Criteria 1-10 Averages, Demographics).
    - Pages 2+: Individual attendee ratings and written open-ended answers.
    """
    settings = get_all_settings()
    event_title = settings.get("event_title", "HIV Awareness Seminar")
    event_date = settings.get("event_date", datetime.now().strftime("%B %d, %Y"))
    venue = settings.get("venue", "ACLC College of Mandaue")
    stats = get_statistics()
    records = get_all_evaluations_for_export()
    schema = get_eval_quiz_schema()

    buf = io.BytesIO()
    page_w, _ = A4
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=13 * mm,
        rightMargin=13 * mm,
        topMargin=11 * mm,
        bottomMargin=14 * mm,
        title=f"{event_title} - Evaluation Answers & Summary",
    )
    usable_w = page_w - doc.leftMargin - doc.rightMargin

    styles = getSampleStyleSheet()
    st_sub = ParagraphStyle("Sub", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8.5, textColor=HIV_CRIMSON)
    st_title = ParagraphStyle("Title", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=14, textColor=ACLC_BLUE, leading=17)
    st_sec = ParagraphStyle("Sec", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=10.5, textColor=ACLC_BLUE, leading=14)
    st_body = ParagraphStyle("Body", parent=styles["Normal"], fontName="Helvetica", fontSize=8.2, textColor=TEXT_DARK, leading=10.5)

    ev_stats = stats.get("evaluations", {})
    crit_avgs = ev_stats.get("criteria_avgs", [0.0] * 10)

    elements: List[Any] = [
        Paragraph("ACLC COLLEGE OF MANDAUE &middot; SEMINAR EVALUATION TALLY &amp; ANSWERS", st_sub),
        Paragraph(_xml_safe(event_title), st_title),
        Paragraph(
            f"Date: {_xml_safe(event_date)} &nbsp;|&nbsp; Venue: {_xml_safe(venue)} &nbsp;|&nbsp; "
            f"Submitted: <b>{ev_stats.get('submitted', 0)}</b> &nbsp;|&nbsp; "
            f"In-Progress Drafts: <b>{ev_stats.get('in_progress', 0)}</b> &nbsp;|&nbsp; "
            f"Overall Rating: <b>{ev_stats.get('overall_avg', 0.0)} / 5.0</b>",
            st_body,
        ),
        Spacer(1, 3.5 * mm),
        Paragraph("I. Part I Criteria Averages (1 to 5 Scale)", st_sec),
        Spacer(1, 1.5 * mm),
    ]

    default_labels = [
        "1. Objectives clearly presented",
        "2. Speaker knowledge of HIV",
        "3. Information accurate & easy to understand",
        "4. Clear understanding of HIV transmission & prevention",
        "5. Addressed misconceptions & stigma",
        "6. Encouraged active participation",
        "7. Increased awareness of prevention & testing",
        "8. Respect & non-discrimination toward PLHIV",
        "9. Materials supported understanding",
        "10. Overall informative & beneficial",
    ]
    crit_rows: List[List[Any]] = [["Evaluation Criterion", "Average (1-5)"]]
    for i, lbl in enumerate(default_labels):
        avg_val = crit_avgs[i] if i < len(crit_avgs) else 0.0
        crit_rows.append([Paragraph(_xml_safe(lbl), st_body), f"{avg_val:.2f}"])

    crit_tbl = Table(crit_rows, colWidths=[usable_w * 0.80, usable_w * 0.20], repeatRows=1)
    crit_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8.5),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
                ("BOX", (0, 0), (-1, -1), 0.9, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    elements.append(crit_tbl)
    elements.append(Spacer(1, 5 * mm))
    elements.append(Paragraph(f"II. Individual Evaluation Responses ({len(records)} records)", st_sec))
    elements.append(Spacer(1, 2 * mm))

    standard_qids = {f"q1_{i}" for i in range(1, 11)} | {f"q{p}_{j}" for p in (2, 3) for j in range(1, 4)}
    custom_items = [
        item for pk in ("part1", "part2", "part3")
        for item in (schema.get(pk) or [])
        if isinstance(item, dict) and item.get("id") and item.get("id") not in standard_qids
    ]

    if not records:
        elements.append(Paragraph("No evaluation responses recorded yet.", st_body))
    else:
        for r in records:
            scores = " | ".join(f"Q{i}:{r.get(f'q1_{i}') or '-'}" for i in range(1, 11))
            draft_map: Dict[str, Any] = {}
            if r.get("draft_json"):
                try:
                    draft_map = json.loads(r["draft_json"])
                except Exception:
                    draft_map = {}

            custom_lines = []
            for ci in custom_items:
                val = draft_map.get(ci.get("id"))
                if val is not None and str(val).strip() != "":
                    custom_lines.append(f"<b>{_xml_safe(ci.get('text') or ci.get('id'))}:</b> {_xml_safe(val)}")

            card_html = (
                f"<b>{_xml_safe(r.get('student_id'))}</b> &mdash; <b>{_xml_safe(r.get('full_name'))}</b> "
                f"({_xml_safe(r.get('section'))}) &nbsp;|&nbsp; "
                f"Status: <b>{_xml_safe(r.get('e_status'))}</b> &nbsp;|&nbsp; "
                f"Avg: <b>{r.get('q1_avg') or 0} / 5.0</b><br/>"
                f"<font size='7.5' color='#475569'>{_xml_safe(scores)}</font><br/>"
                f"<b>2.1 Learned:</b> {_xml_safe(r.get('q2_1') or '-')} &nbsp;|&nbsp; "
                f"<b>2.2 Misconception:</b> {_xml_safe(r.get('q2_2') or '-')} &nbsp;|&nbsp; "
                f"<b>2.3 Apply:</b> {_xml_safe(r.get('q2_3') or '-')}<br/>"
                f"<b>3.1 Helpful:</b> {_xml_safe(r.get('q3_1') or '-')} &nbsp;|&nbsp; "
                f"<b>3.2 More Topics:</b> {_xml_safe(r.get('q3_2') or '-')} &nbsp;|&nbsp; "
                f"<b>3.3 Suggestions:</b> {_xml_safe(r.get('q3_3') or '-')}"
            )
            if custom_lines:
                card_html += "<br/>" + "<br/>".join(custom_lines)

            card_tbl = Table([[Paragraph(card_html, st_body)]], colWidths=[usable_w])
            card_tbl.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
                        ("BOX", (0, 0), (-1, -1), 0.6, BORDER_GRAY),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            elements.append(KeepTogether([card_tbl, Spacer(1, 2 * mm)]))

    footer_left = f"{event_title} | Evaluation Summary & Answers | Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    doc.build(
        elements,
        onFirstPage=lambda c, d: _draw_footer(c, d, footer_left),
        onLaterPages=lambda c, d: _draw_footer(c, d, footer_left),
    )
    return buf.getvalue()


def _make_qr_flowable(data: str, size_mm: float = 38.0, fill_color: str = "#000000") -> Image:
    """Generates a crisp QR code image wrapped as a ReportLab Image flowable."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color=fill_color, back_color="#FFFFFF")
    img_buf = io.BytesIO()
    img.save(img_buf, format="PNG")
    img_buf.seek(0)
    return Image(img_buf, width=size_mm * mm, height=size_mm * mm)


def _make_hiv_ribbon_drawing(size_mm: float = 12.0) -> Drawing:
    """Draws a crisp vector Red & White HIV Awareness Ribbon icon."""
    s = size_mm * mm
    d = Drawing(s, s)
    # Soft white circular badge behind the ribbon so it pops on Crimson header
    # Left & right ribbon legs + top loop
    d.add(
        Polygon(
            [
                s * 0.22, s * 0.08,
                s * 0.36, s * 0.08,
                s * 0.68, s * 0.68,
                s * 0.60, s * 0.88,
                s * 0.44, s * 0.88,
                s * 0.56, s * 0.66,
            ],
            fillColor=colors.white,
            strokeColor=None,
        )
    )
    d.add(
        Polygon(
            [
                s * 0.78, s * 0.08,
                s * 0.64, s * 0.08,
                s * 0.32, s * 0.68,
                s * 0.40, s * 0.88,
                s * 0.56, s * 0.88,
                s * 0.44, s * 0.66,
            ],
            fillColor=HexColor("#FEE2E2"),
            strokeColor=None,
        )
    )
    return d


def _file_to_data_uri(path: str, mime: str = "image/png") -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _qr_to_data_uri(data: str, fill_color: str = "#0F172A") -> str:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color=fill_color, back_color="#FFFFFF")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _render_html_infographic_pdf(context: Dict[str, Any]) -> Optional[bytes]:
    """Renders templates/infographic.html to A4 PDF using headless Chromium if installed."""
    browser = shutil.which("chromium-browser") or shutil.which("chromium") or shutil.which("google-chrome")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    tpl_dir = os.path.join(base_dir, "templates")
    if not browser or not os.path.exists(os.path.join(tpl_dir, "infographic.html")):
        return None

    env = Environment(loader=FileSystemLoader(tpl_dir), autoescape=True)
    html = env.get_template("infographic.html").render(**context)

    with tempfile.TemporaryDirectory() as tmpdir:
        html_path = os.path.join(tmpdir, "infographic.html")
        pdf_path = os.path.join(tmpdir, "infographic.pdf")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            subprocess.run(
                [
                    browser,
                    "--headless",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--print-to-pdf-no-header",
                    f"--print-to-pdf={pdf_path}",
                    f"file://{html_path}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=True,
            )
            if os.path.exists(pdf_path):
                with open(pdf_path, "rb") as pf:
                    data = pf.read()
                if data.startswith(b"%PDF-"):
                    return data
        except Exception:
            return None
    return None


def generate_infographic_pdf(static_url: Optional[str] = None) -> bytes:
    """
    Generates a vibrant, 1-page A4 Portrait Red & White HIV Awareness Month
    Infographic PDF with partner logos, Open Wi-Fi QR, Static Student Portal QR & URL,
    and simplified step-by-step instructions for Guidance Page posting and printing.
    """
    settings = get_all_settings()
    event_title = settings.get("event_title", "HIV Awareness Seminar")
    event_date = settings.get("event_date", "November 25, 2026")
    venue = settings.get("venue", "ACLC College of Mandaue")

    raw_url = (static_url or settings.get("kiosk_url") or "http://192.168.1.100:8080").strip()
    if raw_url.lower().startswith("https://"):
        raw_url = "http://" + raw_url[8:]
    elif not raw_url.lower().startswith("http://"):
        raw_url = "http://" + raw_url.lstrip("/")
    portal_url = raw_url.rstrip("/")
    display_host = re.sub(r"^https?://", "", portal_url, flags=re.IGNORECASE)
    if display_host.endswith(":80"):
        display_host = display_host[:-3]

    ssids = []
    for key in ("wifi_ssid", "wifi_ssid_2", "wifi_ssid_3"):
        val = (settings.get(key) or "").strip()
        if val and val not in ssids:
            ssids.append(val)
    if not ssids:
        ssids = ["HIV Awareness Seminar 1"]
    primary_ssid = ssids[0]
    wifi_password = (settings.get("wifi_password") or "").strip()
    wifi_auth = "WPA" if wifi_password else "nopass"
    wifi_qr_payload = f"WIFI:S:{primary_ssid};T:{wifi_auth};P:{wifi_password};;"

    try:
        min_mins = int(float(settings.get("min_duration_minutes", "150") or "150"))
    except ValueError:
        min_mins = 150
    hrs, rem_mins = divmod(min_mins, 60)
    if hrs and rem_mins:
        dur_label = f"{hrs} hrs {rem_mins} mins ({min_mins} mins)"
    elif hrs:
        dur_label = f"{hrs} hrs ({min_mins} mins)"
    else:
        dur_label = f"{min_mins} mins"

    img_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "img")
    html_pdf = _render_html_infographic_pdf(
        {
            "event_title": event_title,
            "event_date": event_date,
            "venue": venue,
            "display_host": display_host,
            "ssids": ssids,
            "wifi_password": wifi_password,
            "dur_label": dur_label,
            "wifi_qr_uri": _qr_to_data_uri(wifi_qr_payload, "#0F172A"),
            "portal_qr_uri": _qr_to_data_uri(f"{portal_url}/", "#B91C1C"),
            "logo_aclc": _file_to_data_uri(os.path.join(img_dir, "aclc-logo.png")),
            "logo_guidance": _file_to_data_uri(os.path.join(img_dir, "guidance-logo.png")),
            "logo_aclcert": _file_to_data_uri(os.path.join(img_dir, "aclcert-logo.png")),
        }
    )
    if html_pdf:
        return html_pdf

    # Red & White HIV Awareness Month Palette
    hiv_crimson = HexColor("#B91C1C")
    hiv_dark_red = HexColor("#7F1D1D")
    hiv_bright_red = HexColor("#DC2626")
    hiv_rose_bg = HexColor("#FEF2F2")
    hiv_rose_pill = HexColor("#FEE2E2")
    hiv_border = HexColor("#FCA5A5")
    slate_dark = HexColor("#0F172A")
    slate_muted = HexColor("#334155")
    open_green_bg = HexColor("#DCFCE7")
    open_green_txt = HexColor("#166534")

    buf = io.BytesIO()
    page_w, _ = A4
    margin_lr = 12 * mm
    usable_w = page_w - (2 * margin_lr)
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=margin_lr,
        rightMargin=margin_lr,
        topMargin=10 * mm,
        bottomMargin=11 * mm,
        title=f"{event_title} - Attendance & Evaluation Guide",
        author="ACLC College of Mandaue",
    )

    styles = getSampleStyleSheet()
    st_ribbon_tag = ParagraphStyle(
        "HivRibbonTag",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=hiv_rose_pill,
        alignment=2,
    )
    st_hero_title = ParagraphStyle(
        "HivHeroTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=21,
        leading=24,
        textColor=colors.white,
    )
    st_hero_tagline = ParagraphStyle(
        "HivHeroTagline",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=hiv_rose_pill,
    )
    st_hero_meta = ParagraphStyle(
        "HivHeroMeta",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.8,
        leading=12,
        textColor=colors.white,
    )
    st_card_hdr = ParagraphStyle(
        "HivCardHdr",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=13,
        textColor=colors.white,
        alignment=1,
    )
    st_qr_sub = ParagraphStyle(
        "HivQrSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11.5,
        textColor=slate_muted,
        alignment=1,
    )
    st_ssid_list = ParagraphStyle(
        "HivSsidList",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9.2,
        leading=12,
        textColor=slate_dark,
        alignment=1,
    )
    st_open_pill = ParagraphStyle(
        "HivOpenPill",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.8,
        leading=11.5,
        textColor=open_green_txt,
        alignment=1,
    )
    st_pwd_pill = ParagraphStyle(
        "HivPwdPill",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9.5,
        leading=12,
        textColor=hiv_dark_red,
        alignment=1,
    )
    st_mono_box = ParagraphStyle(
        "HivMonoBox",
        parent=styles["Normal"],
        fontName="Courier-Bold",
        fontSize=12.5,
        leading=15,
        textColor=hiv_crimson,
        alignment=1,
    )
    st_sec_hdr = ParagraphStyle(
        "HivSecHdr",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=hiv_crimson,
    )
    st_step_num = ParagraphStyle(
        "HivStepNum",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=15,
        textColor=colors.white,
        alignment=1,
    )
    st_step_title = ParagraphStyle(
        "HivStepTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10.2,
        leading=13,
        textColor=hiv_dark_red,
    )
    st_step_body = ParagraphStyle(
        "HivStepBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12.5,
        textColor=slate_dark,
    )
    st_banner_note = ParagraphStyle(
        "HivBannerNote",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11.5,
        textColor=colors.white,
        alignment=1,
    )

    elements: List[Any] = []

    # 1. Bold Red & White HIV Awareness Month Hero Banner
    img_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "img")
    logo_cells: List[Any] = []
    for fname in ("aclc-logo.png", "guidance-logo.png", "aclcert-logo.png"):
        fpath = os.path.join(img_dir, fname)
        if os.path.exists(fpath):
            logo_cells.append(Image(fpath, width=13.5 * mm, height=13.5 * mm))

    logos_strip = Table([logo_cells], colWidths=[15.5 * mm] * len(logo_cells)) if logo_cells else ""
    if logos_strip:
        logos_strip.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ("ROUNDEDCORNERS", [4, 4, 4, 4]),
                ]
            )
        )

    ribbon_badge = Table(
        [
            [
                Paragraph("HIV AWARENESS MONTH<br/><b>OFFICIAL EVENT GUIDE</b>", st_ribbon_tag),
                _make_hiv_ribbon_drawing(11.5),
            ]
        ],
        colWidths=[42 * mm, 13 * mm],
    )
    ribbon_badge.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    top_bar = Table(
        [[logos_strip, "", ribbon_badge]],
        colWidths=[len(logo_cells) * 15.5 * mm + 2 * mm, usable_w - (len(logo_cells) * 15.5 * mm + 2 * mm) - 57 * mm - 16 * mm, 57 * mm],
    )
    top_bar.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    hero_card = Table(
        [
            [top_bar],
            [Paragraph(_xml_safe(event_title.upper()), st_hero_title)],
            [Paragraph('&ldquo;KNOWLEDGE IS POWER, PREVENTION IS PROTECTION&rdquo;', st_hero_tagline)],
            [
                Paragraph(
                    f"ATTENDANCE &amp; EVALUATION GUIDE &nbsp;&bull;&nbsp; {_xml_safe(event_date.upper())} &nbsp;&bull;&nbsp; {_xml_safe(venue.upper())}",
                    st_hero_meta,
                )
            ],
        ],
        colWidths=[usable_w],
    )
    hero_card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), hiv_crimson),
                ("LINEBELOW", (0, -1), (-1, -1), 3, hiv_dark_red),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (0, 0), 8),
                ("TOPPADDING", (0, 1), (0, -1), 2),
                ("BOTTOMPADDING", (0, 0), (0, -2), 2),
                ("BOTTOMPADDING", (0, -1), (0, -1), 9),
            ]
        )
    )
    elements.append(hero_card)
    elements.append(Spacer(1, 4.5 * mm))

    # 2. Side-by-Side QR Cards (White Cards with Crimson Accents)
    col_w = (usable_w - 6 * mm) / 2.0
    wifi_qr_img = _make_qr_flowable(wifi_qr_payload, size_mm=37.0, fill_color="#0F172A")
    portal_qr_img = _make_qr_flowable(f"{portal_url}/", size_mm=37.0, fill_color="#B91C1C")

    ssid_html = "<br/>".join(_xml_safe(s) for s in ssids)
    if wifi_password:
        auth_pill = Table([[Paragraph(f"Password: {_xml_safe(wifi_password)}", st_pwd_pill)]], colWidths=[col_w - 14 * mm])
        auth_pill.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), hiv_rose_pill),
                    ("BOX", (0, 0), (-1, -1), 0.8, hiv_border),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
    else:
        auth_pill = Table([[Paragraph("OPEN NETWORK (NO PASSWORD)", st_open_pill)]], colWidths=[col_w - 14 * mm])
        auth_pill.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), open_green_bg),
                    ("BOX", (0, 0), (-1, -1), 0.8, open_green_txt),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )

    wifi_card = Table(
        [
            [Paragraph("1. SCAN TO JOIN WI-FI", st_card_hdr)],
            [wifi_qr_img],
            [Paragraph("Connect to any available network:", st_qr_sub)],
            [Paragraph(ssid_html, st_ssid_list)],
            [auth_pill],
            [Paragraph('<i>If prompted "No Internet", tap <b>Stay Connected</b>.</i>', st_qr_sub)],
        ],
        colWidths=[col_w],
    )
    wifi_card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), hiv_crimson),
                ("BACKGROUND", (0, 1), (0, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 1.2, hiv_crimson),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (0, 0), 6),
                ("BOTTOMPADDING", (0, 0), (0, 0), 6),
                ("TOPPADDING", (0, 1), (0, 1), 6),
                ("BOTTOMPADDING", (0, 1), (0, 1), 3),
                ("TOPPADDING", (0, 2), (0, -1), 2),
                ("BOTTOMPADDING", (0, 2), (0, -2), 2),
                ("BOTTOMPADDING", (0, -1), (0, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    url_pill = Table([[Paragraph(_xml_safe(display_host), st_mono_box)]], colWidths=[col_w - 14 * mm])
    url_pill.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), hiv_rose_bg),
                ("BOX", (0, 0), (-1, -1), 1.0, hiv_crimson),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    portal_card = Table(
        [
            [Paragraph("2. SCAN TO OPEN PORTAL", st_card_hdr)],
            [portal_qr_img],
            [Paragraph("Or open Chrome / Safari and type this link:", st_qr_sub)],
            [url_pill],
            [Paragraph("<i>No https:// needed. Works 100% offline on event Wi-Fi.</i>", st_qr_sub)],
        ],
        colWidths=[col_w],
    )
    portal_card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), hiv_dark_red),
                ("BACKGROUND", (0, 1), (0, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 1.2, hiv_dark_red),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (0, 0), 6),
                ("BOTTOMPADDING", (0, 0), (0, 0), 6),
                ("TOPPADDING", (0, 1), (0, 1), 6),
                ("BOTTOMPADDING", (0, 1), (0, 1), 4),
                ("TOPPADDING", (0, 2), (0, -1), 3),
                ("BOTTOMPADDING", (0, 2), (0, -2), 3),
                ("BOTTOMPADDING", (0, -1), (0, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    qr_row = Table([[wifi_card, "", portal_card]], colWidths=[col_w, 6 * mm, col_w])
    qr_row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    elements.append(qr_row)
    elements.append(Spacer(1, 5 * mm))

    # 3. Easy 4-Step Flow
    elements.append(Paragraph("HOW IT WORKS &mdash; 4 EASY STEPS", st_sec_hdr))
    elements.append(Spacer(1, 2.5 * mm))

    steps = [
        (
            "1",
            "Time In &amp; Save Your QR Pass",
            f"Open <b>{_xml_safe(display_host)}</b> &rarr; Enter your <b>Student ID, Full Name, Course/Strand &amp; Email</b> &rarr; Tap <b>Submit Time In</b> &rarr; <b>Screenshot your QR Pass</b>.",
        ),
        (
            "2",
            "Take Evaluation (When Announced)",
            f"Open <b>{_xml_safe(display_host)}</b> &rarr; Click <b>&ldquo;My Passes&rdquo;</b> at the top &rarr; Under your QR Pass, click <b>&ldquo;Take Seminar Evaluation&rdquo;</b> &rarr; Answer &amp; Submit.",
        ),
        (
            "3",
            "Time Out Before Leaving",
            "Open <b>&ldquo;My Passes&rdquo;</b> (or show your saved QR screenshot) at the <b>Exit Scanner</b> so an officer can scan your Time Out.",
        ),
        (
            "4",
            "Earn Your Certificate",
            f"Complete at least <b>{_xml_safe(dur_label)}</b> of attendance <b>+</b> submit the <b>Evaluation Form</b> to receive your official Certificate of Participation.",
        ),
    ]

    for idx, (num, title, body) in enumerate(steps):
        row_bg = colors.white if idx % 2 == 0 else hiv_rose_bg
        step_tbl = Table(
            [
                [
                    Paragraph(num, st_step_num),
                    [
                        Paragraph(title, st_step_title),
                        Spacer(1, 0.8 * mm),
                        Paragraph(body, st_step_body),
                    ],
                ]
            ],
            colWidths=[11 * mm, usable_w - 11 * mm],
        )
        step_tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, 0), hiv_crimson),
                    ("BACKGROUND", (1, 0), (1, 0), row_bg),
                    ("BOX", (0, 0), (-1, -1), 0.9, hiv_border),
                    ("VALIGN", (0, 0), (0, 0), "MIDDLE"),
                    ("VALIGN", (1, 0), (1, 0), "TOP"),
                    ("LEFTPADDING", (0, 0), (0, 0), 2),
                    ("RIGHTPADDING", (0, 0), (0, 0), 2),
                    ("LEFTPADDING", (1, 0), (1, 0), 8),
                    ("RIGHTPADDING", (1, 0), (1, 0), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5.5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
                ]
            )
        )
        elements.append(step_tbl)
        elements.append(Spacer(1, 2.2 * mm))

    elements.append(Spacer(1, 1.5 * mm))

    # 4. Bottom Red Reminder Strip
    bottom_bar = Table(
        [
            [
                Paragraph(
                    "100% OFFLINE (NO MOBILE DATA NEEDED) &nbsp;&bull;&nbsp; DOUBLE-CHECK YOUR FULL NAME &amp; EMAIL FOR CERTIFICATES",
                    st_banner_note,
                )
            ]
        ],
        colWidths=[usable_w],
    )
    bottom_bar.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), hiv_dark_red),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    elements.append(bottom_bar)

    footer_left = f"ACLC College of Mandaue  |  {event_title}  |  Static Portal: {display_host}"
    doc.build(
        elements,
        onFirstPage=lambda c, d: _draw_footer(c, d, footer_left),
        onLaterPages=lambda c, d: _draw_footer(c, d, footer_left),
    )
    return buf.getvalue()
