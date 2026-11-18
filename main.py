import os
import io
import re
import csv
import sys
import json
import hmac
import socket
import hashlib
import secrets
import subprocess
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, HTTPException, Form, Depends, BackgroundTasks, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any
import user_agents

from database import (
    init_db, get_setting, set_setting, get_all_settings,
    register_time_in, register_time_out, get_statistics, get_all_attendees,
    get_sections_list, admin_manual_add_attendee, admin_edit_attendee, admin_delete_attendee, admin_clear_all_data,
    open_or_resume_evaluation, autosave_evaluation_draft, release_evaluation_lock,
    submit_evaluation, finalize_and_tally_incomplete_evaluations,
    get_passes_live_status, get_all_evaluations_for_export, recompute_all_attendees_eligibility,
    format_time_str, get_db,
    get_eval_quiz_schema, save_eval_quiz_schema, reset_eval_quiz_schema,
    get_pending_certificate_student_ids, get_certificate_delivery_summary,
    set_certificate_quality_status, bulk_stage_clean_certificates,
    rescue_near_miss_to_eligible, quick_fix_attendee_identity
)
from certificate_service import (
    CERT_TEMPLATE_FILE,
    CERT_OUTPUT_DIR,
    generate_certificate_pdf,
    send_single_certificate,
    send_test_certificate_email,
    start_batch_send,
    get_batch_status,
    get_quality_dashboard_data,
)
import qrcode

app = FastAPI(title="ACLC Seminar Attendance & Evaluation System")
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def static_cache_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") and response.status_code == 200:
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
    return response

# Custom human-friendly validation error handler
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    msg = "Validation error"
    if errors:
        err = errors[0]
        custom_msg = err.get("msg", "")
        loc = err.get("loc", [])
        field = loc[-1] if loc else "field"
        err_type = err.get("type", "")
        
        if "Value error," in custom_msg:
            msg = custom_msg.replace("Value error,", "").strip()
        elif "Input should be less than or equal to 120" in custom_msg or (field == "age" and "less_than" in err_type):
            msg = "Age cannot exceed 120 years."
        elif "Input should be greater than or equal to 10" in custom_msg or (field == "age" and "greater_than" in err_type):
            msg = "Age must be at least 10 years old."
        elif field == "age":
            msg = "Age must be a valid number between 10 and 120."
        elif field == "email":
            msg = "Please provide a valid active email address."
        elif field == "student_id":
            msg = "Student ID Number is required."
        elif field in ("last_name", "first_name"):
            msg = f"{str(field).replace('_', ' ').title()} is required."
        else:
            msg = custom_msg or f"Invalid input for {field}."
    return JSONResponse(
        status_code=422,
        content={"status": "error", "message": msg}
    )

# Mount offline assets using cross-platform absolute paths (Windows & Linux safe)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Initialize database (enforces eval_open='0' at server start and 150-min default duration)
init_db()

# Security & Lockout State (Persistent across server reloads)
def get_auth_secret() -> str:
    secret = get_setting("admin_auth_secret")
    if not secret:
        secret = secrets.token_hex(24)
        set_setting("admin_auth_secret", secret)
    return secret

AUTH_SECRET = get_auth_secret()
FAILED_ATTEMPTS = 0
LOCKOUT_UNTIL: Optional[datetime] = None
MAX_ATTEMPTS = 4
LOCKOUT_MINUTES = 5

# Digital Attendance Pass HMAC Token Security
def compute_pass_token(student_id: str) -> str:
    salt = get_setting("pass_salt", "ACLC-GAD-2026-SECURITY")
    return hmac.new(salt.encode(), student_id.strip().upper().encode(), hashlib.sha256).hexdigest()[:10].upper()

def verify_pass_token(student_id: str, token: str) -> bool:
    expected = compute_pass_token(student_id)
    return hmac.compare_digest(expected, token.strip().upper())

# Models with rigorous edge-case validation
class BatchTimeOutPayload(BaseModel):
    student_ids: List[str]

class PassesStatusPayload(BaseModel):
    student_ids: List[str]

class EvaluationOpenPayload(BaseModel):
    student_id: str
    session_id: str
    token: Optional[str] = None
    force: Optional[bool] = False

class EvaluationAutosavePayload(BaseModel):
    student_id: str
    session_id: str
    token: Optional[str] = None
    draft: Dict[str, Any]

class EvaluationReleasePayload(BaseModel):
    student_id: str
    session_id: str
    draft: Optional[Dict[str, Any]] = None

class EvaluationSubmitPayload(BaseModel):
    student_id: str
    session_id: str
    token: Optional[str] = None
    answers: Dict[str, Any]

class EvalOpenTogglePayload(BaseModel):
    eval_open: bool

class TimeInPayload(BaseModel):
    student_id: Optional[str] = ""
    id_type: Optional[str] = "Student ID Number"
    last_name: str
    first_name: str
    middle_name: Optional[str] = "N/A"
    sex: str
    age: int = Field(..., ge=10, le=120)
    email: str
    level: str
    section: str
    shs_grade: Optional[str] = None
    strand: Optional[str] = None
    college_year: Optional[str] = None
    course: Optional[str] = None

    @field_validator("student_id")
    def validate_student_id(cls, v):
        if not v:
            return ""
        v = v.strip().upper()
        if not v:
            return ""
        if len(v) < 2 or len(v) > 50:
            raise ValueError("ID Number must be between 2 and 50 characters if provided.")
        return re.sub(r"[<>]", "", v)

    @field_validator("id_type")
    def validate_id_type(cls, v):
        allowed = ("Student ID Number", "USN", "ID Number")
        if not v:
            return "Student ID Number"
        v = v.strip()
        for opt in allowed:
            if v.lower() == opt.lower():
                return opt
        return "Student ID Number"

    @field_validator("last_name")
    def validate_last_name(cls, v):
        v = v.strip()
        if not v or len(v) < 1 or len(v) > 100:
            raise ValueError("Last Name is required (up to 100 characters).")
        return re.sub(r"[<>]", "", v)

    @field_validator("first_name")
    def validate_first_name(cls, v):
        v = v.strip()
        if not v or len(v) < 1 or len(v) > 100:
            raise ValueError("First Name is required (up to 100 characters).")
        return re.sub(r"[<>]", "", v)

    @field_validator("middle_name")
    def validate_middle_name(cls, v):
        if not v:
            return "N/A"
        v = v.strip()
        if not v or v.upper() in ("N/A", "NA", "NONE", "-"):
            return "N/A"
        return re.sub(r"[<>]", "", v)[:100]

    @field_validator("sex")
    def validate_sex(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Sex / Gender identity is required.")
        return re.sub(r"[<>]", "", v)[:50]

    @field_validator("email")
    def validate_email(cls, v):
        v = v.strip().lower()
        if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", v):
            raise ValueError("Please provide a valid active email address (e.g. name@domain.com).")
        return v[:120]

    @field_validator("level")
    def validate_level(cls, v):
        v = v.strip().upper()
        if v not in ("SHS", "COLLEGE", "NON_STUDENT"):
            raise ValueError("Academic Category must be SHS, COLLEGE, or NON_STUDENT.")
        return v

    @field_validator("section")
    def validate_section(cls, v):
        v = v.strip().upper()
        if not v:
            raise ValueError("Section or Role / Designation is required.")
        return re.sub(r"[<>]", "", v)[:100]

class TimeOutPayload(BaseModel):
    student_id: str

    @field_validator("student_id")
    def validate_student_id(cls, v):
        v = v.strip().upper()
        if not v or len(v) < 2:
            raise ValueError("Please enter a valid Student ID Number.")
        return re.sub(r"[<>]", "", v)[:80]

class PhasePayload(BaseModel):
    phase: str

class QRModePayload(BaseModel):
    mode: str

class DurationPayload(BaseModel):
    minutes: int

class KioskConfigPayload(BaseModel):
    event_title: str
    active_session_label: Optional[str] = "Morning Session Time-In"
    current_phase: Optional[str] = None
    event_date: str
    venue: str
    kiosk_announcement: Optional[str] = ""
    terms_and_conditions: Optional[str] = ""

class ProjectorConfigPayload(BaseModel):
    proj_show_form_qr: bool = True
    proj_show_wifi_qr: bool = True
    wifi_ssid: Optional[str] = "HIV Awareness Attendance"
    wifi_password: Optional[str] = ""
    kiosk_url: Optional[str] = ""
    proj_announcement: Optional[str] = ""

class ManualAddPayload(BaseModel):
    student_id: str
    last_name: str
    first_name: str
    middle_name: Optional[str] = "N/A"
    sex: str
    age: int = Field(..., ge=10, le=120)
    email: str
    level: str
    section: str
    shs_grade: Optional[str] = None
    strand: Optional[str] = None
    college_year: Optional[str] = None
    course: Optional[str] = None
    phase: Optional[str] = "MORNING_IN"
    time_in: Optional[str] = None

class EditAttendeePayload(BaseModel):
    student_id: str
    last_name: str
    first_name: str
    middle_name: Optional[str] = "N/A"
    sex: str
    age: int = Field(..., ge=10, le=120)
    email: str
    level: str
    section: str
    morning_in: Optional[str] = None
    morning_out: Optional[str] = None
    afternoon_in: Optional[str] = None
    afternoon_out: Optional[str] = None
    eval_status: Optional[str] = None
    is_eligible: Optional[int] = 0

class DeleteAttendeePayload(BaseModel):
    student_id: str

# Helpers
_CACHED_LOCAL_IP: Optional[str] = None
_CACHED_LOCAL_IP_TS: float = 0.0

def get_local_ip() -> str:
    """Detects the host laptop's active LAN/Wi-Fi IPv4 address on both Windows and Linux (100% offline-safe, cached 30s)."""
    global _CACHED_LOCAL_IP, _CACHED_LOCAL_IP_TS
    now_ts = datetime.now().timestamp()
    if _CACHED_LOCAL_IP and (now_ts - _CACHED_LOCAL_IP_TS) < 30.0:
        return _CACHED_LOCAL_IP

    detected = "192.168.1.100"
    for target in (("192.168.1.1", 80), ("10.255.255.255", 1), ("172.16.0.1", 80)):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.1)
            s.connect(target)
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                detected = ip
                break
        except Exception:
            pass
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
    if detected == "192.168.1.100":
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                    detected = ip
                    break
        except Exception:
            pass
    _CACHED_LOCAL_IP = detected
    _CACHED_LOCAL_IP_TS = now_ts
    return detected

def get_host_wifi_ssid() -> str:
    """Detects active Wi-Fi SSID on host machine (cross-platform, fail-safe)."""
    ssid = ""
    if sys.platform.startswith("linux"):
        try:
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                stderr=subprocess.DEVNULL,
                timeout=1.5
            ).decode("utf-8", errors="ignore")
            for line in out.splitlines():
                if line.startswith("yes:"):
                    ssid = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass
        if not ssid:
            try:
                out = subprocess.check_output(
                    ["iwgetid", "-r"],
                    stderr=subprocess.DEVNULL,
                    timeout=1.0
                ).decode("utf-8", errors="ignore").strip()
                if out:
                    ssid = out
            except Exception:
                pass
    elif sys.platform.startswith("win"):
        try:
            out = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                stderr=subprocess.DEVNULL,
                timeout=1.5
            ).decode("utf-8", errors="ignore")
            for line in out.splitlines():
                if "SSID" in line and "BSSID" not in line:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        ssid = parts[1].strip()
                        break
        except Exception:
            pass
    return ssid

def get_network_status_info() -> dict:
    target_ssid = get_setting("wifi_ssid", "HIV Awareness Attendance").strip()
    active_ssid = get_host_wifi_ssid().strip()
    lan_ip = get_local_ip()
    port = os.environ.get("PORT", "8080")
    if active_ssid:
        # When on Wi-Fi, only mark as event network if the active SSID matches the Wi-Fi QR SSID
        is_event_wifi = (active_ssid == target_ssid)
    else:
        # Wired Ethernet fallback (no Wi-Fi SSID on host)
        is_event_wifi = lan_ip.startswith("192.168.0.") or lan_ip.startswith("192.168.1.")
    return {
        "active_ssid": active_ssid or "Ethernet / Wired",
        "target_ssid": target_ssid,
        "is_event_wifi": is_event_wifi,
        "qr_mismatch": bool(active_ssid and active_ssid != target_ssid),
        "lan_ip": lan_ip,
        "port": port,
        "server_url": f"http://{lan_ip}:{port}/"
    }

def get_base_url(request: Request) -> str:
    custom = get_setting("kiosk_url", "").strip()
    if custom:
        return custom.rstrip("/")
    host_header = (request.headers.get("host") or "").strip()
    if ":" in host_header and not host_header.endswith("]"):
        host_part, port = host_header.rsplit(":", 1)
    else:
        host_part = host_header
        port = os.environ.get("PORT", "8080")
    local_hosts = {"", "localhost", "127.0.0.1", "0.0.0.0", "[::1]", "testserver"}
    if host_part.lower() not in local_hosts:
        return f"http://{host_part}:{port}"
    return f"http://{get_local_ip()}:{port}"

def get_client_device_info(request: Request) -> tuple:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "127.0.0.1"

    ua_str = request.headers.get("user-agent", "")
    try:
        ua = user_agents.parse(ua_str)
        device_brand = ua.device.brand or ""
        device_model = ua.device.model or ""
        device_name = f"{device_brand} {device_model}".strip()
        if not device_name:
            if ua.is_mobile:
                device_name = "Mobile Device"
            elif ua.is_tablet:
                device_name = "Tablet"
            else:
                device_name = "PC / Desktop"

        os_info = f"{ua.os.family} {ua.os.version_string}".strip()
        summary = f"{device_name} ({os_info})"
    except Exception:
        summary = "Unknown Device"

    return ip, summary

def check_admin_auth(request: Request) -> bool:
    session_cookie = request.cookies.get("aclc_admin_session")
    if not session_cookie:
        return False
    return hmac.compare_digest(session_cookie, get_auth_secret())

# Serve official ACLC favicon
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    fav_path = os.path.join(os.path.dirname(__file__), "static", "img", "favicon.ico")
    if os.path.exists(fav_path):
        return FileResponse(fav_path, media_type="image/x-icon")
    return Response(status_code=204)

# Captive Portal Probe Endpoints (Auto-opens browser on Android, iOS, Windows)
@app.get("/generate_204", include_in_schema=False)
@app.get("/gen_204", include_in_schema=False)
@app.get("/hotspot-detect.html", include_in_schema=False)
@app.get("/canonical.html", include_in_schema=False)
@app.get("/ncsi.txt", include_in_schema=False)
@app.get("/connecttest.txt", include_in_schema=False)
def captive_portal_probe():
    return RedirectResponse(url="/captive", status_code=302)

@app.get("/captive", include_in_schema=False)
def captive_portal_page(request: Request):
    scheme = request.url.scheme or "http"
    host = request.headers.get("host") or request.url.netloc or f"{get_local_ip()}:8080"
    kiosk_url = f"{scheme}://{host}/"
    return templates.TemplateResponse(
        request=request,
        name="captive.html",
        context={
            "event_title": get_setting("event_title", "HIV Awareness Seminar"),
            "wifi_ssid": get_setting("wifi_ssid", "ACLC_Seminar_WiFi"),
            "kiosk_url": kiosk_url,
        }
    )

def get_session_and_status(phase: str) -> tuple[str, str]:
    """Returns standardized (session, status) pair e.g. ('Morning', 'Time In')"""
    if phase == "MORNING_IN":
        return "Morning", "Time In"
    elif phase == "MORNING_OUT":
        return "Morning", "Time Out"
    elif phase == "AFTERNOON_IN":
        return "Afternoon", "Time In"
    elif phase == "AFTERNOON_OUT":
        return "Afternoon", "Time Out"
    else:
        return "Morning", "Closed"

def format_duration_hm(minutes_val: str | int | float) -> str:
    try:
        m = int(float(minutes_val))
    except Exception:
        m = 150
    hrs = m // 60
    rem = m % 60
    if hrs > 0 and rem > 0:
        return f"{hrs} hr {rem} min ({m} mins)"
    elif hrs > 0:
        return f"{hrs} hr ({m} mins)"
    return f"{m} mins"

# Web Pages
@app.get("/api/kiosk-live")
def kiosk_live(request: Request):
    base_url = get_base_url(request)
    phase = get_setting("current_phase", "MORNING_IN")
    session, status = get_session_and_status(phase)
    min_dur = get_setting("min_duration_minutes", "150")
    eval_open = get_setting("eval_open", "0") == "1"
    return {
        "event_title": get_setting("event_title", "HIV Awareness Seminar"),
        "active_session_label": f"{session} {status}",
        "phase": phase,
        "session": session,
        "status": status,
        "eval_open": eval_open,
        "eval_quiz_version": int(float(get_setting("eval_quiz_version", "1") or "1")),
        "min_duration": int(float(min_dur)),
        "min_duration_label": format_duration_hm(min_dur),
        "kiosk_announcement": get_setting("kiosk_announcement", ""),
        "venue": get_setting("venue", "ACLC College of Mandaue"),
        "event_date": get_setting("event_date", ""),
        "terms_and_conditions": get_setting("terms_and_conditions", ""),
        "wifi_ssid": get_setting("wifi_ssid", "HIV Awareness Attendance"),
        "wifi_password": get_setting("wifi_password", ""),
        "kiosk_url": get_setting("kiosk_url", ""),
        "base_url": base_url,
        "proj_show_form_qr": get_setting("proj_show_form_qr", "1") == "1",
        "proj_show_wifi_qr": get_setting("proj_show_wifi_qr", "1") == "1",
        "proj_announcement": get_setting("proj_announcement", ""),
        "network": get_network_status_info()
    }

@app.get("/")
def student_page(request: Request):
    phase = get_setting("current_phase", "MORNING_IN")
    session, status = get_session_and_status(phase)
    event_title = get_setting("event_title", "HIV Awareness Seminar")
    venue = get_setting("venue", "ACLC College of Mandaue")
    kiosk_announcement = get_setting("kiosk_announcement", "")
    terms = get_setting("terms_and_conditions", "")
    min_dur = get_setting("min_duration_minutes", "150")
    eval_open = get_setting("eval_open", "0") == "1"
    return templates.TemplateResponse(
        request=request,
        name="student.html",
        context={
            "phase": phase,
            "session": session,
            "status": status,
            "eval_open": eval_open,
            "event_title": event_title,
            "active_session_label": f"{session} {status}",
            "venue": venue,
            "kiosk_announcement": kiosk_announcement,
            "terms_and_conditions": terms,
            "min_duration": int(float(min_dur)),
            "min_duration_label": format_duration_hm(min_dur),
            "eval_quiz_schema": get_eval_quiz_schema(),
            "eval_quiz_version": int(float(get_setting("eval_quiz_version", "1") or "1"))
        }
    )

@app.get("/timeout")
def timeout_redirect():
    return RedirectResponse(url="/", status_code=303)

@app.get("/try-evaluation")
def try_evaluation_redirect():
    """Direct preview shortcut to open and test-drive the Seminar Evaluation Form quiz."""
    return RedirectResponse(url="/?try_quiz=1", status_code=303)

# Admin Authentication Gate
@app.get("/admin/login")
def admin_login_page(request: Request, next: Optional[str] = None):
    global FAILED_ATTEMPTS, LOCKOUT_UNTIL
    
    is_locked = False
    lockout_remaining = 0
    now = datetime.now()

    if LOCKOUT_UNTIL:
        if now < LOCKOUT_UNTIL:
            is_locked = True
            lockout_remaining = int((LOCKOUT_UNTIL - now).total_seconds())
        else:
            FAILED_ATTEMPTS = 0
            LOCKOUT_UNTIL = None

    return templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={
            "is_locked": is_locked,
            "lockout_remaining": lockout_remaining,
            "error_msg": None,
            "next_url": next or "/admin"
        }
    )

@app.post("/admin/login")
def admin_login_submit(request: Request, password: str = Form(...), next_url: Optional[str] = Form(None)):
    global FAILED_ATTEMPTS, LOCKOUT_UNTIL
    
    now = datetime.now()
    if LOCKOUT_UNTIL and now < LOCKOUT_UNTIL:
        remaining = int((LOCKOUT_UNTIL - now).total_seconds())
        return templates.TemplateResponse(
            request=request,
            name="admin_login.html",
            context={
                "is_locked": True,
                "lockout_remaining": remaining,
                "error_msg": "System locked. Please wait for the countdown.",
                "next_url": next_url or "/admin"
            }
        )

    correct_password = get_setting("admin_password", "nevergrowold")
    if password == correct_password:
        FAILED_ATTEMPTS = 0
        LOCKOUT_UNTIL = None
        target = next_url if (next_url and (next_url.startswith("/admin") or next_url.startswith("/scanner"))) else "/admin"
        response = RedirectResponse(url=target, status_code=303)
        response.set_cookie(key="aclc_admin_session", value=get_auth_secret(), httponly=True)
        return response
    else:
        FAILED_ATTEMPTS += 1
        if FAILED_ATTEMPTS >= MAX_ATTEMPTS:
            LOCKOUT_UNTIL = now + timedelta(minutes=LOCKOUT_MINUTES)
            return templates.TemplateResponse(
                request=request,
                name="admin_login.html",
                context={
                    "is_locked": True,
                    "lockout_remaining": LOCKOUT_MINUTES * 60,
                    "error_msg": f"Maximum 4 attempts exceeded. Locked for {LOCKOUT_MINUTES} minutes.",
                    "next_url": next_url or "/admin"
                }
            )
        
        remaining_tries = MAX_ATTEMPTS - FAILED_ATTEMPTS
        return templates.TemplateResponse(
            request=request,
            name="admin_login.html",
            context={
                "is_locked": False,
                "lockout_remaining": 0,
                "error_msg": f"Invalid password. {remaining_tries} attempts remaining before temporary lockout.",
                "next_url": next_url or "/admin"
            }
        )

@app.get("/admin/logout")
def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(key="aclc_admin_session")
    return response

@app.get("/admin")
def admin_page(request: Request, search: Optional[str] = None, level: Optional[str] = None):
    if not check_admin_auth(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    settings = get_all_settings()
    stats = get_statistics()
    attendees = get_all_attendees(search=search, filter_level=level)
    phase = settings.get("current_phase", "MORNING_IN")
    session, status = get_session_and_status(phase)
    min_dur = settings.get("min_duration_minutes", "150")
    eval_open = settings.get("eval_open", "0") == "1"
    
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "event_title": settings.get("event_title", "HIV Awareness Seminar"),
            "active_session_label": f"{session} {status}",
            "phase": phase,
            "session": session,
            "status": status,
            "eval_open": eval_open,
            "event_date": settings.get("event_date", datetime.now().strftime("%B %d, %Y")),
            "venue": settings.get("venue", "ACLC College of Mandaue"),
            "kiosk_announcement": settings.get("kiosk_announcement", ""),
            "terms_and_conditions": settings.get("terms_and_conditions", ""),
            "current_phase": phase,
            "qr_mode": settings.get("qr_mode", "STATIC"),
            "min_duration": min_dur,
            "min_duration_label": format_duration_hm(min_dur),
            "wifi_ssid": settings.get("wifi_ssid", "HIV Awareness Attendance"),
            "wifi_password": settings.get("wifi_password", ""),
            "kiosk_url": settings.get("kiosk_url", ""),
            "base_url": get_base_url(request),
            "proj_show_form_qr": settings.get("proj_show_form_qr", "1") == "1",
            "proj_show_wifi_qr": settings.get("proj_show_wifi_qr", "1") == "1",
            "proj_announcement": settings.get("proj_announcement", ""),
            "network": get_network_status_info(),
            "stats": stats,
            "attendees": attendees,
            "eval_quiz_schema": get_eval_quiz_schema(),
            "eval_quiz_version": int(float(get_setting("eval_quiz_version", "1") or "1"))
        }
    )

@app.get("/projector")
def projector_page(request: Request):
    phase = get_setting("current_phase", "MORNING_IN")
    session, status = get_session_and_status(phase)
    event_title = get_setting("event_title", "HIV Awareness Seminar")
    base_url = get_base_url(request)
    min_dur = get_setting("min_duration_minutes", "150")
    eval_open = get_setting("eval_open", "0") == "1"
    return templates.TemplateResponse(
        request=request,
        name="projector.html",
        context={
            "phase": phase,
            "session": session,
            "status": status,
            "eval_open": eval_open,
            "event_title": event_title,
            "active_session_label": f"{session} {status}",
            "base_url": base_url,
            "min_duration": min_dur,
            "min_duration_label": format_duration_hm(min_dur),
            "wifi_ssid": get_setting("wifi_ssid", "HIV Awareness Attendance"),
            "wifi_password": get_setting("wifi_password", ""),
            "kiosk_url": get_setting("kiosk_url", ""),
            "proj_show_form_qr": get_setting("proj_show_form_qr", "1") == "1",
            "proj_show_wifi_qr": get_setting("proj_show_wifi_qr", "1") == "1",
            "proj_announcement": get_setting("proj_announcement", "")
        }
    )

# QR Code Generation Endpoints
@app.get("/api/qr")
def generate_qr(request: Request):
    base_host = get_base_url(request)
    target_url = f"{base_host}/"
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(target_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="#0A3981", back_color="#FFFFFF")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")

@app.get("/api/qr/wifi")
def generate_wifi_qr():
    """Generates standard Wi-Fi quick-connect QR code"""
    ssid = get_setting("wifi_ssid", "HIV Awareness Attendance")
    pwd = get_setting("wifi_password", "")
    auth = "WPA" if pwd else "nopass"
    wifi_data = f"WIFI:S:{ssid};T:{auth};P:{pwd};;"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(wifi_data)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="#0A3981", back_color="#FFFFFF")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")

# Attendance & Scanner Pages (Protected: Scanner is inside Admin Operations)
@app.get("/scanner")
@app.get("/admin/scanner")
def officer_scanner_page(request: Request):
    if not check_admin_auth(request):
        return RedirectResponse(url="/admin/login?next=/admin/scanner", status_code=303)
    phase = get_setting("current_phase", "MORNING_OUT")
    session, status = get_session_and_status(phase)
    event_title = get_setting("event_title", "HIV Awareness Seminar")
    min_dur = get_setting("min_duration_minutes", "150")
    return templates.TemplateResponse(
        request=request,
        name="scanner.html",
        context={
            "phase": phase,
            "session": session,
            "status": status,
            "event_title": event_title,
            "min_duration": min_dur,
            "min_duration_label": format_duration_hm(min_dur),
            "active_session_label": f"{session} {status}",
            "is_admin": True
        }
    )

# Student Attendance QR Pass Generator (Salted HMAC-SHA256 Signed QR)
@app.get("/api/qr/student-pass")
def generate_student_pass_qr(student_id: str):
    sid = student_id.strip().upper()
    token = compute_pass_token(sid)
    qr_payload = f"ACLC:{sid}:{token}"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(qr_payload)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="#0A3981", back_color="#FFFFFF")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")

# Attendance APIs with Strict Mutual Exclusivity
@app.post("/api/attendance/in")
def attendance_time_in(payload: TimeInPayload, request: Request):
    phase = get_setting("current_phase", "MORNING_IN")
    if phase not in ("MORNING_IN", "AFTERNOON_IN"):
        return {"status": "error", "message": "Time In is closed."}
        
    ip_addr, device_info = get_client_device_info(request)
    result = register_time_in(payload.model_dump(), phase, ip_address=ip_addr, device_info=device_info)
    if result.get("status") == "error":
        return result
    
    # Return Salted HMAC-SHA256 token and signed QR payload
    sid = result.get("student_id") or (payload.student_id or "").strip().upper()
    token = compute_pass_token(sid)
    result["token"] = token
    result["qr_data"] = f"ACLC:{sid}:{token}"
    result["level"] = payload.level
    return result

@app.post("/api/attendance/out")
def attendance_time_out(payload: TimeOutPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized. Time Out must be recorded through the Scanner.")
        
    phase = get_setting("current_phase", "MORNING_OUT")
    if phase not in ("MORNING_OUT", "AFTERNOON_OUT"):
        return {"status": "error", "message": "Time Out is not open yet."}
        
    raw_id = payload.student_id.strip()
    # Verify Salted HMAC-SHA256 signature on QR scans (ACLC:ID:TOKEN)
    if raw_id.startswith("ACLC:"):
        parts = raw_id.split(":")
        if len(parts) != 3:
            return {"status": "error", "message": "Invalid QR pass format."}
        student_id = parts[1].strip().upper()
        provided_token = parts[2].strip().upper()
        if not verify_pass_token(student_id, provided_token):
            return {"status": "error", "message": "Invalid or forged QR pass signature."}
    else:
        student_id = raw_id.upper()
        
    ip_addr, _ = get_client_device_info(request)
    min_dur = float(get_setting("min_duration_minutes", "150"))
    result = register_time_out(student_id, phase, min_dur, ip_address=ip_addr)
    return result

@app.get("/api/admin/sections")
def fetch_sections(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"sections": get_sections_list()}

@app.post("/api/admin/batch-timeout")
def batch_timeout(payload: BatchTimeOutPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    phase = get_setting("current_phase", "MORNING_OUT")
    if phase not in ("MORNING_OUT", "AFTERNOON_OUT"):
        return {"status": "error", "message": f"Time-Out is closed. Active phase: {phase}"}
    min_dur = float(get_setting("min_duration_minutes", "150"))
    results = []
    for sid in payload.student_ids:
        clean_sid = sid.strip().upper()
        r = register_time_out(clean_sid, phase, min_dur, ip_address="OFFICER_BATCH")
        results.append(r)
    return {"status": "success", "processed": len(results), "details": results}

def _build_pass_dict(row) -> Dict[str, Any]:
    sid = row["student_id"]
    time_stamp = row["morning_in"] or row["afternoon_in"] or "Timed In"
    time_fmt = format_time_str(time_stamp) if time_stamp != "Timed In" else "Timed In"
    tok = compute_pass_token(sid)
    return {
        "student_id": sid,
        "student_name": row["full_name"],
        "section": row["section"],
        "level": row["level"],
        "time": time_stamp,
        "time_fmt": time_fmt,
        "token": tok,
        "qr_data": f"ACLC:{sid}:{tok}",
        "eval_status": row["eval_status"] or "PENDING",
        "attendance_eligible": bool(row["attendance_eligible"]),
        "is_eligible": bool(row["is_eligible"]),
        "cert_status": row["cert_status"] or "PENDING"
    }


# Verified Pass Lookup for recovering a lost pass or loading a classmate's pass
@app.get("/api/attendance/pass-lookup")
def pass_lookup(student_id: Optional[str] = None, q: Optional[str] = None, last_name: Optional[str] = None):
    raw_query = (q if q is not None else student_id) or ""
    query = raw_query.strip()
    ln_query = " ".join((last_name or "").strip().split()).upper()
    if not query or not ln_query:
        return {"status": "error", "message": "Please enter both your ID/Email and Last Name to load a pass."}

    conn = get_db()
    cursor = conn.cursor()
    upper_q = query.upper()

    # Exact Student ID or Email match + Last Name verification
    row = cursor.execute(
        """
        SELECT * FROM attendees
        WHERE (UPPER(TRIM(student_id)) = ? OR UPPER(TRIM(email)) = ?)
          AND UPPER(TRIM(last_name)) = ?
        LIMIT 1
        """,
        (upper_q, upper_q, ln_query)
    ).fetchone()
    conn.close()

    if not row:
        return {"status": "error", "message": "No matching attendee found. Check that both your ID/Email and Last Name match your Time In registration."}

    res = _build_pass_dict(row)
    res["status"] = "success"
    return res

@app.post("/api/attendance/passes-status")
def passes_live_status(payload: PassesStatusPayload):
    """Returns live evaluation, lock, and certificate eligibility states for passes saved on a phone."""
    return {
        "status": "success",
        "eval_open": get_setting("eval_open", "0") == "1",
        "passes": get_passes_live_status(payload.student_ids)
    }

# =====================================================================
# SEMINAR EVALUATION FORM ENDPOINTS (CONCURRENCY-SAFE & AUTO-SAVING)
# =====================================================================

@app.get("/api/evaluation/schema")
def api_get_eval_schema():
    """Returns the live evaluation quiz questions and current schema version."""
    return {
        "version": int(float(get_setting("eval_quiz_version", "1") or "1")),
        "schema": get_eval_quiz_schema()
    }


@app.post("/api/admin/evaluation/schema")
async def api_admin_save_eval_schema(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid quiz schema payload.")
    schema, new_ver = save_eval_quiz_schema(body)
    return {"status": "success", "version": new_ver, "schema": schema}


@app.post("/api/admin/evaluation/schema/reset")
def api_admin_reset_eval_schema(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    schema, new_ver = reset_eval_quiz_schema()
    return {"status": "success", "version": new_ver, "schema": schema}

@app.post("/api/evaluation/open")
def api_evaluation_open(payload: EvaluationOpenPayload, request: Request):
    sid = payload.student_id.strip().upper()
    if not sid.startswith("PREVIEW") and not verify_pass_token(sid, payload.token or ""):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Invalid pass token signature."})
    ip_addr, device_info = get_client_device_info(request)
    res = open_or_resume_evaluation(
        sid, payload.session_id,
        ip_address=ip_addr, device_info=device_info,
        force=bool(payload.force)
    )
    if res["status"] == "locked":
        return JSONResponse(status_code=409, content=res)
    return res

@app.post("/api/evaluation/autosave")
def api_evaluation_autosave(payload: EvaluationAutosavePayload, request: Request):
    sid = payload.student_id.strip().upper()
    if not sid.startswith("PREVIEW") and not verify_pass_token(sid, payload.token or ""):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Invalid pass token signature."})
    ip_addr, device_info = get_client_device_info(request)
    res = autosave_evaluation_draft(sid, payload.session_id, payload.draft, ip_address=ip_addr, device_info=device_info)
    if res["status"] == "locked":
        return JSONResponse(status_code=409, content=res)
    return res

@app.post("/api/evaluation/release")
async def api_evaluation_release(request: Request):
    """Supports both standard JSON fetch and navigator.sendBeacon on page unload/modal close."""
    try:
        raw_body = await request.body()
        data = json.loads(raw_body.decode("utf-8"))
        sid = str(data.get("student_id", "")).strip().upper()
        sess = str(data.get("session_id", "")).strip()
        tok = str(data.get("token", "")).strip()
        draft = data.get("draft")
        if not sid or not sess:
            return {"status": "error", "message": "Missing student_id or session_id."}
        if not sid.startswith("PREVIEW") and not verify_pass_token(sid, tok):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Invalid pass token signature."})
        return release_evaluation_lock(sid, sess, draft=draft)
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/evaluation/submit")
def api_evaluation_submit(payload: EvaluationSubmitPayload, request: Request):
    sid = payload.student_id.strip().upper()
    if not sid.startswith("PREVIEW") and not verify_pass_token(sid, payload.token or ""):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Invalid pass token signature."})
    ip_addr, device_info = get_client_device_info(request)
    res = submit_evaluation(sid, payload.session_id, payload.answers, ip_address=ip_addr, device_info=device_info)
    if res["status"] == "locked":
        return JSONResponse(status_code=409, content=res)
    return res

# Admin Manual Attendance & Modification APIs
@app.post("/api/admin/manual-add")
def manual_add_attendee(payload: ManualAddPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return admin_manual_add_attendee(payload.model_dump())

@app.post("/api/admin/edit-attendee")
def edit_attendee(payload: EditAttendeePayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return admin_edit_attendee(payload.student_id, payload.model_dump())

@app.post("/api/admin/delete-attendee")
def delete_attendee(payload: DeleteAttendeePayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return admin_delete_attendee(payload.student_id)

class ClearAllPayload(BaseModel):
    confirm_text: str = ""

@app.post("/api/admin/clear-all")
def clear_all_test_data(payload: ClearAllPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if (payload.confirm_text or "").strip().upper() != "CLEAR ALL":
        return JSONResponse(status_code=400, content={"status": "error", "message": "Please type CLEAR ALL to confirm."})
    res = admin_clear_all_data()
    removed_pdfs = 0
    if os.path.isdir(CERT_OUTPUT_DIR):
        for fname in os.listdir(CERT_OUTPUT_DIR):
            if fname.lower().endswith(".pdf"):
                try:
                    os.remove(os.path.join(CERT_OUTPUT_DIR, fname))
                    removed_pdfs += 1
                except Exception:
                    pass
    res["deleted_pdfs"] = removed_pdfs
    return res

@app.get("/api/stats")
def fetch_stats(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return get_statistics()

@app.get("/api/admin/live-feed")
def fetch_admin_live_feed(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {
        "eval_open": get_setting("eval_open", "0") == "1",
        "stats": get_statistics(),
        "attendees": get_all_attendees()
    }

# Admin Control APIs (Protected)
@app.post("/api/admin/set-eval-open")
def set_eval_open(payload: EvalOpenTogglePayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    set_setting("eval_open", "1" if payload.eval_open else "0")
    return {"status": "success", "eval_open": payload.eval_open}

@app.post("/api/admin/tally-evaluations")
def admin_tally_evaluations(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return finalize_and_tally_incomplete_evaluations()

@app.get("/api/admin/evaluation-detail")
def admin_get_evaluation_detail(student_id: str, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    sid = student_id.strip().upper()
    conn = get_db()
    att = conn.cursor().execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (sid,)).fetchone()
    ev = conn.cursor().execute("SELECT * FROM evaluations WHERE UPPER(student_id) = ?", (sid,)).fetchone()
    conn.close()
    if not att:
        return {"status": "error", "message": "Attendee not found."}
    return {
        "status": "success",
        "attendee": dict(att),
        "evaluation": dict(ev) if ev else None
    }

@app.post("/api/admin/set-phase")
def set_phase(payload: PhasePayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    valid_phases = ["MORNING_IN", "MORNING_OUT", "AFTERNOON_IN", "AFTERNOON_OUT", "LOCKED"]
    if payload.phase not in valid_phases:
        raise HTTPException(status_code=400, detail="Invalid phase")
    set_setting("current_phase", payload.phase)
    session, status = get_session_and_status(payload.phase)
    set_setting("active_session_label", f"{session} {status}")
    return {"status": "success", "phase": payload.phase, "session": session, "event_status": status}

@app.post("/api/admin/set-qr-mode")
def set_qr_mode(payload: QRModePayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if payload.mode not in ("STATIC", "ROLLING"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    set_setting("qr_mode", payload.mode)
    if payload.mode == "ROLLING":
        new_token = secrets.token_hex(4).upper()
        set_setting("rolling_token", new_token)
    return {"status": "success", "mode": payload.mode}

@app.post("/api/admin/set-duration")
def set_duration(payload: DurationPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if payload.minutes < 1:
        raise HTTPException(status_code=400, detail="Duration must be positive")
    set_setting("min_duration_minutes", str(payload.minutes))
    recompute_all_attendees_eligibility(float(payload.minutes))
    return {"status": "success", "minutes": payload.minutes, "label": format_duration_hm(payload.minutes)}

@app.post("/api/admin/set-kiosk-config")
def set_kiosk_config(payload: KioskConfigPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    set_setting("event_title", payload.event_title)
    set_setting("active_session_label", payload.active_session_label or "")
    if payload.current_phase:
        set_setting("current_phase", payload.current_phase)
    set_setting("event_date", payload.event_date)
    set_setting("venue", payload.venue)
    set_setting("kiosk_announcement", payload.kiosk_announcement or "")
    if payload.terms_and_conditions:
        set_setting("terms_and_conditions", payload.terms_and_conditions)
    return {"status": "success", "config": payload.model_dump()}

@app.post("/api/admin/set-projector-config")
def set_projector_config(payload: ProjectorConfigPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    set_setting("proj_show_form_qr", "1" if payload.proj_show_form_qr else "0")
    set_setting("proj_show_wifi_qr", "1" if payload.proj_show_wifi_qr else "0")
    set_setting("wifi_ssid", (payload.wifi_ssid or "HIV Awareness Attendance").strip())
    set_setting("wifi_password", payload.wifi_password.strip() if payload.wifi_password else "")
    set_setting("kiosk_url", payload.kiosk_url.strip() if payload.kiosk_url else "")
    set_setting("proj_announcement", payload.proj_announcement.strip() if payload.proj_announcement else "")
    return {"status": "success", "config": payload.model_dump()}

@app.get("/api/admin/network-status")
def admin_network_status(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"status": "success", "network": get_network_status_info()}

@app.post("/api/admin/network-connect")
def admin_network_connect(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    target_ssid = get_setting("wifi_ssid", "HIV Awareness Attendance")
    if sys.platform.startswith("linux"):
        try:
            res = subprocess.run(
                ["nmcli", "con", "up", target_ssid],
                capture_output=True,
                text=True,
                timeout=10
            )
            if res.returncode == 0:
                return {"status": "success", "message": f"Successfully connected to '{target_ssid}'!"}
            else:
                err_msg = res.stderr.strip() or res.stdout.strip()
                return {"status": "error", "message": f"Connection attempt returned: {err_msg}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    elif sys.platform.startswith("win"):
        try:
            res = subprocess.run(
                ["netsh", "wlan", "connect", f"name={target_ssid}"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return {"status": "success", "message": f"Initiated connection to '{target_ssid}'."}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "Direct Wi-Fi control not supported on this OS."}

# CSV Exports
@app.get("/api/export/deped-csv")
def export_deped_csv(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    attendees = get_all_attendees()
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Headers
    writer.writerow([
        "Student ID", "Last Name", "First Name", "Middle Name", "Full Name",
        "Sex", "Age", "Academic Level", 
        "SHS Grade", "SHS Strand", "College Year", "College Course", "Section",
        "Email", "Client IP", "Device Model",
        "Morning In", "Morning Out", "Morning Duration (Mins)",
        "Afternoon In", "Afternoon Out", "Afternoon Duration (Mins)",
        "Duration Criteria Met", "Evaluation Status", "Evaluation Avg Rating",
        "Certificate Eligible", "Cert Status"
    ])
    
    for a in attendees:
        writer.writerow([
            a["student_id"], a.get("last_name") or "", a.get("first_name") or "", a.get("middle_name") or "", a["full_name"],
            a["sex"], a["age"], a["level"],
            a["shs_grade"] or "", a["strand"] or "", a["college_year"] or "", a["course"] or "", a["section"],
            a["email"], a.get("ip_address") or "", a.get("device_info") or "",
            a["morning_in"] or "", a["morning_out"] or "", a["morning_duration_min"],
            a["afternoon_in"] or "", a["afternoon_out"] or "", a["afternoon_duration_min"],
            "YES" if a.get("attendance_eligible") else "NO",
            a.get("eval_status") or "PENDING",
            a.get("eval_avg") if a.get("eval_avg") is not None else "",
            "YES" if a["is_eligible"] else "NO", a["cert_status"]
        ])
        
    output.seek(0)
    filename = f"ACLC_HIV_Seminar_Attendance_Master_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/api/export/certificate-csv")
def export_certificate_csv(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    attendees = get_all_attendees()
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Google Docs / Sheets merge tags
    writer.writerow([
        "Student_ID", "Last_Name", "First_Name", "Middle_Name", "Full_Name", 
        "Sex", "Email", "Level_Strand_Course", "Event_Title", "Event_Date"
    ])
    
    event_title = get_setting("event_title", "HIV Awareness Seminar")
    event_date = get_setting("event_date", datetime.now().strftime("%B %d, %Y"))
    
    for a in attendees:
        if a["is_eligible"]:
            if a["level"] == "SHS":
                parts = [p for p in (a.get("shs_grade"), a.get("strand"), a.get("section")) if p]
                track_info = f"Senior High School ({' - '.join(parts)})" if parts else "Senior High School"
            elif a["level"] == "COLLEGE":
                parts = [p for p in (a.get("college_year"), a.get("course"), a.get("section")) if p]
                track_info = f"College ({' - '.join(parts)})" if parts else "College"
            else:
                track_info = f"Guest / Faculty ({a['section']})"
                
            writer.writerow([
                a["student_id"], a.get("last_name") or "", a.get("first_name") or "", a.get("middle_name") or "",
                a["full_name"], a["sex"], a["email"], track_info, event_title, event_date
            ])
            
    output.seek(0)
    filename = f"ACLC_Certificate_Eligible_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/api/export/evaluation-csv")
def export_evaluation_csv(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    records = get_all_evaluations_for_export()
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Student ID", "Full Name", "Sex", "Age", "Academic Level", "Section", "Email",
        "Evaluation Status", "Tallied Mode", "Submitted Timestamp", "Part I Average (1-5)",
        "Q1.1 Objectives Clearly Presented",
        "Q1.2 Speaker Knowledge of HIV",
        "Q1.3 Info Accurate & Easy to Understand",
        "Q1.4 Clear Understanding of HIV Transmission & Prevention",
        "Q1.5 Addressed Misconceptions & Stigma",
        "Q1.6 Encouraged Active Participation",
        "Q1.7 Increased Awareness of Prevention & Testing",
        "Q1.8 Respect & Non-Discrimination Toward PLHIV",
        "Q1.9 Materials Supported Understanding",
        "Q1.10 Overall Informative & Beneficial",
        "Q2.1 Important Information Learned",
        "Q2.2 Misconception Clarified",
        "Q2.3 Application of Knowledge in Daily Life",
        "Q3.1 Most Helpful Part of Seminar",
        "Q3.2 Topics Related to HIV Awareness to Learn More About",
        "Q3.3 Suggestions to Improve Future Seminars",
        "Attendance Duration Met", "Certificate Eligible"
    ])

    for r in records:
        writer.writerow([
            r["student_id"], r["full_name"], r["sex"], r["age"], r["level"], r["section"], r["email"],
            r["e_status"],
            "Auto-Tallied Draft" if r.get("is_auto_tallied") else "Direct Submit",
            r.get("submitted_at") or r.get("updated_at") or "",
            r.get("q1_avg") or "",
            r.get("q1_1") or "", r.get("q1_2") or "", r.get("q1_3") or "", r.get("q1_4") or "", r.get("q1_5") or "",
            r.get("q1_6") or "", r.get("q1_7") or "", r.get("q1_8") or "", r.get("q1_9") or "", r.get("q1_10") or "",
            r.get("q2_1") or "", r.get("q2_2") or "", r.get("q2_3") or "",
            r.get("q3_1") or "", r.get("q3_2") or "", r.get("q3_3") or "",
            "YES" if r.get("attendance_eligible") else "NO",
            "YES" if r.get("is_eligible") else "NO"
        ])

    output.seek(0)
    filename = f"ACLC_HIV_Awareness_Evaluations_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ==============================================================================
# CERTIFICATE STUDIO & GMAIL SMTP DELIVERY ENDPOINTS
# ==============================================================================

class CertSettingsPayload(BaseModel):
    cert_auto_send: Optional[bool] = None
    cert_name_x_pct: Optional[float] = None
    cert_name_y_pct: Optional[float] = None
    cert_name_font_size: Optional[float] = None
    cert_name_color: Optional[str] = None
    cert_name_uppercase: Optional[bool] = None
    cert_name_font: Optional[str] = None
    cert_show_track: Optional[bool] = None
    cert_track_y_pct: Optional[float] = None
    cert_track_font_size: Optional[float] = None
    cert_show_date: Optional[bool] = None
    cert_date_y_pct: Optional[float] = None
    cert_date_font_size: Optional[float] = None
    cert_show_serial: Optional[bool] = None
    smtp_user: Optional[str] = None
    smtp_app_password: Optional[str] = None
    smtp_from_name: Optional[str] = None
    cert_email_subject: Optional[str] = None
    cert_email_body: Optional[str] = None


class CertTestEmailPayload(BaseModel):
    to_email: str
    sample_name: Optional[str] = "DELA CRUZ, JUAN R."


class CertSendOnePayload(BaseModel):
    student_id: str
    force: bool = True


class CertBatchSendPayload(BaseModel):
    include_failed: bool = True


@app.get("/api/admin/certificate/settings")
def api_get_cert_settings(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    s = get_all_settings()
    tmpl_path = (s.get("cert_template_path") or "").strip()
    has_template = bool(tmpl_path and os.path.isfile(tmpl_path))
    return {
        "cert_auto_send": s.get("cert_auto_send", "0") == "1",
        "has_template": has_template,
        "cert_name_x_pct": float(s.get("cert_name_x_pct", "50.0") or "50.0"),
        "cert_name_y_pct": float(s.get("cert_name_y_pct", "50.0") or "50.0"),
        "cert_name_font_size": float(s.get("cert_name_font_size", "38") or "38"),
        "cert_name_color": s.get("cert_name_color", "#1B365D"),
        "cert_name_uppercase": s.get("cert_name_uppercase", "1") == "1",
        "cert_name_font": s.get("cert_name_font", "Helvetica-Bold"),
        "cert_show_track": s.get("cert_show_track", "0") == "1",
        "cert_track_y_pct": float(s.get("cert_track_y_pct", "57.0") or "57.0"),
        "cert_track_font_size": float(s.get("cert_track_font_size", "15") or "15"),
        "cert_show_date": s.get("cert_show_date", "0") == "1",
        "cert_date_y_pct": float(s.get("cert_date_y_pct", "63.0") or "63.0"),
        "cert_date_font_size": float(s.get("cert_date_font_size", "13") or "13"),
        "cert_show_serial": s.get("cert_show_serial", "1") == "1",
        "smtp_user": s.get("smtp_user", ""),
        "smtp_password_set": bool((s.get("smtp_app_password") or "").strip()),
        "smtp_from_name": s.get("smtp_from_name", "ACLC College Seminar Committee"),
        "cert_email_subject": s.get("cert_email_subject", "Your Certificate of Participation - {event_title}"),
        "cert_email_body": s.get("cert_email_body", ""),
        "batch": get_batch_status(),
    }


@app.post("/api/admin/certificate/settings")
def api_save_cert_settings(payload: CertSettingsPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    bool_fields = (
        "cert_auto_send", "cert_name_uppercase", "cert_show_track",
        "cert_show_date", "cert_show_serial"
    )
    for field in bool_fields:
        val = getattr(payload, field)
        if val is not None:
            set_setting(field, "1" if val else "0")

    num_fields = (
        "cert_name_x_pct", "cert_name_y_pct", "cert_name_font_size",
        "cert_track_y_pct", "cert_track_font_size", "cert_date_y_pct", "cert_date_font_size"
    )
    for field in num_fields:
        val = getattr(payload, field)
        if val is not None:
            set_setting(field, str(val))

    str_fields = (
        "cert_name_color", "cert_name_font", "smtp_user",
        "smtp_from_name", "cert_email_subject", "cert_email_body"
    )
    for field in str_fields:
        val = getattr(payload, field)
        if val is not None:
            set_setting(field, val.strip() if field in ("cert_name_color", "smtp_user", "smtp_from_name") else val)

    # Only overwrite App Password if a non-empty value was explicitly provided
    if payload.smtp_app_password is not None and payload.smtp_app_password.strip():
        set_setting("smtp_app_password", payload.smtp_app_password.strip().replace(" ", ""))

    return {"status": "success", "message": "Certificate & Gmail SMTP settings saved."}


@app.post("/api/admin/certificate/upload-template")
async def api_upload_cert_template(request: Request, file: UploadFile = File(...)):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    content = await file.read()
    if not content or len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File must be a PNG or JPG image under 15 MB.")

    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(content)) as raw_img:
            img = ImageOps.exif_transpose(raw_img)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            os.makedirs(os.path.dirname(CERT_TEMPLATE_FILE), exist_ok=True)
            img.save(CERT_TEMPLATE_FILE, format="PNG")
            w, h = img.size
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image file: {e}")

    set_setting("cert_template_path", CERT_TEMPLATE_FILE)
    return {"status": "success", "message": f"Canva template uploaded ({w}x{h} px).", "width": w, "height": h}


@app.delete("/api/admin/certificate/template")
def api_delete_cert_template(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    set_setting("cert_template_path", "")
    if os.path.isfile(CERT_TEMPLATE_FILE):
        try:
            os.remove(CERT_TEMPLATE_FILE)
        except OSError:
            pass
    return {"status": "success", "message": "Reverted to built-in ACLC certificate layout."}


@app.get("/api/admin/certificate/template-image")
def api_get_cert_template_image(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    tmpl_path = get_setting("cert_template_path", "").strip()
    if not tmpl_path or not os.path.isfile(tmpl_path):
        raise HTTPException(status_code=404, detail="No custom template uploaded.")
    return FileResponse(tmpl_path, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/admin/certificate/preview")
def api_preview_cert_pdf(request: Request, sample_name: str = "DELA CRUZ, JUAN R."):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    sample_attendee = {
        "student_id": "SAMPLE-2026-001",
        "full_name": (sample_name or "DELA CRUZ, JUAN R.").strip(),
        "email": "sample@aclc.edu.ph",
        "level": "COLLEGE",
        "college_year": "3rd Year",
        "course": "BSCS",
        "section": "CS31A",
    }
    pdf_bytes = generate_certificate_pdf(sample_attendee)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=ACLC_Certificate_Preview.pdf"}
    )


@app.post("/api/admin/certificate/test-email")
def api_send_test_cert_email(payload: CertTestEmailPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return send_test_certificate_email(payload.to_email, payload.sample_name or "DELA CRUZ, JUAN R.")


@app.post("/api/admin/certificate/send-one")
def api_send_one_cert(payload: CertSendOnePayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return send_single_certificate(payload.student_id, force=payload.force)


@app.post("/api/admin/certificate/batch-send")
def api_batch_send_certs(payload: CertBatchSendPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return start_batch_send(include_failed=payload.include_failed)


@app.get("/api/admin/certificate/batch-status")
def api_cert_batch_status(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return get_batch_status()


# ==============================================================================
# /quality — HUMAN QUALITY CHECK, STASH POOL & DELIVERY CSV REPORT
# ==============================================================================

class QualityStatusPayload(BaseModel):
    student_id: str
    quality_status: str


class QualityRescuePayload(BaseModel):
    student_id: str


class QualityQuickFixPayload(BaseModel):
    student_id: str
    last_name: str
    first_name: str
    middle_name: Optional[str] = "N/A"
    email: str


@app.get("/quality")
def quality_review_page(request: Request):
    if not check_admin_auth(request):
        return RedirectResponse(url="/admin/login?next=/quality", status_code=303)
    settings = get_all_settings()
    return templates.TemplateResponse(
        request=request,
        name="quality.html",
        context={
            "event_title": settings.get("event_title", "HIV Awareness Seminar"),
            "min_duration": int(float(settings.get("min_duration_minutes", "150") or "150")),
            "smtp_configured": bool((settings.get("smtp_user") or "").strip() and (settings.get("smtp_app_password") or "").strip()),
        }
    )


@app.get("/api/admin/quality/data")
def api_quality_data(request: Request, grace_minutes: float = 10.0):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return get_quality_dashboard_data(grace_minutes=grace_minutes)


@app.post("/api/admin/quality/set-status")
def api_quality_set_status(payload: QualityStatusPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return set_certificate_quality_status(payload.student_id, payload.quality_status)


@app.post("/api/admin/quality/stash-all-clean")
def api_quality_stash_all_clean(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = get_quality_dashboard_data(grace_minutes=0.0)
    clean_ids = [
        a["student_id"] for a in data["eligible"]
        if a["quality"]["severity"] == "clean"
        and a["cert_quality_status"] == "PENDING_REVIEW"
        and a["cert_email_status"] != "SENT"
    ]
    count = bulk_stage_clean_certificates(clean_ids)
    return {"status": "success", "staged_count": count, "message": f"Stashed {count} clean certificate(s) into the send pool."}


@app.post("/api/admin/quality/rescue-near-miss")
def api_quality_rescue_near_miss(payload: QualityRescuePayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return rescue_near_miss_to_eligible(payload.student_id)


@app.post("/api/admin/quality/quick-fix")
def api_quality_quick_fix(payload: QualityQuickFixPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return quick_fix_attendee_identity(
        payload.student_id,
        payload.last_name,
        payload.first_name,
        payload.middle_name or "N/A",
        payload.email,
    )


@app.get("/api/admin/certificate/preview-attendee")
def api_preview_attendee_cert_pdf(student_id: str, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    sid = student_id.strip().upper()
    conn = get_db()
    row = conn.cursor().execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (sid,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Attendee not found.")
    pdf_bytes = generate_certificate_pdf(dict(row))
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=Preview_{sid}.pdf"}
    )


@app.post("/api/admin/certificate/send-staged")
def api_send_staged_certs(payload: CertBatchSendPayload, request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return start_batch_send(include_failed=payload.include_failed, staged_only=True)


@app.get("/api/export/certificate-delivery-csv")
def export_certificate_delivery_csv(request: Request):
    if not check_admin_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = get_quality_dashboard_data(grace_minutes=0.0)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Student_ID", "Full_Name", "Email", "Section", "Duration_Min",
        "Eval_Status", "Cert_Status", "Quality_Status", "Quality_Flags",
        "Email_Delivery_Status", "Emailed_At", "Delivery_Result_Or_Error"
    ])
    for a in data["eligible"]:
        flags_str = "; ".join(f["msg"] for f in a["quality"]["flags"]) if a["quality"]["flags"] else "Clean"
        es = a["cert_email_status"]
        result_note = "OK - Sent without error" if es == "SENT" else (a["cert_email_error"] or "Pending")
        writer.writerow([
            a["student_id"], a["full_name"], a["email"], a["section"], a["duration_min"],
            a["eval_status"], a["cert_status"], a["cert_quality_status"], flags_str,
            es, a["cert_emailed_at"] or "", result_note
        ])
    output.seek(0)
    filename = f"ACLC_Certificate_Email_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


if __name__ == "__main__":
    import uvicorn

    def _find_available_port(preferred: int = 8080) -> int:
        for candidate in (preferred, 8000, 8081, 8888):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("0.0.0.0", candidate))
                    return candidate
                except OSError:
                    continue
        return preferred

    env_port = int(os.environ.get("PORT", "8080"))
    chosen_port = _find_available_port(env_port)
    os.environ["PORT"] = str(chosen_port)
    lan_ip = get_local_ip()

    print("=" * 62)
    print("  ACLC College of Mandaue - HIV Awareness Seminar")
    print("  Offline Attendance, Evaluation & Demographics Server")
    print("=" * 62)
    print(f"  Local Kiosk URL:      http://localhost:{chosen_port}/")
    print(f"  LAN / Wi-Fi URL:      http://{lan_ip}:{chosen_port}/")
    print(f"  Projector Display:    http://localhost:{chosen_port}/projector")
    print(f"  Admin Dashboard:      http://localhost:{chosen_port}/admin")
    print(f"  Exit QR Scanner:      http://localhost:{chosen_port}/admin/scanner")
    print("  Admin Password:       nevergrowold")
    print("=" * 62)
    uvicorn.run("main:app", host="0.0.0.0", port=chosen_port, reload=False)
