import os
import io
import re
import time
import smtplib
import threading
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr, formataddr
from typing import Dict, Any, Optional, Tuple, List

from PIL import Image, ImageOps
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.lib.colors import HexColor

from database import (
    get_all_settings,
    get_all_attendees,
    claim_certificate_for_sending,
    update_certificate_delivery_status,
    get_pending_certificate_student_ids,
    get_certificate_delivery_summary,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CERT_OUTPUT_DIR = os.path.join(DATA_DIR, "certificates")
CERT_TEMPLATE_FILE = os.path.join(DATA_DIR, "cert_template.png")

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
ALLOWED_FONTS = {
    "Helvetica-Bold": "Helvetica-Bold",
    "Times-Bold": "Times-Bold",
    "Courier-Bold": "Courier-Bold",
    "Helvetica": "Helvetica",
    "Times-Roman": "Times-Roman",
}

# Thread-safe cache for normalized Canva template image bytes
_TEMPLATE_CACHE_LOCK = threading.Lock()
_TEMPLATE_CACHE: Dict[str, Any] = {
    "path": None,
    "mtime": None,
    "img_bytes": None,
    "width": 0,
    "height": 0,
}

# Thread-safe singleton state for background batch email sending
_BATCH_LOCK = threading.Lock()
_BATCH_STATE: Dict[str, Any] = {
    "running": False,
    "total": 0,
    "processed": 0,
    "sent": 0,
    "failed": 0,
    "skipped": 0,
    "last_error": "",
    "started_at": "",
    "finished_at": "",
}


def is_valid_email(raw_email: Optional[str]) -> bool:
    """Validates basic email syntax and rejects placeholders like N/A."""
    if not raw_email:
        return False
    _, addr = parseaddr(raw_email.strip())
    if not addr or len(addr) > 254:
        return False
    return bool(EMAIL_REGEX.match(addr))


def safe_filename_part(student_id: str) -> str:
    """Sanitizes a Student ID for safe cross-platform PDF filenames."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", (student_id or "ATTENDEE").strip().upper())
    return cleaned or "ATTENDEE"


def _winansi_safe(text: str) -> str:
    """Ensures text is representable in ReportLab's WinAnsiEncoding without crashing."""
    if not text:
        return ""
    try:
        text.encode("cp1252")
        return text
    except UnicodeEncodeError:
        normalized = unicodedata.normalize("NFKD", text)
        return normalized.encode("cp1252", errors="replace").decode("cp1252")


def _parse_hex_color(raw: str, fallback: str = "#1B365D") -> HexColor:
    val = (raw or "").strip()
    if re.match(r"^#[0-9A-Fa-f]{6}$", val):
        return HexColor(val)
    return HexColor(fallback)


def _clamp_float(val: Any, default: float, lo: float, hi: float) -> float:
    try:
        f = float(val)
        return max(lo, min(hi, f))
    except (TypeError, ValueError):
        return default


def _format_track_info(attendee: Dict[str, Any]) -> str:
    level = (attendee.get("level") or "").upper()
    if level == "SHS":
        parts = [p for p in (attendee.get("shs_grade"), attendee.get("strand"), attendee.get("section")) if p]
        return f"Senior High School - {' | '.join(parts)}" if parts else "Senior High School"
    if level == "COLLEGE":
        parts = [p for p in (attendee.get("college_year"), attendee.get("course"), attendee.get("section")) if p]
        return f"College - {' | '.join(parts)}" if parts else "College"
    sec = attendee.get("section") or ""
    return f"Guest / Faculty ({sec})" if sec else "Guest / Faculty"


def _load_normalized_template(template_path: str) -> Optional[Tuple[bytes, int, int]]:
    """
    Loads and normalizes a Canva PNG/JPG template (EXIF orientation, RGB flatten,
    downscale >2400px to keep email attachments fast & lightweight), cached by mtime.
    """
    if not template_path or not os.path.isfile(template_path):
        return None
    try:
        mtime = os.path.getmtime(template_path)
    except OSError:
        return None

    with _TEMPLATE_CACHE_LOCK:
        if (
            _TEMPLATE_CACHE["path"] == template_path
            and _TEMPLATE_CACHE["mtime"] == mtime
            and _TEMPLATE_CACHE["img_bytes"] is not None
        ):
            return _TEMPLATE_CACHE["img_bytes"], _TEMPLATE_CACHE["width"], _TEMPLATE_CACHE["height"]

    with Image.open(template_path) as raw_img:
        img = ImageOps.exif_transpose(raw_img)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        max_dim = 2400
        if max(img.width, img.height) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92, optimize=True)
        img_bytes = buf.getvalue()
        w, h = img.size

    with _TEMPLATE_CACHE_LOCK:
        _TEMPLATE_CACHE["path"] = template_path
        _TEMPLATE_CACHE["mtime"] = mtime
        _TEMPLATE_CACHE["img_bytes"] = img_bytes
        _TEMPLATE_CACHE["width"] = w
        _TEMPLATE_CACHE["height"] = h

    return img_bytes, w, h


def _draw_builtin_aclc_frame(c: canvas.Canvas, page_w: float, page_h: float, settings: Dict[str, str]) -> None:
    """Fallback ACLC-styled Certificate of Participation frame when no Canva template is uploaded."""
    c.setFillColor(HexColor("#FFFFFF"))
    c.rect(0, 0, page_w, page_h, stroke=0, fill=1)

    # Outer Royal Blue border + Inner Gold border
    c.setStrokeColor(HexColor("#1B365D"))
    c.setLineWidth(6)
    c.rect(24, 24, page_w - 48, page_h - 48, stroke=1, fill=0)

    c.setStrokeColor(HexColor("#F5A623"))
    c.setLineWidth(2)
    c.rect(34, 34, page_w - 68, page_h - 68, stroke=1, fill=0)

    # Header block
    c.setFillColor(HexColor("#1B365D"))
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(page_w / 2.0, page_h - 82, "ACLC COLLEGE OF MANDAUE")

    c.setFillColor(HexColor("#C8102E"))
    c.setFont("Helvetica-Bold", 30)
    c.drawCentredString(page_w / 2.0, page_h - 130, "CERTIFICATE OF PARTICIPATION")

    c.setFillColor(HexColor("#4B5563"))
    c.setFont("Helvetica", 13)
    c.drawCentredString(page_w / 2.0, page_h - 172, "This certificate is proudly presented to")

    # Signature underline under default name position
    name_y_pct = _clamp_float(settings.get("cert_name_y_pct"), 50.0, 5.0, 95.0)
    line_y = page_h * (1.0 - name_y_pct / 100.0) - 10
    c.setStrokeColor(HexColor("#D1D5DB"))
    c.setLineWidth(1)
    c.line(page_w * 0.22, line_y, page_w * 0.78, line_y)

    event_title = _winansi_safe(settings.get("event_title") or "HIV Awareness Seminar")
    event_date = _winansi_safe(settings.get("event_date") or datetime.now().strftime("%B %d, %Y"))
    venue = _winansi_safe(settings.get("venue") or "ACLC College of Mandaue")

    c.setFillColor(HexColor("#374151"))
    c.setFont("Helvetica", 12)
    c.drawCentredString(page_w / 2.0, line_y - 42, f"for actively participating in the {event_title}")
    c.drawCentredString(page_w / 2.0, line_y - 60, f"held on {event_date} at {venue}.")


def _draw_fitted_centered_text(
    c: canvas.Canvas,
    text: str,
    x_pt: float,
    y_pt: float,
    font_name: str,
    base_size: float,
    max_width_pt: float,
    min_size: float = 11.0,
) -> float:
    """Draws centered text, automatically shrinking font size so long names never overflow."""
    size = base_size
    while size > min_size and c.stringWidth(text, font_name, size) > max_width_pt:
        size -= 1.0
    c.setFont(font_name, size)
    c.drawCentredString(x_pt, y_pt, text)
    return size


def generate_certificate_pdf(
    attendee: Dict[str, Any],
    settings: Optional[Dict[str, str]] = None,
    output_path: Optional[str] = None,
) -> bytes:
    """
    Generates a crisp vector-text PDF certificate over the uploaded Canva template
    (or the built-in ACLC frame if no Canva template is uploaded).
    """
    cfg = settings if settings is not None else get_all_settings()
    template_path = (cfg.get("cert_template_path") or "").strip()
    tmpl = _load_normalized_template(template_path)

    if tmpl is not None:
        img_bytes, img_w, img_h = tmpl
        # Scale page to ~842pt on the longest side while keeping exact Canva aspect ratio
        scale = 841.89 / float(max(img_w, img_h))
        page_w = img_w * scale
        page_h = img_h * scale
    else:
        page_w, page_h = landscape(A4)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    full_name_raw = (attendee.get("full_name") or "Attendee").strip()
    c.setTitle(f"Certificate - {_winansi_safe(full_name_raw)}")
    c.setAuthor(_winansi_safe(cfg.get("smtp_from_name") or "ACLC College"))

    if tmpl is not None:
        c.drawImage(
            ImageReader(io.BytesIO(img_bytes)),
            0, 0,
            width=page_w,
            height=page_h,
            preserveAspectRatio=False,
        )
    else:
        _draw_builtin_aclc_frame(c, page_w, page_h, cfg)

    # 1. Recipient Name
    name_x_pct = _clamp_float(cfg.get("cert_name_x_pct"), 50.0, 5.0, 95.0)
    name_y_pct = _clamp_float(cfg.get("cert_name_y_pct"), 50.0, 5.0, 95.0)
    name_size = _clamp_float(cfg.get("cert_name_font_size"), 38.0, 14.0, 96.0)
    name_color = _parse_hex_color(cfg.get("cert_name_color", "#1B365D"))
    font_name = ALLOWED_FONTS.get(cfg.get("cert_name_font", "Helvetica-Bold"), "Helvetica-Bold")
    use_upper = cfg.get("cert_name_uppercase", "1") == "1"

    display_name = _winansi_safe(full_name_raw.upper() if use_upper else full_name_raw)
    name_x_pt = page_w * (name_x_pct / 100.0)
    name_y_pt = page_h * (1.0 - name_y_pct / 100.0)

    c.setFillColor(name_color)
    _draw_fitted_centered_text(
        c,
        display_name,
        name_x_pt,
        name_y_pt,
        font_name,
        name_size,
        max_width_pt=page_w * 0.78,
    )

    # 2. Optional Track / Course / Section
    if cfg.get("cert_show_track", "0") == "1":
        track_y_pct = _clamp_float(cfg.get("cert_track_y_pct"), 57.0, 5.0, 95.0)
        track_size = _clamp_float(cfg.get("cert_track_font_size"), 15.0, 8.0, 36.0)
        track_text = _winansi_safe(_format_track_info(attendee))
        c.setFillColor(HexColor("#374151"))
        _draw_fitted_centered_text(
            c,
            track_text,
            name_x_pt,
            page_h * (1.0 - track_y_pct / 100.0),
            "Helvetica",
            track_size,
            max_width_pt=page_w * 0.76,
            min_size=9.0,
        )

    # 3. Optional Event Date
    if cfg.get("cert_show_date", "0") == "1":
        date_y_pct = _clamp_float(cfg.get("cert_date_y_pct"), 63.0, 5.0, 95.0)
        date_size = _clamp_float(cfg.get("cert_date_font_size"), 13.0, 8.0, 32.0)
        date_text = _winansi_safe(cfg.get("event_date") or datetime.now().strftime("%B %d, %Y"))
        c.setFillColor(HexColor("#4B5563"))
        _draw_fitted_centered_text(
            c,
            date_text,
            name_x_pt,
            page_h * (1.0 - date_y_pct / 100.0),
            "Helvetica",
            date_size,
            max_width_pt=page_w * 0.72,
            min_size=8.0,
        )

    # 4. Optional Certificate Serial ID at bottom-right
    if cfg.get("cert_show_serial", "1") == "1":
        sid_text = _winansi_safe(f"Cert ID: {attendee.get('student_id', '')}")
        c.setFillColor(HexColor("#6B7280"))
        c.setFont("Helvetica", 8.5)
        c.drawRightString(page_w - 36, 22, sid_text)

    c.showPage()
    c.save()
    pdf_bytes = buf.getvalue()

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)

    return pdf_bytes


def _render_template_str(template: str, attendee: Dict[str, Any], settings: Dict[str, str]) -> str:
    replacements = {
        "{full_name}": attendee.get("full_name") or "Participant",
        "{student_id}": attendee.get("student_id") or "",
        "{event_title}": settings.get("event_title") or "HIV Awareness Seminar",
        "{event_date}": settings.get("event_date") or datetime.now().strftime("%B %d, %Y"),
        "{venue}": settings.get("venue") or "ACLC College of Mandaue",
        "{from_name}": settings.get("smtp_from_name") or "ACLC College Seminar Committee",
    }
    out = template
    for k, v in replacements.items():
        out = out.replace(k, v)
    return out


def _build_email_message(
    attendee: Dict[str, Any],
    settings: Dict[str, str],
    pdf_bytes: bytes,
) -> EmailMessage:
    smtp_user = (settings.get("smtp_user") or "").strip()
    from_name = (settings.get("smtp_from_name") or "ACLC College Seminar Committee").strip()
    recipient_email = (attendee.get("email") or "").strip()

    subject_tpl = settings.get("cert_email_subject") or "Your Certificate of Participation - {event_title}"
    body_tpl = settings.get("cert_email_body") or (
        "Dear {full_name},\n\n"
        "Thank you for participating in the {event_title} on {event_date} at {venue}.\n\n"
        "Attached is your official Certificate of Participation (PDF).\n\n"
        "Best regards,\n{from_name}"
    )

    msg = EmailMessage()
    msg["Subject"] = _render_template_str(subject_tpl, attendee, settings)
    msg["From"] = formataddr((from_name, smtp_user))
    msg["To"] = recipient_email
    msg.set_content(_render_template_str(body_tpl, attendee, settings))

    safe_sid = safe_filename_part(attendee.get("student_id") or "ATTENDEE")
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=f"Certificate_{safe_sid}.pdf",
    )
    return msg


def _open_gmail_smtp(settings: Dict[str, str]) -> smtplib.SMTP:
    smtp_user = (settings.get("smtp_user") or "").strip()
    smtp_pass = (settings.get("smtp_app_password") or "").strip().replace(" ", "")
    if not smtp_user or not smtp_pass:
        raise ValueError("Gmail SMTP address and 16-character App Password must be configured in Admin.")

    server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
    server.ehlo()
    server.starttls()
    server.ehlo()
    server.login(smtp_user, smtp_pass)
    return server


def send_single_certificate(student_id: str, force: bool = False) -> Dict[str, Any]:
    """
    Atomically claims an eligible attendee, generates their PDF certificate,
    and emails it via Gmail SMTP. Safe against concurrent triggers.
    """
    attendee = claim_certificate_for_sending(student_id, force=force)
    if not attendee:
        return {"status": "skipped", "message": "Attendee is ineligible, already sent, or currently sending."}

    sid = attendee["student_id"]
    settings = get_all_settings()
    recipient = (attendee.get("email") or "").strip()
    if not is_valid_email(recipient):
        err = f"Invalid email address: '{recipient or 'empty'}'"
        update_certificate_delivery_status(sid, "FAILED", error=err)
        return {"status": "error", "message": err}

    pdf_path = os.path.join(CERT_OUTPUT_DIR, f"{safe_filename_part(sid)}.pdf")
    try:
        pdf_bytes = generate_certificate_pdf(attendee, settings=settings, output_path=pdf_path)
    except Exception as e:
        err = f"PDF generation failed: {e}"
        update_certificate_delivery_status(sid, "FAILED", error=err)
        return {"status": "error", "message": err}

    try:
        msg = _build_email_message(attendee, settings, pdf_bytes)
        with _open_gmail_smtp(settings) as server:
            server.send_message(msg)
        update_certificate_delivery_status(sid, "SENT", file_path=pdf_path)
        return {"status": "success", "message": f"Certificate emailed to {recipient}."}
    except smtplib.SMTPAuthenticationError:
        err = "Gmail authentication failed. Check your Gmail address and 16-char App Password."
        update_certificate_delivery_status(sid, "FAILED", file_path=pdf_path, error=err)
        return {"status": "error", "message": err}
    except Exception as e:
        err = f"SMTP error: {e}"
        update_certificate_delivery_status(sid, "FAILED", file_path=pdf_path, error=err)
        return {"status": "error", "message": err}


def send_test_certificate_email(to_email: str, sample_name: str = "DELA CRUZ, JUAN R.") -> Dict[str, Any]:
    """Generates a sample certificate PDF and sends a test email via Gmail SMTP."""
    target = (to_email or "").strip()
    if not is_valid_email(target):
        return {"status": "error", "message": "Please provide a valid recipient email address for the test."}

    settings = get_all_settings()
    sample_attendee = {
        "student_id": "SAMPLE-2026-001",
        "full_name": sample_name.strip() or "DELA CRUZ, JUAN R.",
        "email": target,
        "level": "COLLEGE",
        "college_year": "3rd Year",
        "course": "BSCS",
        "section": "CS31A",
    }
    try:
        pdf_bytes = generate_certificate_pdf(sample_attendee, settings=settings)
        msg = _build_email_message(sample_attendee, settings, pdf_bytes)
        msg.replace_header("Subject", f"[TEST] {msg['Subject']}")
        with _open_gmail_smtp(settings) as server:
            server.send_message(msg)
        return {"status": "success", "message": f"Test certificate emailed to {target}."}
    except smtplib.SMTPAuthenticationError:
        return {
            "status": "error",
            "message": "Gmail authentication failed. Verify 2-Step Verification is ON and use a 16-char App Password.",
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to send test email: {e}"}


def _batch_worker(student_ids: List[str], staged_only: bool = False) -> None:
    """Background worker that reuses a single Gmail SMTP session across the batch."""
    settings = get_all_settings()
    server: Optional[smtplib.SMTP] = None

    try:
        try:
            server = _open_gmail_smtp(settings)
        except smtplib.SMTPAuthenticationError:
            with _BATCH_LOCK:
                _BATCH_STATE["last_error"] = "Gmail authentication failed. Check your Gmail address and App Password."
                _BATCH_STATE["running"] = False
                _BATCH_STATE["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return
        except Exception as e:
            with _BATCH_LOCK:
                _BATCH_STATE["last_error"] = f"Could not connect to Gmail SMTP: {e}"
                _BATCH_STATE["running"] = False
                _BATCH_STATE["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return

        for idx, raw_sid in enumerate(student_ids):
            attendee = claim_certificate_for_sending(raw_sid, force=False, require_staged=staged_only)
            if not attendee:
                with _BATCH_LOCK:
                    _BATCH_STATE["skipped"] += 1
                    _BATCH_STATE["processed"] += 1
                continue

            sid = attendee["student_id"]
            recipient = (attendee.get("email") or "").strip()
            if not is_valid_email(recipient):
                update_certificate_delivery_status(sid, "FAILED", error=f"Invalid email address: '{recipient or 'empty'}'")
                with _BATCH_LOCK:
                    _BATCH_STATE["failed"] += 1
                    _BATCH_STATE["processed"] += 1
                continue

            pdf_path = os.path.join(CERT_OUTPUT_DIR, f"{safe_filename_part(sid)}.pdf")
            try:
                pdf_bytes = generate_certificate_pdf(attendee, settings=settings, output_path=pdf_path)
                msg = _build_email_message(attendee, settings, pdf_bytes)
            except Exception as e:
                update_certificate_delivery_status(sid, "FAILED", error=f"PDF error: {e}")
                with _BATCH_LOCK:
                    _BATCH_STATE["failed"] += 1
                    _BATCH_STATE["processed"] += 1
                continue

            # Send with 1 automatic reconnect on transient connection drop
            sent_ok = False
            last_err = ""
            for attempt in range(2):
                try:
                    if server is None:
                        server = _open_gmail_smtp(settings)
                    server.send_message(msg)
                    sent_ok = True
                    break
                except smtplib.SMTPAuthenticationError:
                    last_err = "Gmail authentication failed mid-batch. Aborting remaining sends."
                    update_certificate_delivery_status(sid, "FAILED", file_path=pdf_path, error=last_err)
                    with _BATCH_LOCK:
                        _BATCH_STATE["failed"] += 1
                        _BATCH_STATE["processed"] += 1
                        _BATCH_STATE["last_error"] = last_err
                    return
                except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as e:
                    last_err = str(e)
                    try:
                        if server is not None:
                            server.quit()
                    except Exception:
                        pass
                    server = None
                    if attempt == 0:
                        time.sleep(0.5)
                except Exception as e:
                    last_err = str(e)
                    break

            if sent_ok:
                update_certificate_delivery_status(sid, "SENT", file_path=pdf_path)
                with _BATCH_LOCK:
                    _BATCH_STATE["sent"] += 1
                    _BATCH_STATE["processed"] += 1
            else:
                update_certificate_delivery_status(sid, "FAILED", file_path=pdf_path, error=last_err or "SMTP send failed")
                with _BATCH_LOCK:
                    _BATCH_STATE["failed"] += 1
                    _BATCH_STATE["processed"] += 1
                    _BATCH_STATE["last_error"] = last_err

            # Gentle pacing between emails to avoid Gmail rate-limit spikes
            if idx < len(student_ids) - 1:
                time.sleep(0.35)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
        with _BATCH_LOCK:
            _BATCH_STATE["running"] = False
            _BATCH_STATE["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def start_batch_send(include_failed: bool = True, staged_only: bool = False) -> Dict[str, Any]:
    """Starts a background batch email job for pending eligible (or stashed) attendees."""
    settings = get_all_settings()
    if not (settings.get("smtp_user") or "").strip() or not (settings.get("smtp_app_password") or "").strip():
        return {"status": "error", "message": "Configure Gmail address and App Password first."}

    with _BATCH_LOCK:
        if _BATCH_STATE["running"]:
            return {"status": "busy", "message": "A batch email job is already running.", "batch": dict(_BATCH_STATE)}

        pending_ids = get_pending_certificate_student_ids(include_failed=include_failed, staged_only=staged_only)
        if not pending_ids:
            msg = "No stashed certificates ready to send." if staged_only else "No pending eligible attendees to email."
            return {"status": "empty", "message": msg}

        _BATCH_STATE.update({
            "running": True,
            "total": len(pending_ids),
            "processed": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "last_error": "",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
        })

    t = threading.Thread(target=_batch_worker, args=(pending_ids, staged_only), daemon=True)
    t.start()
    return {"status": "started", "total": len(pending_ids), "message": f"Batch sending started for {len(pending_ids)} attendee(s)."}


def get_batch_status() -> Dict[str, Any]:
    """Returns live batch progress plus overall certificate delivery summary counts."""
    with _BATCH_LOCK:
        state = dict(_BATCH_STATE)
    state["summary"] = get_certificate_delivery_summary()
    return state


# ==============================================================================
# QUALITY CHECK (/quality) — TROLL NAME, LONG NAME & NEAR-MISS DETECTOR
# ==============================================================================

TROLL_WORDS = {
    "test", "testing", "troll", "asdf", "qwerty", "zxcv", "unknown", "anonymous",
    "anon", "none", "null", "sample", "dummy", "fake", "batman", "goku", "naruto",
    "skibidi", "sigma", "bruh", "lol", "lmao", "admin", "user", "student", "guest",
    "nobody", "name", "firstname", "lastname", "hehe", "haha", "huhu", "uwu", "owo",
}
TYPO_EMAIL_DOMAINS = {
    "gmial.com", "gmal.com", "gmai.com", "gmil.com", "gmail.co", "gmail.con",
    "yaho.com", "yahoo.co", "yahoo.con", "hotmal.com", "outlok.com",
}


def _compute_fitted_font_pt(text: str, settings: Dict[str, str]) -> float:
    """Computes the exact ReportLab font size (pt) that fits within 78% of page width."""
    base_size = _clamp_float(settings.get("cert_name_font_size"), 38.0, 14.0, 96.0)
    font_name = ALLOWED_FONTS.get(settings.get("cert_name_font", "Helvetica-Bold"), "Helvetica-Bold")
    use_upper = settings.get("cert_name_uppercase", "1") == "1"
    display = _winansi_safe((text or "").strip().upper() if use_upper else (text or "").strip())

    # Default A4 landscape width is 841.89pt; 78% is ~656.67pt
    max_width_pt = 841.89 * 0.78
    size = base_size
    while size > 11.0 and pdfmetrics.stringWidth(display, font_name, size) > max_width_pt:
        size -= 1.0
    return size


def analyze_attendee_quality(attendee: Dict[str, Any], settings: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Inspects an attendee's name and email for troll entries, formatting anomalies,
    long-name font shrinkage, and email typos before certificate delivery.
    """
    cfg = settings if settings is not None else get_all_settings()
    full_name = (attendee.get("full_name") or "").strip()
    first_name = (attendee.get("first_name") or "").strip()
    last_name = (attendee.get("last_name") or "").strip()
    email = (attendee.get("email") or "").strip().lower()

    flags: List[Dict[str, str]] = []
    fitted_pt = _compute_fitted_font_pt(full_name, cfg)

    # 1. Troll / junk name checks
    if len(full_name) < 4 or len(first_name) < 2 or len(last_name) < 2:
        flags.append({"level": "critical", "code": "short_name", "msg": "Suspiciously short name (< 2 chars in first/last)"})

    if re.search(r"(.)\1{3,}", full_name.lower()):
        flags.append({"level": "critical", "code": "repeated_chars", "msg": "Repeated characters detected (possible troll/spam)"})

    if re.search(r"\d", full_name):
        flags.append({"level": "critical", "code": "digits_in_name", "msg": "Contains numbers in name"})

    if re.search(r"[^A-Za-zÀ-ÿÑñ\s.,'-]", full_name):
        flags.append({"level": "warning", "code": "unusual_chars", "msg": "Contains unusual symbols"})

    tokens = set(re.findall(r"[a-z]+", full_name.lower()))
    matched_trolls = sorted(tokens & TROLL_WORDS)
    if matched_trolls:
        flags.append({"level": "critical", "code": "troll_word", "msg": f"Flagged keyword in name: {', '.join(matched_trolls)}"})

    # 2. Long name check
    if len(full_name) > 42 or fitted_pt < 24.0:
        flags.append({
            "level": "warning",
            "code": "long_name",
            "msg": f"Long name ({len(full_name)} chars, auto-fit to {int(fitted_pt)}pt) — preview PDF"
        })

    # 3. Email checks
    if not is_valid_email(email):
        flags.append({"level": "critical", "code": "invalid_email", "msg": f"Invalid email format: '{email or 'empty'}'"})
    elif "@" in email:
        domain = email.split("@", 1)[1]
        if domain in TYPO_EMAIL_DOMAINS:
            flags.append({"level": "warning", "code": "email_typo", "msg": f"Possible domain typo: @{domain}"})

    severity = "clean"
    if any(f["level"] == "critical" for f in flags):
        severity = "critical"
    elif flags:
        severity = "warning"

    return {
        "flags": flags,
        "severity": severity,
        "name_length": len(full_name),
        "fitted_font_pt": fitted_pt,
    }


def get_quality_dashboard_data(grace_minutes: float = 10.0) -> Dict[str, Any]:
    """
    Returns enriched eligible attendees for /quality review & stashing,
    plus near-miss attendees (e.g. 2 mins short of min_duration) that a human can rescue.
    """
    settings = get_all_settings()
    min_dur = float(settings.get("min_duration_minutes", "150") or "150")
    grace = max(0.0, min(120.0, float(grace_minutes)))
    attendees = get_all_attendees()

    eligible: List[Dict[str, Any]] = []
    near_miss: List[Dict[str, Any]] = []

    for a in attendees:
        m_dur = float(a.get("morning_duration_min") or 0) if a.get("morning_out") else 0.0
        a_dur = float(a.get("afternoon_duration_min") or 0) if a.get("afternoon_out") else 0.0
        total_dur = round(m_dur + a_dur, 1)
        has_timeout = bool(a.get("morning_out") or a.get("afternoon_out"))
        eval_sub = (a.get("eval_status") or "PENDING").upper() == "SUBMITTED"

        if a.get("is_eligible"):
            q = analyze_attendee_quality(a, settings)
            eligible.append({
                "student_id": a["student_id"],
                "last_name": a.get("last_name") or "",
                "first_name": a.get("first_name") or "",
                "middle_name": a.get("middle_name") or "N/A",
                "full_name": a["full_name"],
                "email": a["email"],
                "section": a["section"],
                "level": a["level"],
                "duration_min": total_dur,
                "eval_status": a.get("eval_status") or "PENDING",
                "cert_status": a.get("cert_status") or "APPROVED",
                "cert_quality_status": a.get("cert_quality_status") or "PENDING_REVIEW",
                "cert_email_status": a.get("cert_email_status") or "NOT_SENT",
                "cert_emailed_at": a.get("cert_emailed_at") or "",
                "cert_email_error": a.get("cert_email_error") or "",
                "quality": q,
            })
        else:
            # Near-miss candidate: timed out within grace window of min_dur
            shortfall = round(max(0.0, min_dur - total_dur), 1)
            if has_timeout and total_dur >= max(1.0, min_dur - grace):
                reasons = []
                if shortfall > 0:
                    reasons.append(f"{shortfall} min short ({total_dur}/{int(min_dur)} min)")
                if not eval_sub:
                    reasons.append(f"Eval: {a.get('eval_status') or 'PENDING'}")
                near_miss.append({
                    "student_id": a["student_id"],
                    "full_name": a["full_name"],
                    "email": a["email"],
                    "section": a["section"],
                    "duration_min": total_dur,
                    "shortfall_min": shortfall,
                    "eval_status": a.get("eval_status") or "PENDING",
                    "reason": " | ".join(reasons) if reasons else "Near eligibility threshold",
                })

    summary = get_certificate_delivery_summary()
    summary["near_miss_count"] = len(near_miss)
    summary["min_duration_minutes"] = min_dur
    summary["grace_minutes"] = grace
    return {
        "eligible": eligible,
        "near_miss": near_miss,
        "summary": summary,
        "batch": get_batch_status(),
    }
