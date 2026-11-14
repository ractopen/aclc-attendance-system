import sqlite3
import os
import json
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "attendance.db")
EVAL_LOCK_LEASE_SECONDS = 35
_SETTINGS_CACHE: Dict[str, Dict[str, str]] = {}

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    return conn

def compute_full_name(last_name: str, first_name: str, middle_name: Optional[str] = None) -> str:
    ln = last_name.strip()
    fn = first_name.strip()
    mn = (middle_name or "").strip()
    if mn and mn.upper() not in ("N/A", "NA", "NONE", "-"):
        return f"{ln}, {fn} {mn}"
    return f"{ln}, {fn}"

def normalize_level(raw_level: Optional[str]) -> str:
    val = (raw_level or "").strip().upper()
    if "SHS" in val or "SENIOR" in val:
        return "SHS"
    if "COL" in val:
        return "COLLEGE"
    if "NON" in val or "GUEST" in val or "FACULTY" in val or "OTHER" in val:
        return "NON_STUDENT"
    return val if val in ("SHS", "COLLEGE", "NON_STUDENT") else "SHS"

def infer_academic_details(
    level: str,
    section: str,
    shs_grade: Optional[str] = None,
    strand: Optional[str] = None,
    college_year: Optional[str] = None,
    course: Optional[str] = None
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Infers shs_grade, strand, college_year, and course from section keywords
    when not explicitly supplied by the form payload.
    """
    norm_level = normalize_level(level)
    sec_up = (section or "").strip().upper()

    if norm_level == "SHS":
        inferred_grade = shs_grade
        if not inferred_grade:
            if re.search(r"(?:^|\b|G|GRADE\s*)11(?:\b|-|_)", sec_up):
                inferred_grade = "Grade 11"
            elif re.search(r"(?:^|\b|G|GRADE\s*)12(?:\b|-|_)", sec_up):
                inferred_grade = "Grade 12"

        inferred_strand = strand
        if not inferred_strand:
            if "STEM" in sec_up:
                inferred_strand = "STEM"
            elif "ICT" in sec_up or "TVL" in sec_up or "HE" in sec_up:
                inferred_strand = "TVL-ICT"
            elif "ABM" in sec_up:
                inferred_strand = "ABM"
            elif "HUMSS" in sec_up:
                inferred_strand = "HUMSS"
            elif "GAS" in sec_up:
                inferred_strand = "GAS"
        return inferred_grade, inferred_strand, None, None

    if norm_level == "COLLEGE":
        inferred_course = course
        if not inferred_course:
            if "BSCS" in sec_up or re.search(r"(?:^|\b)CS(?:\d|\b|-|_)", sec_up):
                inferred_course = "BSCS"
            elif "BSIT" in sec_up or re.search(r"(?:^|\b)IT(?:\d|\b|-|_)", sec_up):
                inferred_course = "BSIT"
            elif "BSBA" in sec_up or re.search(r"(?:^|\b)BA(?:\d|\b|-|_)", sec_up):
                inferred_course = "BSBA"
            elif "BSHM" in sec_up or re.search(r"(?:^|\b)HM(?:\d|\b|-|_)", sec_up):
                inferred_course = "BSHM"
            elif "ACT" in sec_up:
                inferred_course = "ACT"

        inferred_year = college_year
        if not inferred_year:
            # Match patterns like BSIT-2B, CS31A (first digit 3 -> 3rd Year), 1ST, etc.
            m_code = re.search(r"(?:BSCS|BSIT|BSBA|BSHM|ACT|CS|IT|BA|HM)[-_\s]*([1-4])", sec_up)
            if m_code:
                yr_num = m_code.group(1)
                yr_map = {"1": "1st Year", "2": "2nd Year", "3": "3rd Year", "4": "4th Year"}
                inferred_year = yr_map.get(yr_num)
        return None, None, inferred_year, inferred_course

    return None, None, None, None

def format_time_str(iso_str: Optional[str]) -> str:
    """Formats an ISO timestamp (YYYY-MM-DD HH:MM:SS) into clean format (e.g. 8:02 AM)"""
    if not iso_str:
        return ""
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%d %H:%M:%S")
        hour = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return f"{hour}:{dt.minute:02d} {ampm}"
    except Exception:
        return iso_str

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Attendees table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS attendees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id TEXT UNIQUE NOT NULL,
        last_name TEXT NOT NULL,
        first_name TEXT NOT NULL,
        middle_name TEXT DEFAULT 'N/A',
        full_name TEXT NOT NULL,
        sex TEXT NOT NULL,
        age INTEGER NOT NULL,
        email TEXT NOT NULL,
        level TEXT NOT NULL, -- 'SHS', 'COLLEGE', or 'NON_STUDENT'
        shs_grade TEXT,      -- 'Grade 11' or 'Grade 12'
        strand TEXT,         -- 'STEM', 'TVL-ICT', 'ABM', 'HUMSS', 'GAS', etc.
        college_year TEXT,   -- '1st Year', '2nd Year', '3rd Year', '4th Year'
        course TEXT,         -- 'BSIT', 'BSCS', 'BSBA', 'ACT', etc.
        section TEXT NOT NULL,
        ip_address TEXT,
        device_info TEXT,
        morning_in TIMESTAMP,
        morning_out TIMESTAMP,
        morning_duration_min REAL DEFAULT 0,
        afternoon_in TIMESTAMP,
        afternoon_out TIMESTAMP,
        afternoon_duration_min REAL DEFAULT 0,
        attendance_eligible INTEGER DEFAULT 0,
        eval_status TEXT DEFAULT 'PENDING',
        is_eligible INTEGER DEFAULT 0,
        cert_status TEXT DEFAULT 'PENDING',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Safe schema migrations for existing databases
    existing_cols = {row["name"] for row in cursor.execute("PRAGMA table_info(attendees)").fetchall()}
    if "attendance_eligible" not in existing_cols:
        cursor.execute("ALTER TABLE attendees ADD COLUMN attendance_eligible INTEGER DEFAULT 0;")
    if "eval_status" not in existing_cols:
        cursor.execute("ALTER TABLE attendees ADD COLUMN eval_status TEXT DEFAULT 'PENDING';")
    
    # Evaluations table (Per-Account Seminar Evaluation Form with Auto-Save & Session Lock)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS evaluations (
        student_id TEXT PRIMARY KEY,
        status TEXT DEFAULT 'IN_PROGRESS', -- 'IN_PROGRESS' or 'SUBMITTED'
        draft_json TEXT DEFAULT '{}',
        q1_1 INTEGER,
        q1_2 INTEGER,
        q1_3 INTEGER,
        q1_4 INTEGER,
        q1_5 INTEGER,
        q1_6 INTEGER,
        q1_7 INTEGER,
        q1_8 INTEGER,
        q1_9 INTEGER,
        q1_10 INTEGER,
        q1_avg REAL DEFAULT 0,
        q2_1 TEXT DEFAULT '',
        q2_2 TEXT DEFAULT '',
        q2_3 TEXT DEFAULT '',
        q3_1 TEXT DEFAULT '',
        q3_2 TEXT DEFAULT '',
        q3_3 TEXT DEFAULT '',
        is_auto_tallied INTEGER DEFAULT 0,
        active_session_id TEXT,
        lock_expires_at TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        submitted_at TIMESTAMP,
        ip_address TEXT,
        device_info TEXT
    )
    """)

    # Performance Indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_attendees_level ON attendees(level);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_attendees_sex ON attendees(sex);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_attendees_eligible ON attendees(is_eligible);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_attendees_eval_status ON attendees(eval_status);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_attendees_upper_sid ON attendees(UPPER(student_id));")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_evaluations_status ON evaluations(status);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_evaluations_upper_sid ON evaluations(UPPER(student_id));")
    
    # System Settings table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)
    
    # Default settings (150 minutes default eligible time, evaluation closed at server start, HIV Awareness theme)
    default_settings = {
        "admin_password": "nevergrowold",
        "current_phase": "MORNING_IN",
        "qr_mode": "STATIC",
        "rolling_token": "ACLC-START",
        "min_duration_minutes": "150",
        "eval_open": "0",
        "event_title": "HIV Awareness Seminar",
        "active_session_label": "Morning Session Time-In",
        "event_date": datetime.now().strftime("%B %d, %Y"),
        "venue": "ACLC College of Mandaue",
        "kiosk_announcement": "Welcome to the HIV Awareness Seminar! Please Time In, complete the Seminar Evaluation Form when opened, and scan your pass for Time Out.",
        "terms_and_conditions": "1. Attendance Verification: Attendees must record their Time-In upon arrival and Time-Out prior to leaving.\n2. Minimum Duration & Evaluation: To qualify for the official Certificate of Participation, attendees must complete at least 2 hours and 30 minutes (150 mins) of active attendance AND submit the HIV Awareness Seminar Evaluation Form.\n3. Certificate Accuracy: Ensure your Student ID, Full Name, and Email address are entered accurately.\n4. Respect & Non-Discrimination: Participants must maintain a respectful, safe, and non-discriminatory environment.",
        "wifi_ssid": "Event_Attendance",
        "wifi_password": "",
        "kiosk_url": "",
        "proj_show_form_qr": "1",
        "proj_show_wifi_qr": "1",
        "proj_announcement": "Welcome to the HIV Awareness Seminar! Connect to Wi-Fi and Scan QR to record attendance and access your evaluation form."
    }
    
    for key, val in default_settings.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

    # Enforce server-start defaults requested: evaluation closed on server start, migrate old 90-min/title defaults
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('eval_open', '0')")
    cursor.execute("UPDATE settings SET value = '150' WHERE key = 'min_duration_minutes' AND value = '90'")
    cursor.execute("UPDATE settings SET value = 'HIV Awareness Seminar' WHERE key = 'event_title' AND value = 'Gender and Society Seminar 2026'")
    cursor.execute("""
        UPDATE settings
        SET value = 'Welcome to the HIV Awareness Seminar! Please Time In, complete the Seminar Evaluation Form when opened, and scan your pass for Time Out.'
        WHERE key = 'kiosk_announcement' AND value LIKE '%Gender and Society Seminar%'
    """)
    cursor.execute("""
        UPDATE settings
        SET value = 'Welcome to the HIV Awareness Seminar! Connect to Wi-Fi and Scan QR to record attendance and access your evaluation form.'
        WHERE key = 'proj_announcement' AND value = 'Welcome Attendees! Connect to Wi-Fi and Scan QR to record attendance.'
    """)
        
    conn.commit()
    rows = cursor.execute("SELECT key, value FROM settings").fetchall()
    _SETTINGS_CACHE[DB_PATH] = {r["key"]: r["value"] for r in rows}
    conn.close()

def _ensure_settings_cache() -> Dict[str, str]:
    cache = _SETTINGS_CACHE.get(DB_PATH)
    if cache is None:
        conn = get_db()
        rows = conn.cursor().execute("SELECT key, value FROM settings").fetchall()
        conn.close()
        cache = {r["key"]: r["value"] for r in rows}
        _SETTINGS_CACHE[DB_PATH] = cache
    return cache

def get_setting(key: str, default: str = "") -> str:
    cache = _ensure_settings_cache()
    return cache.get(key, default)

def set_setting(key: str, value: str):
    str_val = str(value)
    conn = get_db()
    conn.cursor().execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str_val))
    conn.commit()
    conn.close()
    cache = _ensure_settings_cache()
    cache[key] = str_val

def get_all_settings() -> Dict[str, str]:
    return dict(_ensure_settings_cache())

def recompute_attendee_eligibility(cursor: sqlite3.Cursor, student_id: str, min_duration: float) -> Dict[str, Any]:
    """
    Recomputes attendance_eligible, is_eligible, and cert_status for a student.
    To be eligible for a certificate (is_eligible = 1, cert_status = 'APPROVED'):
      1. Student must have timed in AND timed out with total duration >= min_duration
      2. Student must have submitted the Seminar Evaluation Form (eval_status == 'SUBMITTED')
      (Or if an admin explicitly set cert_status == 'MANUAL_APPROVED', preserves approval).
    """
    sid = student_id.strip().upper()
    row = cursor.execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (sid,)).fetchone()
    if not row:
        return {"attendance_eligible": False, "eval_submitted": False, "is_eligible": False, "cert_status": "PENDING"}

    m_dur = float(row["morning_duration_min"] or 0) if row["morning_out"] else 0.0
    a_dur = float(row["afternoon_duration_min"] or 0) if row["afternoon_out"] else 0.0
    has_timeout = bool(row["morning_out"] or row["afternoon_out"])
    total_dur = round(m_dur + a_dur, 1)

    att_eligible = 1 if (has_timeout and total_dur >= min_duration) else 0
    eval_status = (row["eval_status"] or "PENDING").upper()
    eval_submitted = (eval_status == "SUBMITTED")

    if row["cert_status"] == "MANUAL_APPROVED" and int(row["is_eligible"] or 0) == 1:
        is_eligible = 1
        cert_status = "MANUAL_APPROVED"
    else:
        is_eligible = 1 if (att_eligible == 1 and eval_submitted) else 0
        if is_eligible == 1:
            cert_status = "APPROVED"
        elif att_eligible == 1 and not eval_submitted:
            cert_status = "EVAL_PENDING"
        elif eval_submitted and not has_timeout:
            cert_status = "TIMEOUT_PENDING"
        elif has_timeout and att_eligible == 0:
            cert_status = "UNDER_DURATION"
        else:
            cert_status = "PENDING"

    cursor.execute("""
        UPDATE attendees SET
            attendance_eligible = ?,
            is_eligible = ?,
            cert_status = ?
        WHERE UPPER(student_id) = ?
    """, (att_eligible, is_eligible, cert_status, sid))

    return {
        "attendance_eligible": bool(att_eligible),
        "eval_submitted": eval_submitted,
        "eval_status": eval_status,
        "is_eligible": bool(is_eligible),
        "cert_status": cert_status,
        "duration_min": total_dur
    }

def recompute_all_attendees_eligibility(min_duration: float):
    conn = get_db()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT student_id FROM attendees").fetchall()
    for r in rows:
        recompute_attendee_eligibility(cursor, r["student_id"], min_duration)
    conn.commit()
    conn.close()

def register_time_in(data: Dict[str, Any], phase: str, ip_address: str = "", device_info: str = "") -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Normalize ID and names
    student_id = data["student_id"].strip().upper()
    last_name = data["last_name"].strip()
    first_name = data["first_name"].strip()
    raw_middle = (data.get("middle_name") or "").strip()
    middle_name = "N/A" if (not raw_middle or raw_middle.upper() in ("N/A", "NA", "NONE", "-")) else raw_middle
    full_name = compute_full_name(last_name, first_name, middle_name)
    sex = data["sex"].strip()
    section = data["section"].strip()
    email = data["email"].strip()
    level = normalize_level(data["level"])
    shs_grade, strand, college_year, course = infer_academic_details(
        level, section,
        data.get("shs_grade"), data.get("strand"),
        data.get("college_year"), data.get("course")
    )
    
    # Check if student exists
    row = cursor.execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (student_id,)).fetchone()
    
    if phase == "MORNING_IN":
        if row:
            if row["morning_in"]:
                conn.close()
                return {
                    "status": "already_timed_in",
                    "message": f"Already timed in at {format_time_str(row['morning_in'])}",
                    "student_name": row["full_name"],
                    "student_id": student_id,
                    "section": row["section"],
                    "time": row["morning_in"],
                    "time_fmt": format_time_str(row["morning_in"]),
                    "eval_status": row["eval_status"] or "PENDING",
                    "is_eligible": bool(row["is_eligible"]),
                    "cert_status": row["cert_status"] or "PENDING"
                }
            cursor.execute("""
                UPDATE attendees SET
                    last_name = ?, first_name = ?, middle_name = ?, full_name = ?,
                    sex = ?, age = ?, email = ?, level = ?,
                    shs_grade = ?, strand = ?, college_year = ?, course = ?, section = ?,
                    ip_address = ?, device_info = ?, morning_in = ?
                WHERE student_id = ?
            """, (
                last_name, first_name, middle_name, full_name,
                sex, data["age"], email, level,
                shs_grade, strand, college_year, course, section,
                ip_address, device_info, now_iso, student_id
            ))
        else:
            cursor.execute("""
                INSERT INTO attendees (
                    student_id, last_name, first_name, middle_name, full_name,
                    sex, age, email, level,
                    shs_grade, strand, college_year, course, section,
                    ip_address, device_info, morning_in
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                student_id, last_name, first_name, middle_name, full_name,
                sex, data["age"], email, level,
                shs_grade, strand, college_year, course, section,
                ip_address, device_info, now_iso
            ))
    elif phase == "AFTERNOON_IN":
        if row:
            if row["afternoon_in"]:
                conn.close()
                return {
                    "status": "already_timed_in",
                    "message": f"Already timed in at {format_time_str(row['afternoon_in'])}",
                    "student_name": row["full_name"],
                    "student_id": student_id,
                    "section": row["section"],
                    "time": row["afternoon_in"],
                    "time_fmt": format_time_str(row["afternoon_in"]),
                    "eval_status": row["eval_status"] or "PENDING",
                    "is_eligible": bool(row["is_eligible"]),
                    "cert_status": row["cert_status"] or "PENDING"
                }
            cursor.execute("""
                UPDATE attendees SET
                    last_name = ?, first_name = ?, middle_name = ?, full_name = ?,
                    sex = ?, age = ?, email = ?, level = ?,
                    shs_grade = ?, strand = ?, college_year = ?, course = ?, section = ?,
                    ip_address = ?, device_info = ?, afternoon_in = ?
                WHERE student_id = ?
            """, (
                last_name, first_name, middle_name, full_name,
                sex, data["age"], email, level,
                shs_grade, strand, college_year, course, section,
                ip_address, device_info, now_iso, student_id
            ))
        else:
            cursor.execute("""
                INSERT INTO attendees (
                    student_id, last_name, first_name, middle_name, full_name,
                    sex, age, email, level,
                    shs_grade, strand, college_year, course, section,
                    ip_address, device_info, afternoon_in
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                student_id, last_name, first_name, middle_name, full_name,
                sex, data["age"], email, level,
                shs_grade, strand, college_year, course, section,
                ip_address, device_info, now_iso
            ))
    else:
        conn.close()
        return {"status": "error", "message": "Time In is closed"}
        
    conn.commit()
    conn.close()
    return {
        "status": "success",
        "message": f"Timed in at {format_time_str(now_iso)}",
        "time": now_iso,
        "time_fmt": format_time_str(now_iso),
        "student_name": full_name,
        "student_id": student_id,
        "section": section,
        "eval_status": row["eval_status"] if row else "PENDING",
        "is_eligible": bool(row["is_eligible"]) if row else False,
        "cert_status": row["cert_status"] if row else "PENDING"
    }

def register_time_out(student_id: str, phase: str, min_duration: float, ip_address: str = "") -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    student_id = student_id.strip().upper()
    row = cursor.execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (student_id,)).fetchone()
    
    if not row:
        conn.close()
        return {"status": "error", "message": f"Student ID '{student_id}' not found. Please time in first."}
        
    now = datetime.now()
    now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
    
    if phase not in ("MORNING_OUT", "AFTERNOON_OUT"):
        conn.close()
        return {"status": "error", "message": "Time Out is not open yet"}

    # Determine which session slot to check out (supports whole-day Morning-In -> Afternoon-Out seamlessly)
    target_slot = None
    if phase == "MORNING_OUT":
        if row["morning_in"]:
            target_slot = "MORNING"
        elif row["afternoon_in"]:
            target_slot = "AFTERNOON"
    elif phase == "AFTERNOON_OUT":
        if row["afternoon_in"]:
            target_slot = "AFTERNOON"
        elif row["morning_in"]:
            target_slot = "MORNING"

    if not target_slot:
        conn.close()
        return {"status": "error", "message": "No Time In record found for this attendee."}

    if target_slot == "MORNING":
        if row["morning_out"]:
            elig_info = recompute_attendee_eligibility(cursor, student_id, min_duration)
            conn.commit()
            conn.close()
            return {
                "status": "already_timed_out",
                "message": f"Already timed out at {format_time_str(row['morning_out'])} ({round(elig_info['duration_min'])} min)",
                "student_name": row["full_name"],
                "student_id": student_id,
                "time": row["morning_out"],
                "time_fmt": format_time_str(row["morning_out"]),
                "duration_min": elig_info["duration_min"],
                "attendance_eligible": elig_info["attendance_eligible"],
                "eval_status": elig_info["eval_status"],
                "is_eligible": elig_info["is_eligible"],
                "cert_status": elig_info["cert_status"]
            }
            
        time_in_dt = datetime.strptime(row["morning_in"], "%Y-%m-%d %H:%M:%S")
        duration_min = max(0.0, round((now - time_in_dt).total_seconds() / 60, 1))
        
        cursor.execute("""
            UPDATE attendees SET
                morning_out = ?,
                morning_duration_min = ?
            WHERE student_id = ?
        """, (now_iso, duration_min, row["student_id"]))
        
    else:
        if row["afternoon_out"]:
            elig_info = recompute_attendee_eligibility(cursor, student_id, min_duration)
            conn.commit()
            conn.close()
            return {
                "status": "already_timed_out",
                "message": f"Already timed out at {format_time_str(row['afternoon_out'])} ({round(elig_info['duration_min'])} min)",
                "student_name": row["full_name"],
                "student_id": student_id,
                "time": row["afternoon_out"],
                "time_fmt": format_time_str(row["afternoon_out"]),
                "duration_min": elig_info["duration_min"],
                "attendance_eligible": elig_info["attendance_eligible"],
                "eval_status": elig_info["eval_status"],
                "is_eligible": elig_info["is_eligible"],
                "cert_status": elig_info["cert_status"]
            }
            
        time_in_dt = datetime.strptime(row["afternoon_in"], "%Y-%m-%d %H:%M:%S")
        duration_min = max(0.0, round((now - time_in_dt).total_seconds() / 60, 1))
        
        cursor.execute("""
            UPDATE attendees SET
                afternoon_out = ?,
                afternoon_duration_min = ?
            WHERE student_id = ?
        """, (now_iso, duration_min, row["student_id"]))
        
    elig_info = recompute_attendee_eligibility(cursor, student_id, min_duration)
    total_dur = elig_info["duration_min"]
    conn.commit()
    conn.close()
    
    if elig_info["is_eligible"]:
        eligibility_note = "Certificate Approved (Duration & Evaluation Complete)"
    elif elig_info["attendance_eligible"] and not elig_info["eval_submitted"]:
        eligibility_note = f"Duration Met ({round(total_dur)} min) - Evaluation Required for Certificate"
    else:
        eligibility_note = f"Under duration ({round(total_dur)} min < {round(min_duration)} min required)"

    return {
        "status": "success",
        "message": f"Timed out at {format_time_str(now_iso)} ({round(total_dur)} min)",
        "student_name": row["full_name"],
        "student_id": student_id,
        "duration_min": total_dur,
        "attendance_eligible": elig_info["attendance_eligible"],
        "eval_status": elig_info["eval_status"],
        "is_eligible": elig_info["is_eligible"],
        "cert_status": elig_info["cert_status"],
        "note": eligibility_note,
        "time": now_iso,
        "time_fmt": format_time_str(now_iso)
    }

# =====================================================================
# SEMINAR EVALUATION FORM: LOCKING, AUTO-SAVE, SUBMIT & TALLY FUNCTIONS
# =====================================================================

def _sanitize_draft_payload(raw_draft: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitizes and normalizes rating (q1_1..q1_10) and open-ended (q2_1..q3_3) draft fields."""
    clean: Dict[str, Any] = {}
    for i in range(1, 11):
        k = f"q1_{i}"
        val = raw_draft.get(k)
        if val is not None and str(val).strip() != "":
            try:
                iv = int(val)
                if 1 <= iv <= 5:
                    clean[k] = iv
                else:
                    clean[k] = None
            except Exception:
                clean[k] = None
        else:
            clean[k] = None

    for part in (2, 3):
        for j in range(1, 4):
            k = f"q{part}_{j}"
            val = raw_draft.get(k)
            if val is not None:
                s = re.sub(r"[<>]", "", str(val)).strip()
                clean[k] = s[:2000]
            else:
                clean[k] = ""

    ratings = [clean[f"q1_{i}"] for i in range(1, 11) if clean.get(f"q1_{i}") is not None]
    clean["q1_avg"] = round(sum(ratings) / len(ratings), 2) if ratings else 0.0
    clean["answered_count"] = len(ratings) + sum(
        1 for part in (2, 3) for j in range(1, 4) if clean.get(f"q{part}_{j}", "").strip() != ""
    )
    return clean

def _is_lock_active(lock_expires_at: Optional[str], now: datetime) -> Tuple[bool, int]:
    if not lock_expires_at:
        return False, 0
    try:
        exp_dt = datetime.strptime(lock_expires_at, "%Y-%m-%d %H:%M:%S")
        if now < exp_dt:
            rem = int((exp_dt - now).total_seconds())
            return True, max(1, rem)
    except Exception:
        pass
    return False, 0

def open_or_resume_evaluation(
    student_id: str,
    session_id: str,
    ip_address: str = "",
    device_info: str = "",
    force: bool = False
) -> Dict[str, Any]:
    """
    Acquires or renews the per-account evaluation session lock with first-in priority.
    If another active session holds an unexpired lock for this student_id (and force=False), returns status='locked'.
    If force=True, immediately transfers the active session lock to this session so a student switching browsers is never stuck.
    If already submitted, returns status='already_submitted'.
    """
    sid = student_id.strip().upper()
    sess = session_id.strip()
    if not sess:
        return {"status": "error", "message": "Missing client session identifier."}

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        eval_open_row = cursor.execute("SELECT value FROM settings WHERE key = 'eval_open'").fetchone()
        eval_open = (eval_open_row["value"] == "1") if eval_open_row else False

        attendee = cursor.execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (sid,)).fetchone()
        if not attendee:
            conn.rollback()
            conn.close()
            return {"status": "error", "message": f"Student ID '{sid}' not found. Please Time In first."}

        eval_row = cursor.execute("SELECT * FROM evaluations WHERE UPPER(student_id) = ?", (sid,)).fetchone()
        if (eval_row and eval_row["status"] == "SUBMITTED") or (attendee["eval_status"] == "SUBMITTED"):
            conn.rollback()
            conn.close()
            return {
                "status": "already_submitted",
                "message": "Your Seminar Evaluation Form has already been submitted and locked.",
                "student_id": attendee["student_id"],
                "student_name": attendee["full_name"],
                "is_eligible": bool(attendee["is_eligible"]),
                "cert_status": attendee["cert_status"]
            }

        if not eval_open:
            conn.rollback()
            conn.close()
            return {
                "status": "eval_closed",
                "message": "The Seminar Evaluation Form is currently closed. Please wait for the organizer to open it during or after the lecture."
            }

        now = datetime.now()
        now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
        lease_iso = (now + timedelta(seconds=EVAL_LOCK_LEASE_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")

        if eval_row:
            holder = eval_row["active_session_id"]
            is_locked, rem_sec = _is_lock_active(eval_row["lock_expires_at"], now)
            if not force and holder and holder != sess and is_locked:
                conn.rollback()
                conn.close()
                return {
                    "status": "locked",
                    "message": f"An evaluation is currently being taken under {attendee['full_name']} ({attendee['student_id']}) on another device or window. Only the first active session has priority.",
                    "student_id": attendee["student_id"],
                    "student_name": attendee["full_name"],
                    "retry_after_seconds": rem_sec
                }

            cursor.execute("""
                UPDATE evaluations SET
                    active_session_id = ?,
                    lock_expires_at = ?,
                    updated_at = ?,
                    ip_address = CASE WHEN ? != '' THEN ? ELSE ip_address END,
                    device_info = CASE WHEN ? != '' THEN ? ELSE device_info END
                WHERE UPPER(student_id) = ?
            """, (sess, lease_iso, now_iso, ip_address, ip_address, device_info, device_info, sid))
            try:
                saved_draft = json.loads(eval_row["draft_json"] or "{}")
            except Exception:
                saved_draft = {}
        else:
            saved_draft = {}
            cursor.execute("""
                INSERT INTO evaluations (
                    student_id, status, draft_json, active_session_id, lock_expires_at,
                    updated_at, ip_address, device_info
                ) VALUES (?, 'IN_PROGRESS', '{}', ?, ?, ?, ?, ?)
            """, (attendee["student_id"], sess, lease_iso, now_iso, ip_address, device_info))

        if (attendee["eval_status"] or "PENDING") == "PENDING":
            cursor.execute("UPDATE attendees SET eval_status = 'IN_PROGRESS' WHERE UPPER(student_id) = ?", (sid,))

        conn.commit()
        conn.close()
        return {
            "status": "success",
            "student_id": attendee["student_id"],
            "student_name": attendee["full_name"],
            "section": attendee["section"],
            "draft": saved_draft,
            "lease_seconds": EVAL_LOCK_LEASE_SECONDS,
            "saved_at_fmt": format_time_str(now_iso)
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return {"status": "error", "message": f"Database error opening evaluation: {str(e)}"}

def autosave_evaluation_draft(
    student_id: str,
    session_id: str,
    draft: Dict[str, Any],
    ip_address: str = "",
    device_info: str = ""
) -> Dict[str, Any]:
    """
    Auto-saves the student's in-progress evaluation answers and extends the active session lease.
    Blocks overwrites if another active session owns the lock or if already submitted.
    """
    sid = student_id.strip().upper()
    sess = session_id.strip()
    clean = _sanitize_draft_payload(draft or {})

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        attendee = cursor.execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (sid,)).fetchone()
        if not attendee:
            conn.rollback()
            conn.close()
            return {"status": "error", "message": "Student record not found."}

        eval_row = cursor.execute("SELECT * FROM evaluations WHERE UPPER(student_id) = ?", (sid,)).fetchone()
        if (eval_row and eval_row["status"] == "SUBMITTED") or attendee["eval_status"] == "SUBMITTED":
            conn.rollback()
            conn.close()
            return {"status": "already_submitted", "message": "Evaluation has already been submitted and locked."}

        now = datetime.now()
        now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
        lease_iso = (now + timedelta(seconds=EVAL_LOCK_LEASE_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")

        if eval_row:
            holder = eval_row["active_session_id"]
            is_locked, rem_sec = _is_lock_active(eval_row["lock_expires_at"], now)
            if holder and holder != sess and is_locked:
                conn.rollback()
                conn.close()
                return {
                    "status": "locked",
                    "message": "Another session currently holds priority for this evaluation.",
                    "retry_after_seconds": rem_sec
                }

            cursor.execute("""
                UPDATE evaluations SET
                    draft_json = ?,
                    q1_1 = ?, q1_2 = ?, q1_3 = ?, q1_4 = ?, q1_5 = ?,
                    q1_6 = ?, q1_7 = ?, q1_8 = ?, q1_9 = ?, q1_10 = ?,
                    q1_avg = ?,
                    q2_1 = ?, q2_2 = ?, q2_3 = ?,
                    q3_1 = ?, q3_2 = ?, q3_3 = ?,
                    active_session_id = ?,
                    lock_expires_at = ?,
                    updated_at = ?
                WHERE UPPER(student_id) = ?
            """, (
                json.dumps(clean),
                clean["q1_1"], clean["q1_2"], clean["q1_3"], clean["q1_4"], clean["q1_5"],
                clean["q1_6"], clean["q1_7"], clean["q1_8"], clean["q1_9"], clean["q1_10"],
                clean["q1_avg"],
                clean["q2_1"], clean["q2_2"], clean["q2_3"],
                clean["q3_1"], clean["q3_2"], clean["q3_3"],
                sess, lease_iso, now_iso, sid
            ))
        else:
            cursor.execute("""
                INSERT INTO evaluations (
                    student_id, status, draft_json,
                    q1_1, q1_2, q1_3, q1_4, q1_5, q1_6, q1_7, q1_8, q1_9, q1_10, q1_avg,
                    q2_1, q2_2, q2_3, q3_1, q3_2, q3_3,
                    active_session_id, lock_expires_at, updated_at, ip_address, device_info
                ) VALUES (?, 'IN_PROGRESS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                attendee["student_id"], json.dumps(clean),
                clean["q1_1"], clean["q1_2"], clean["q1_3"], clean["q1_4"], clean["q1_5"],
                clean["q1_6"], clean["q1_7"], clean["q1_8"], clean["q1_9"], clean["q1_10"],
                clean["q1_avg"],
                clean["q2_1"], clean["q2_2"], clean["q2_3"],
                clean["q3_1"], clean["q3_2"], clean["q3_3"],
                sess, lease_iso, now_iso, ip_address, device_info
            ))

        if attendee["eval_status"] != "SUBMITTED":
            cursor.execute("UPDATE attendees SET eval_status = 'IN_PROGRESS' WHERE UPPER(student_id) = ?", (sid,))

        conn.commit()
        conn.close()
        return {
            "status": "success",
            "saved_at": now_iso,
            "saved_at_fmt": format_time_str(now_iso),
            "answered_count": clean["answered_count"]
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return {"status": "error", "message": f"Auto-save error: {str(e)}"}

def release_evaluation_lock(
    student_id: str,
    session_id: str,
    draft: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Saves final draft (if provided) and immediately releases the active session lock
    so the student can close and reopen their evaluation on the same or another device.
    """
    sid = student_id.strip().upper()
    sess = session_id.strip()

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        eval_row = cursor.execute("SELECT * FROM evaluations WHERE UPPER(student_id) = ?", (sid,)).fetchone()
        if not eval_row or eval_row["status"] == "SUBMITTED":
            conn.rollback()
            conn.close()
            return {"status": "success"}

        now = datetime.now()
        now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
        holder = eval_row["active_session_id"]
        is_locked, _ = _is_lock_active(eval_row["lock_expires_at"], now)

        # Only the lock owner (or if lock already expired) can release/update draft
        if not holder or holder == sess or not is_locked:
            if draft is not None:
                clean = _sanitize_draft_payload(draft)
                cursor.execute("""
                    UPDATE evaluations SET
                        draft_json = ?,
                        q1_1 = ?, q1_2 = ?, q1_3 = ?, q1_4 = ?, q1_5 = ?,
                        q1_6 = ?, q1_7 = ?, q1_8 = ?, q1_9 = ?, q1_10 = ?,
                        q1_avg = ?,
                        q2_1 = ?, q2_2 = ?, q2_3 = ?,
                        q3_1 = ?, q3_2 = ?, q3_3 = ?,
                        active_session_id = NULL,
                        lock_expires_at = NULL,
                        updated_at = ?
                    WHERE UPPER(student_id) = ?
                """, (
                    json.dumps(clean),
                    clean["q1_1"], clean["q1_2"], clean["q1_3"], clean["q1_4"], clean["q1_5"],
                    clean["q1_6"], clean["q1_7"], clean["q1_8"], clean["q1_9"], clean["q1_10"],
                    clean["q1_avg"],
                    clean["q2_1"], clean["q2_2"], clean["q2_3"],
                    clean["q3_1"], clean["q3_2"], clean["q3_3"],
                    now_iso, sid
                ))
            else:
                cursor.execute("""
                    UPDATE evaluations SET
                        active_session_id = NULL,
                        lock_expires_at = NULL,
                        updated_at = ?
                    WHERE UPPER(student_id) = ?
                """, (now_iso, sid))

        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return {"status": "error", "message": str(e)}

def submit_evaluation(
    student_id: str,
    session_id: str,
    answers: Dict[str, Any],
    ip_address: str = "",
    device_info: str = ""
) -> Dict[str, Any]:
    """
    Validates and permanently submits a student's Seminar Evaluation Form.
    Once submitted, the student is locked out from retaking the evaluation and their
    certificate eligibility is recomputed against their attendance duration.
    """
    sid = student_id.strip().upper()
    sess = session_id.strip()
    clean = _sanitize_draft_payload(answers or {})

    # Validate all 10 Likert ratings
    for i in range(1, 11):
        if clean.get(f"q1_{i}") is None:
            return {"status": "error", "message": f"Please select a rating (1 to 5) for Statement #{i} in Part I."}

    # Validate all 6 open-ended questions
    part_ii_labels = [
        "II.1 (Important information learned)",
        "II.2 (Misconception clarified)",
        "II.3 (Application in daily life)"
    ]
    for j in range(1, 4):
        if not clean.get(f"q2_{j}", "").strip():
            return {"status": "error", "message": f"Please provide an answer for {part_ii_labels[j-1]}."}

    part_iii_labels = [
        "III.1 (Most helpful part of the seminar)",
        "III.2 (Topics to learn more about)",
        "III.3 (Suggestions for future seminars)"
    ]
    for j in range(1, 4):
        if not clean.get(f"q3_{j}", "").strip():
            return {"status": "error", "message": f"Please provide an answer for {part_iii_labels[j-1]}."}

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        attendee = cursor.execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (sid,)).fetchone()
        if not attendee:
            conn.rollback()
            conn.close()
            return {"status": "error", "message": f"Student ID '{sid}' not found. Please Time In first."}

        min_dur_row = cursor.execute("SELECT value FROM settings WHERE key = 'min_duration_minutes'").fetchone()
        min_dur = float(min_dur_row["value"]) if min_dur_row else 150.0

        eval_row = cursor.execute("SELECT * FROM evaluations WHERE UPPER(student_id) = ?", (sid,)).fetchone()
        if (eval_row and eval_row["status"] == "SUBMITTED") or attendee["eval_status"] == "SUBMITTED":
            elig_info = recompute_attendee_eligibility(cursor, sid, min_dur)
            conn.commit()
            conn.close()
            return {
                "status": "already_submitted",
                "message": "Your evaluation was already submitted.",
                "student_id": attendee["student_id"],
                "student_name": attendee["full_name"],
                "is_eligible": elig_info["is_eligible"],
                "attendance_eligible": elig_info["attendance_eligible"],
                "cert_status": elig_info["cert_status"]
            }

        now = datetime.now()
        now_iso = now.strftime("%Y-%m-%d %H:%M:%S")

        if eval_row:
            holder = eval_row["active_session_id"]
            is_locked, rem_sec = _is_lock_active(eval_row["lock_expires_at"], now)
            if holder and holder != sess and is_locked:
                conn.rollback()
                conn.close()
                return {
                    "status": "locked",
                    "message": "Another session currently holds priority for this evaluation.",
                    "retry_after_seconds": rem_sec
                }

            cursor.execute("""
                UPDATE evaluations SET
                    status = 'SUBMITTED',
                    draft_json = ?,
                    q1_1 = ?, q1_2 = ?, q1_3 = ?, q1_4 = ?, q1_5 = ?,
                    q1_6 = ?, q1_7 = ?, q1_8 = ?, q1_9 = ?, q1_10 = ?,
                    q1_avg = ?,
                    q2_1 = ?, q2_2 = ?, q2_3 = ?,
                    q3_1 = ?, q3_2 = ?, q3_3 = ?,
                    is_auto_tallied = 0,
                    active_session_id = NULL,
                    lock_expires_at = NULL,
                    updated_at = ?,
                    submitted_at = ?,
                    ip_address = CASE WHEN ? != '' THEN ? ELSE ip_address END,
                    device_info = CASE WHEN ? != '' THEN ? ELSE device_info END
                WHERE UPPER(student_id) = ?
            """, (
                json.dumps(clean),
                clean["q1_1"], clean["q1_2"], clean["q1_3"], clean["q1_4"], clean["q1_5"],
                clean["q1_6"], clean["q1_7"], clean["q1_8"], clean["q1_9"], clean["q1_10"],
                clean["q1_avg"],
                clean["q2_1"], clean["q2_2"], clean["q2_3"],
                clean["q3_1"], clean["q3_2"], clean["q3_3"],
                now_iso, now_iso, ip_address, ip_address, device_info, device_info, sid
            ))
        else:
            cursor.execute("""
                INSERT INTO evaluations (
                    student_id, status, draft_json,
                    q1_1, q1_2, q1_3, q1_4, q1_5, q1_6, q1_7, q1_8, q1_9, q1_10, q1_avg,
                    q2_1, q2_2, q2_3, q3_1, q3_2, q3_3,
                    is_auto_tallied, active_session_id, lock_expires_at,
                    updated_at, submitted_at, ip_address, device_info
                ) VALUES (?, 'SUBMITTED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?)
            """, (
                attendee["student_id"], json.dumps(clean),
                clean["q1_1"], clean["q1_2"], clean["q1_3"], clean["q1_4"], clean["q1_5"],
                clean["q1_6"], clean["q1_7"], clean["q1_8"], clean["q1_9"], clean["q1_10"],
                clean["q1_avg"],
                clean["q2_1"], clean["q2_2"], clean["q2_3"],
                clean["q3_1"], clean["q3_2"], clean["q3_3"],
                now_iso, now_iso, ip_address, device_info
            ))

        cursor.execute("UPDATE attendees SET eval_status = 'SUBMITTED' WHERE UPPER(student_id) = ?", (sid,))
        elig_info = recompute_attendee_eligibility(cursor, sid, min_dur)

        conn.commit()
        conn.close()

        if elig_info["is_eligible"]:
            msg = "Thank you! Your evaluation has been submitted and your Certificate of Participation is APPROVED."
        else:
            msg = "Thank you! Your evaluation has been submitted. Complete your Time-Out meeting the required seminar duration to unlock certificate approval."

        return {
            "status": "success",
            "message": msg,
            "student_id": attendee["student_id"],
            "student_name": attendee["full_name"],
            "submitted_at": now_iso,
            "submitted_at_fmt": format_time_str(now_iso),
            "q1_avg": clean["q1_avg"],
            "attendance_eligible": elig_info["attendance_eligible"],
            "is_eligible": elig_info["is_eligible"],
            "cert_status": elig_info["cert_status"]
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return {"status": "error", "message": f"Failed to submit evaluation: {str(e)}"}

def finalize_and_tally_incomplete_evaluations() -> Dict[str, Any]:
    """
    End-of-day Admin action: Finds all IN_PROGRESS evaluations where the student has answered
    at least 1 item (rating or text), finalizes them as SUBMITTED (is_auto_tallied = 1),
    releases any active session locks, and recomputes certificate eligibility for all attendees.
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        min_dur_row = cursor.execute("SELECT value FROM settings WHERE key = 'min_duration_minutes'").fetchone()
        min_dur = float(min_dur_row["value"]) if min_dur_row else 150.0

        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        in_prog_rows = cursor.execute("SELECT * FROM evaluations WHERE status = 'IN_PROGRESS'").fetchall()

        tallied_count = 0
        newly_eligible_count = 0

        for er in in_prog_rows:
            sid = er["student_id"]
            try:
                draft = json.loads(er["draft_json"] or "{}")
            except Exception:
                draft = {}
            clean = _sanitize_draft_payload(draft)

            if clean["answered_count"] > 0:
                cursor.execute("""
                    UPDATE evaluations SET
                        status = 'SUBMITTED',
                        q1_1 = ?, q1_2 = ?, q1_3 = ?, q1_4 = ?, q1_5 = ?,
                        q1_6 = ?, q1_7 = ?, q1_8 = ?, q1_9 = ?, q1_10 = ?,
                        q1_avg = ?,
                        q2_1 = ?, q2_2 = ?, q2_3 = ?,
                        q3_1 = ?, q3_2 = ?, q3_3 = ?,
                        is_auto_tallied = 1,
                        active_session_id = NULL,
                        lock_expires_at = NULL,
                        updated_at = ?,
                        submitted_at = COALESCE(submitted_at, ?)
                    WHERE UPPER(student_id) = ?
                """, (
                    clean["q1_1"], clean["q1_2"], clean["q1_3"], clean["q1_4"], clean["q1_5"],
                    clean["q1_6"], clean["q1_7"], clean["q1_8"], clean["q1_9"], clean["q1_10"],
                    clean["q1_avg"],
                    clean["q2_1"], clean["q2_2"], clean["q2_3"],
                    clean["q3_1"], clean["q3_2"], clean["q3_3"],
                    now_iso, now_iso, sid.upper()
                ))
                cursor.execute("UPDATE attendees SET eval_status = 'SUBMITTED' WHERE UPPER(student_id) = ?", (sid.upper(),))
                elig_info = recompute_attendee_eligibility(cursor, sid, min_dur)
                tallied_count += 1
                if elig_info["is_eligible"]:
                    newly_eligible_count += 1

        # Recompute eligibility for all attendees to ensure consistency
        all_sids = cursor.execute("SELECT student_id FROM attendees").fetchall()
        for r in all_sids:
            recompute_attendee_eligibility(cursor, r["student_id"], min_dur)

        conn.commit()
        conn.close()
        return {
            "status": "success",
            "tallied_count": tallied_count,
            "newly_eligible_count": newly_eligible_count,
            "message": f"Tallied and finalized {tallied_count} in-progress evaluation(s). ({newly_eligible_count} newly qualified for Certificate)."
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return {"status": "error", "message": f"Error tallying evaluations: {str(e)}"}

def get_passes_live_status(student_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Returns real-time attendance & evaluation status for a list of student IDs on a phone in a single batched query."""
    if not student_ids:
        return {}
    clean_ids = []
    seen = set()
    for raw_sid in student_ids[:30]:
        sid = (raw_sid or "").strip().upper()
        if sid and sid not in seen:
            seen.add(sid)
            clean_ids.append(sid)
    if not clean_ids:
        return {}

    min_dur = float(get_setting("min_duration_minutes", "150") or "150")
    placeholders = ",".join("?" for _ in clean_ids)
    conn = get_db()
    rows = conn.cursor().execute(f"""
        SELECT
            a.student_id, a.full_name, a.section,
            a.morning_out, a.morning_duration_min,
            a.afternoon_out, a.afternoon_duration_min,
            a.attendance_eligible, a.eval_status, a.is_eligible, a.cert_status,
            e.active_session_id, e.lock_expires_at, e.draft_json
        FROM attendees a
        LEFT JOIN evaluations e ON UPPER(a.student_id) = UPPER(e.student_id)
        WHERE UPPER(a.student_id) IN ({placeholders})
    """, clean_ids).fetchall()
    conn.close()

    result: Dict[str, Dict[str, Any]] = {}
    now = datetime.now()
    for row in rows:
        is_locked = False
        lock_holder = None
        answered_count = 0
        if row["lock_expires_at"] or row["draft_json"]:
            is_locked, _ = _is_lock_active(row["lock_expires_at"], now)
            lock_holder = row["active_session_id"] if is_locked else None
            try:
                d = json.loads(row["draft_json"] or "{}")
                answered_count = int(d.get("answered_count", 0))
            except Exception:
                answered_count = 0

        m_dur = float(row["morning_duration_min"] or 0) if row["morning_out"] else 0.0
        a_dur = float(row["afternoon_duration_min"] or 0) if row["afternoon_out"] else 0.0
        has_timeout = bool(row["morning_out"] or row["afternoon_out"])
        total_dur = round(m_dur + a_dur)

        result[row["student_id"]] = {
            "student_id": row["student_id"],
            "student_name": row["full_name"],
            "section": row["section"],
            "has_timeout": has_timeout,
            "duration_min": total_dur,
            "min_duration": int(min_dur),
            "attendance_eligible": bool(row["attendance_eligible"]),
            "eval_status": row["eval_status"] or "PENDING",
            "eval_answered_count": answered_count,
            "eval_locked": is_locked,
            "eval_lock_holder": lock_holder,
            "is_eligible": bool(row["is_eligible"]),
            "cert_status": row["cert_status"] or "PENDING"
        }
    return result

def get_all_evaluations_for_export() -> List[Dict[str, Any]]:
    """Returns joined attendee + evaluation records for CSV export and admin inspection."""
    conn = get_db()
    rows = conn.cursor().execute("""
        SELECT
            a.student_id, a.last_name, a.first_name, a.middle_name, a.full_name,
            a.sex, a.age, a.level, a.shs_grade, a.strand, a.college_year, a.course,
            a.section, a.email,
            a.morning_in, a.morning_out, a.morning_duration_min,
            a.afternoon_in, a.afternoon_out, a.afternoon_duration_min,
            a.attendance_eligible, a.eval_status, a.is_eligible, a.cert_status,
            e.status as e_status,
            e.q1_1, e.q1_2, e.q1_3, e.q1_4, e.q1_5,
            e.q1_6, e.q1_7, e.q1_8, e.q1_9, e.q1_10, e.q1_avg,
            e.q2_1, e.q2_2, e.q2_3,
            e.q3_1, e.q3_2, e.q3_3,
            e.is_auto_tallied, e.updated_at, e.submitted_at
        FROM evaluations e
        INNER JOIN attendees a ON UPPER(a.student_id) = UPPER(e.student_id)
        ORDER BY COALESCE(e.submitted_at, e.updated_at) DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_statistics() -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    
    # Single-pass conditional aggregation query on attendees
    cursor.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(morning_in) as morning_in,
            COUNT(morning_out) as morning_out,
            COUNT(afternoon_in) as afternoon_in,
            COUNT(afternoon_out) as afternoon_out,
            COALESCE(SUM(CASE WHEN attendance_eligible = 1 THEN 1 ELSE 0 END), 0) as attendance_eligible,
            COALESCE(SUM(CASE WHEN eval_status = 'SUBMITTED' THEN 1 ELSE 0 END), 0) as eval_submitted,
            COALESCE(SUM(CASE WHEN eval_status = 'IN_PROGRESS' THEN 1 ELSE 0 END), 0) as eval_in_progress,
            COALESCE(SUM(CASE WHEN is_eligible = 1 THEN 1 ELSE 0 END), 0) as eligible,
            COALESCE(SUM(CASE WHEN LOWER(sex) = 'male' THEN 1 ELSE 0 END), 0) as male,
            COALESCE(SUM(CASE WHEN LOWER(sex) = 'female' THEN 1 ELSE 0 END), 0) as female,
            COALESCE(SUM(CASE WHEN level = 'SHS' THEN 1 ELSE 0 END), 0) as shs,
            COALESCE(SUM(CASE WHEN level = 'COLLEGE' THEN 1 ELSE 0 END), 0) as college,
            COALESCE(SUM(CASE WHEN level = 'NON_STUDENT' THEN 1 ELSE 0 END), 0) as non_student,
            COALESCE(SUM(CASE WHEN level = 'SHS' AND shs_grade = 'Grade 11' THEN 1 ELSE 0 END), 0) as shs_g11,
            COALESCE(SUM(CASE WHEN level = 'SHS' AND shs_grade = 'Grade 12' THEN 1 ELSE 0 END), 0) as shs_g12
        FROM attendees
    """)
    row = cursor.fetchone()
    
    total = row["total"]
    male_count = row["male"]
    female_count = row["female"]
    other_sex_count = total - (male_count + female_count)
    
    # Evaluation ratings aggregation (across SUBMITTED evaluations, plus count of answerable drafts)
    eval_agg = cursor.execute("""
        SELECT
            COUNT(CASE WHEN status = 'SUBMITTED' THEN 1 END) as submitted_cnt,
            COUNT(CASE WHEN status = 'IN_PROGRESS' AND draft_json != '{}' THEN 1 END) as draft_cnt,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' AND q1_avg > 0 THEN q1_avg END), 2) as overall_avg,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_1 END), 2) as avg_1,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_2 END), 2) as avg_2,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_3 END), 2) as avg_3,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_4 END), 2) as avg_4,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_5 END), 2) as avg_5,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_6 END), 2) as avg_6,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_7 END), 2) as avg_7,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_8 END), 2) as avg_8,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_9 END), 2) as avg_9,
            ROUND(AVG(CASE WHEN status = 'SUBMITTED' THEN q1_10 END), 2) as avg_10
        FROM evaluations
    """).fetchone()

    # Grouped breakdowns
    strand_rows = cursor.execute("""
        SELECT strand, COUNT(*) as cnt 
        FROM attendees 
        WHERE level = 'SHS' AND strand IS NOT NULL AND strand != ''
        GROUP BY strand 
        ORDER BY cnt DESC
    """).fetchall()
    strand_stats = {r["strand"]: r["cnt"] for r in strand_rows}
    
    course_rows = cursor.execute("""
        SELECT course, COUNT(*) as cnt 
        FROM attendees 
        WHERE level = 'COLLEGE' AND course IS NOT NULL AND course != ''
        GROUP BY course 
        ORDER BY cnt DESC
    """).fetchall()
    course_stats = {r["course"]: r["cnt"] for r in course_rows}
    
    conn.close()
    
    return {
        "total": total,
        "morning_in": row["morning_in"],
        "morning_out": row["morning_out"],
        "afternoon_in": row["afternoon_in"],
        "afternoon_out": row["afternoon_out"],
        "attendance_eligible": row["attendance_eligible"],
        "eval_submitted": row["eval_submitted"],
        "eval_in_progress": eval_agg["draft_cnt"] if eval_agg else row["eval_in_progress"],
        "eligible": row["eligible"],
        "evaluations": {
            "submitted": eval_agg["submitted_cnt"] if eval_agg else 0,
            "in_progress": eval_agg["draft_cnt"] if eval_agg else 0,
            "overall_avg": eval_agg["overall_avg"] or 0.0,
            "criteria_avgs": [
                eval_agg[f"avg_{i}"] or 0.0 for i in range(1, 11)
            ] if eval_agg else [0.0] * 10
        },
        "sex": {
            "male": male_count,
            "female": female_count,
            "other": other_sex_count
        },
        "level": {
            "shs": row["shs"],
            "college": row["college"],
            "non_student": row["non_student"],
            "shs_g11": row["shs_g11"],
            "shs_g12": row["shs_g12"]
        },
        "strands": strand_stats,
        "courses": course_stats
    }

def get_all_attendees(search: Optional[str] = None, filter_level: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db()
    query = """
        SELECT a.*, e.q1_avg as eval_avg, e.is_auto_tallied as eval_auto_tallied
        FROM attendees a
        LEFT JOIN evaluations e ON UPPER(a.student_id) = UPPER(e.student_id)
        WHERE 1=1
    """
    params = []
    
    if search:
        query += " AND (a.student_id LIKE ? OR a.full_name LIKE ? OR a.last_name LIKE ? OR a.first_name LIKE ? OR a.email LIKE ? OR a.section LIKE ? OR a.ip_address LIKE ? OR a.device_info LIKE ?)"
        s = f"%{search}%"
        params.extend([s, s, s, s, s, s, s, s])
        
    if filter_level and filter_level in ("SHS", "COLLEGE", "NON_STUDENT"):
        query += " AND a.level = ?"
        params.append(filter_level)
        
    query += " ORDER BY a.id DESC"
    rows = conn.cursor().execute(query, params).fetchall()
    conn.close()
    
    attendees = []
    for r in rows:
        d = dict(r)
        d["morning_in_fmt"] = format_time_str(d.get("morning_in"))
        d["morning_out_fmt"] = format_time_str(d.get("morning_out"))
        d["afternoon_in_fmt"] = format_time_str(d.get("afternoon_in"))
        d["afternoon_out_fmt"] = format_time_str(d.get("afternoon_out"))
        d["eval_status"] = d.get("eval_status") or "PENDING"
        
        # Primary session time display
        if d.get("morning_in"):
            d["primary_in_fmt"] = d["morning_in_fmt"]
            d["primary_out_fmt"] = d["morning_out_fmt"] or "-"
            d["primary_duration_min"] = round(d.get("morning_duration_min") or 0)
        elif d.get("afternoon_in"):
            d["primary_in_fmt"] = d["afternoon_in_fmt"]
            d["primary_out_fmt"] = d["afternoon_out_fmt"] or "-"
            d["primary_duration_min"] = round(d.get("afternoon_duration_min") or 0)
        else:
            d["primary_in_fmt"] = "-"
            d["primary_out_fmt"] = "-"
            d["primary_duration_min"] = 0
            
        attendees.append(d)
    return attendees

def get_sections_list() -> List[str]:
    conn = get_db()
    rows = conn.cursor().execute("SELECT DISTINCT section FROM attendees WHERE section != '' ORDER BY section ASC").fetchall()
    conn.close()
    return [r["section"] for r in rows]

def admin_manual_add_attendee(data: Dict[str, Any]) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    student_id = data["student_id"].strip().upper()
    last_name = data["last_name"].strip()
    first_name = data["first_name"].strip()
    middle_name = (data.get("middle_name") or "N/A").strip()
    full_name = compute_full_name(last_name, first_name, middle_name)
    sex = data["sex"].strip()
    age = int(data["age"])
    email = data["email"].strip().lower()
    level = normalize_level(data["level"])
    section = data["section"].strip()
    shs_grade, strand, college_year, course = infer_academic_details(
        level, section,
        data.get("shs_grade"), data.get("strand"),
        data.get("college_year"), data.get("course")
    )
    phase = data.get("phase", "MORNING_IN")
    time_stamp = data.get("time_in") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    existing = cursor.execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (student_id,)).fetchone()
    if existing:
        conn.close()
        return {"status": "error", "message": f"Student ID '{student_id}' already exists in records."}

    morning_in = time_stamp if "MORNING" in phase else None
    afternoon_in = time_stamp if "AFTERNOON" in phase else None

    cursor.execute("""
        INSERT INTO attendees (
            student_id, last_name, first_name, middle_name, full_name,
            sex, age, email, level, shs_grade, strand, college_year, course, section,
            ip_address, device_info, morning_in, afternoon_in
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        student_id, last_name, first_name, middle_name, full_name,
        sex, age, email, level, shs_grade, strand,
        college_year, course, section,
        "MANUAL_ENTRY", "Admin Dashboard", morning_in, afternoon_in
    ))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Successfully registered attendee {full_name} manually.", "student_id": student_id, "student_name": full_name, "time": time_stamp}

def admin_edit_attendee(student_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    sid = student_id.strip().upper()
    row = cursor.execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (sid,)).fetchone()
    if not row:
        conn.close()
        return {"status": "error", "message": f"Student ID '{sid}' not found."}

    last_name = data.get("last_name", row["last_name"]).strip()
    first_name = data.get("first_name", row["first_name"]).strip()
    middle_name = (data.get("middle_name") or row["middle_name"] or "N/A").strip()
    full_name = compute_full_name(last_name, first_name, middle_name)
    sex = data.get("sex", row["sex"]).strip()
    age = int(data.get("age", row["age"]))
    email = data.get("email", row["email"]).strip()
    level = normalize_level(data.get("level", row["level"]))
    section = data.get("section", row["section"]).strip()
    shs_grade, strand, college_year, course = infer_academic_details(
        level, section,
        data.get("shs_grade") or row["shs_grade"],
        data.get("strand") or row["strand"],
        data.get("college_year") or row["college_year"],
        data.get("course") or row["course"]
    )
    morning_in = data.get("morning_in") if data.get("morning_in") is not None else row["morning_in"]
    morning_out = data.get("morning_out") if data.get("morning_out") is not None else row["morning_out"]
    afternoon_in = data.get("afternoon_in") if data.get("afternoon_in") is not None else row["afternoon_in"]
    afternoon_out = data.get("afternoon_out") if data.get("afternoon_out") is not None else row["afternoon_out"]
    eval_status = (data.get("eval_status") or row["eval_status"] or "PENDING").strip().upper()
    is_eligible = int(data.get("is_eligible", row["is_eligible"]))

    # Recompute durations if both in and out are present; reset to 0 if out was cleared
    m_dur = 0.0
    if morning_in and morning_out:
        try:
            t1 = datetime.strptime(morning_in, "%Y-%m-%d %H:%M:%S")
            t2 = datetime.strptime(morning_out, "%Y-%m-%d %H:%M:%S")
            m_dur = max(0.0, round((t2 - t1).total_seconds() / 60, 1))
        except Exception:
            m_dur = float(row["morning_duration_min"] or 0)

    a_dur = 0.0
    if afternoon_in and afternoon_out:
        try:
            t1 = datetime.strptime(afternoon_in, "%Y-%m-%d %H:%M:%S")
            t2 = datetime.strptime(afternoon_out, "%Y-%m-%d %H:%M:%S")
            a_dur = max(0.0, round((t2 - t1).total_seconds() / 60, 1))
        except Exception:
            a_dur = float(row["afternoon_duration_min"] or 0)

    min_dur = float(get_setting("min_duration_minutes", "150") or "150")
    has_timeout = bool(morning_out or afternoon_out)
    att_eligible = 1 if (has_timeout and (m_dur + a_dur) >= min_dur) else 0
    if is_eligible == 1:
        cert_status = "APPROVED" if (att_eligible == 1 and eval_status == "SUBMITTED") else "MANUAL_APPROVED"
    else:
        cert_status = "PENDING"

    cursor.execute("""
        UPDATE attendees SET
            last_name = ?, first_name = ?, middle_name = ?, full_name = ?,
            sex = ?, age = ?, email = ?, level = ?,
            shs_grade = ?, strand = ?, college_year = ?, course = ?, section = ?,
            morning_in = ?, morning_out = ?, morning_duration_min = ?,
            afternoon_in = ?, afternoon_out = ?, afternoon_duration_min = ?,
            attendance_eligible = ?, eval_status = ?, is_eligible = ?, cert_status = ?
        WHERE UPPER(student_id) = ?
    """, (
        last_name, first_name, middle_name, full_name,
        sex, age, email, level,
        shs_grade, strand, college_year, course, section,
        morning_in, morning_out, m_dur,
        afternoon_in, afternoon_out, a_dur,
        att_eligible, eval_status, is_eligible, cert_status, sid
    ))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Updated record for {full_name}."}

def admin_delete_attendee(student_id: str) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    sid = student_id.strip().upper()
    row = cursor.execute("SELECT * FROM attendees WHERE UPPER(student_id) = ?", (sid,)).fetchone()
    if not row:
        conn.close()
        return {"status": "error", "message": f"Student ID '{sid}' not found."}
    name = row["full_name"]
    cursor.execute("DELETE FROM attendees WHERE UPPER(student_id) = ?", (sid,))
    cursor.execute("DELETE FROM evaluations WHERE UPPER(student_id) = ?", (sid,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Deleted attendee record for {name} ({sid})."}
