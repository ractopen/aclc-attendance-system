import os
import sys
import time
import statistics
import tempfile
import sqlite3

# Ensure app modules are importable
current_dir = os.path.dirname(os.path.abspath(__file__))
app_dir = os.path.dirname(current_dir)
sys.path.insert(0, app_dir)

# Override DB_PATH to isolated temporary test database
import database
TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f"aclc_test_{int(time.time())}.db")
database.DB_PATH = TEST_DB_PATH

from main import app
from fastapi.testclient import TestClient

def run_performance_and_unit_tests():
    print("=" * 65)
    print("ACLC Attendance System: Unit Test & Latency Benchmark Suite")
    print(f"Isolated Test Database: {TEST_DB_PATH}")
    print("=" * 65)

    database.init_db()
    client = TestClient(app)

    try:
        # ---------------------------------------------------------
        # 1. UNIT TESTS: Functional Logic Verification
        # ---------------------------------------------------------
        print("\n[Phase 1] Executing Functional Unit Tests...")

        # Test A: Basic Student Registration (SHS)
        shs_payload = {
            "student_id": "2024-SHS-001",
            "last_name": "Dela Cruz",
            "first_name": "Juan",
            "middle_name": "Reyes",
            "sex": "Male",
            "age": 17,
            "email": "juan.cruz@aclc.edu.ph",
            "level": "SHS",
            "shs_grade": "Grade 11",
            "strand": "TVL-ICT",
            "section": "11-ICT-A"
        }
        r = client.post("/api/attendance/in", json=shs_payload)
        assert r.status_code == 200 and r.json()["status"] == "success", f"SHS Check-In failed: {r.text}"
        print("  [PASS] SHS Student Check-In (Grade 11 TVL-ICT)")

        # Test B: College Registration with Empty Middle Name (Auto N/A) & Smart Section Inference
        college_payload = {
            "student_id": "2024-COL-002",
            "last_name": "Santos",
            "first_name": "Maria Clara",
            "middle_name": "",
            "sex": "Female",
            "age": 19,
            "email": "maria.santos@aclc.edu.ph",
            "level": "COLLEGE",
            "section": "BSIT-2B"
        }
        r = client.post("/api/attendance/in", json=college_payload)
        assert r.status_code == 200 and r.json()["status"] == "success"
        attendees = database.get_all_attendees()
        maria = [a for a in attendees if a["student_id"] == "2024-COL-002"][0]
        assert maria["middle_name"] == "N/A", "Empty middle name should default to N/A"
        assert maria["course"] == "BSIT" and maria["college_year"] == "2nd Year", f"Expected inferred BSIT 2nd Year, got {maria['course']} {maria['college_year']}"
        print("  [PASS] College Check-In with Auto N/A Middle Name & Smart Section Inference")

        # Test C: Guest / Non-Student Registration (Role instead of Section)
        guest_payload = {
            "student_id": "FACULTY-001",
            "last_name": "Prof. Alcantara",
            "first_name": "Eduardo",
            "middle_name": "N/A",
            "sex": "Male",
            "age": 38,
            "email": "eduardo.alcantara@aclc.edu.ph",
            "level": "NON_STUDENT",
            "section": "College Dean / Keynote Speaker"
        }
        r = client.post("/api/attendance/in", json=guest_payload)
        assert r.status_code == 200 and r.json()["status"] == "success"
        print("  [PASS] Guest / Faculty Check-In with Custom Role")

        # Test Age Boundaries (Min 10, Max 120)
        age_10_payload = dict(guest_payload)
        age_10_payload["student_id"] = "2024-AGE-10"
        age_10_payload["age"] = 10
        r_10 = client.post("/api/attendance/in", json=age_10_payload)
        assert r_10.status_code == 200

        age_120_payload = dict(guest_payload)
        age_120_payload["student_id"] = "2024-AGE-120"
        age_120_payload["age"] = 120
        r_120 = client.post("/api/attendance/in", json=age_120_payload)
        assert r_120.status_code == 200

        # Invalid ages (< 10 or > 120) should be rejected with clean human messages
        age_low = dict(guest_payload)
        age_low["student_id"] = "2024-AGE-LOW"
        age_low["age"] = 9
        r_low = client.post("/api/attendance/in", json=age_low)
        assert r_low.status_code == 422 and "Age must be at least 10" in r_low.json()["message"]

        age_high = dict(guest_payload)
        age_high["student_id"] = "2024-AGE-HIGH"
        age_high["age"] = 121
        r_high = client.post("/api/attendance/in", json=age_high)
        assert r_high.status_code == 422 and "Age cannot exceed 120" in r_high.json()["message"]

        # Extreme overflow bypass attempt (e.g. 999999999999999)
        age_extreme = dict(guest_payload)
        age_extreme["student_id"] = "2024-AGE-EXTREME"
        age_extreme["age"] = 999999999999999
        r_extreme = client.post("/api/attendance/in", json=age_extreme)
        assert r_extreme.status_code == 422 and "Age cannot exceed 120" in r_extreme.json()["message"]

        # Invalid Email bypass check
        bad_email = dict(guest_payload)
        bad_email["student_id"] = "2024-BAD-EMAIL"
        bad_email["email"] = "notanemail"
        r_email = client.post("/api/attendance/in", json=bad_email)
        assert r_email.status_code == 422 and "valid active email" in r_email.json()["message"]

        # XSS Script Injection Sanitization Check
        xss_payload = dict(guest_payload)
        xss_payload["student_id"] = "<script>2024-XSS</script>"
        xss_payload["last_name"] = "<b>Hacker</b>"
        r_xss = client.post("/api/attendance/in", json=xss_payload)
        assert r_xss.status_code == 200
        saved_attendees = database.get_all_attendees()
        xss_saved = [a for a in saved_attendees if "2024-XSS" in a["student_id"]][0]
        assert "<script>" not in xss_saved["student_id"], "Script tags must be stripped"
        assert "<b>" not in xss_saved["last_name"], "HTML tags must be stripped"
        print("  [PASS] Comprehensive Edge-Case & Bypass Audit (Age, Email, XSS, Overflow)")

        # Test D: Duplicate Prevention & Case Normalization
        dup_payload = dict(shs_payload)
        dup_payload["student_id"] = "  2024-shs-001  " # Lowercase with spaces
        r = client.post("/api/attendance/in", json=dup_payload)
        assert r.status_code == 200 and r.json()["status"] == "already_timed_in"
        print("  [PASS] Duplicate Prevention & ID Case Normalization")

        # Test E: Hardened Time In / Time Out & Mutual Exclusivity
        import main
        # 0. Unauthenticated Time-Out is rejected (no student self time-out)
        database.set_setting("current_phase", "MORNING_OUT")
        r_unauth_out = client.post("/api/attendance/out", json={"student_id": "2024-SHS-001"})
        assert r_unauth_out.status_code == 401
        print("  [PASS] Student Self Time-Out Blocked (Admin Scanner Auth Required)")

        # Authenticate as Admin for Scanner operations
        admin_cookie = main.get_auth_secret()
        client.cookies.set("aclc_admin_session", admin_cookie)

        # 1. When in MORNING_IN, Time-Out is strictly closed
        database.set_setting("current_phase", "MORNING_IN")
        r_timeout_blocked = client.post("/api/attendance/out", json={"student_id": "2024-SHS-001"})
        assert r_timeout_blocked.json()["status"] == "error" and "not open" in r_timeout_blocked.json()["message"].lower()

        # 2. Switch to MORNING_OUT -> Time-In is strictly closed
        database.set_setting("current_phase", "MORNING_OUT")
        r_timein_blocked = client.post("/api/attendance/in", json=shs_payload)
        assert r_timein_blocked.json()["status"] == "error" and "closed" in r_timein_blocked.json()["message"].lower()
        print("  [PASS] Strict Schedule Mutual Exclusivity (In vs Out Mutual Lock)")

        # 3. Test Salted HMAC-SHA256 QR Pass Verification (ACLC:<ID>:<TOKEN>)
        r_forged = client.post("/api/attendance/out", json={"student_id": "ACLC:2024-SHS-001:FORGED9999"})
        assert r_forged.json()["status"] == "error" and "forged" in r_forged.json()["message"].lower()

        valid_token = main.compute_pass_token("2024-SHS-001")
        r = client.post("/api/attendance/out", json={"student_id": f"ACLC:2024-SHS-001:{valid_token}"})
        assert r.status_code == 200 and r.json()["status"] == "success", f"Time-Out failed: {r.text}"
        assert r.json()["is_eligible"] == False, "Immediate time-out should be ineligible (<150m)"
        print("  [PASS] Salted HMAC-SHA256 QR Verification & Scanner Time Out Verified")

        # Verify default min_duration_minutes is 150 and eval_open is False at server start
        r_init_live = client.get("/api/kiosk-live").json()
        assert r_init_live["min_duration"] == 150, f"Expected default 150 min, got {r_init_live['min_duration']}"
        assert r_init_live["eval_open"] is False, "Evaluation must default to Closed at server start"
        print("  [PASS] Server Start Defaults Verified (150 mins duration & Evaluation Closed)")

        # Strict Morning vs Afternoon session separation + Void unclosed Morning In + Admin/Officer QR Time-In
        # Maria (2024-COL-002) timed in during MORNING_IN and did NOT scan MORNING_OUT.
        # 1. Attempting AFTERNOON_OUT without AFTERNOON_IN must be rejected (no cross-session fallback).
        database.set_setting("current_phase", "AFTERNOON_OUT")
        r_no_aft_in = client.post("/api/attendance/out", json={"student_id": "2024-COL-002"})
        assert r_no_aft_in.status_code == 200 and r_no_aft_in.json()["status"] == "error"

        # 2. Switching via /api/admin/set-phase to AFTERNOON_IN voids unclosed Morning In (Maria)
        #    while preserving completed Morning attendance (2024-SHS-001).
        r_phase_aft = client.post("/api/admin/set-phase", json={"phase": "AFTERNOON_IN"})
        assert r_phase_aft.status_code == 200 and r_phase_aft.json()["voided_morning_count"] >= 1
        conn = database.get_db()
        maria_row = conn.execute("SELECT morning_in, morning_voided FROM attendees WHERE student_id = '2024-COL-002'").fetchone()
        shs_row = conn.execute("SELECT morning_in, morning_out, morning_voided FROM attendees WHERE student_id = '2024-SHS-001'").fetchone()
        conn.close()
        assert maria_row["morning_in"] is None and maria_row["morning_voided"] == 1
        assert shs_row["morning_in"] is not None and shs_row["morning_out"] is not None and shs_row["morning_voided"] == 0

        # 3. Officer/Admin scans Maria's QR pass on Scanner during AFTERNOON_IN to record Afternoon Time In
        maria_tok = main.compute_pass_token("2024-COL-002")
        r_scan_in = client.post("/api/admin/scanner-time-in", json={"student_id": f"ACLC:2024-COL-002:{maria_tok}"})
        assert r_scan_in.status_code == 200 and r_scan_in.json()["status"] == "success"

        # Back-date Maria's afternoon_in to 160 minutes ago (>= 150m) and scan AFTERNOON_OUT
        conn = database.get_db()
        conn.execute("UPDATE attendees SET afternoon_in = datetime('now', '-160 minutes') WHERE student_id = '2024-COL-002'")
        conn.commit()
        conn.close()

        database.set_setting("current_phase", "AFTERNOON_OUT")
        r = client.post("/api/attendance/out", json={"student_id": "2024-COL-002"})
        assert r.status_code == 200 and r.json()["status"] == "success"
        assert r.json()["attendance_eligible"] == True, "160-min afternoon attendance should meet duration requirement"
        assert r.json()["is_eligible"] == False, "Certificate requires BOTH >=150m duration AND Evaluation submission"
        print("  [PASS] Strict Session Split, Unclosed Morning Voiding & Scanner QR Time-In Verified")

        # Test Seminar Evaluation Concurrency Lock, Auto-Save, Release, Submit & Admin Tally
        maria_tok = main.compute_pass_token("2024-COL-002")
        # 1. While eval_open is False, opening quiz is blocked
        r_closed_eval = client.post("/api/evaluation/open", json={
            "student_id": "2024-COL-002",
            "session_id": "SESS-DEVICE-1",
            "token": maria_tok
        })
        assert r_closed_eval.json()["status"] == "eval_closed"

        # 2. Admin opens Evaluation during lecture
        r_open_toggle = client.post("/api/admin/set-eval-open", json={"eval_open": True})
        assert r_open_toggle.status_code == 200 and r_open_toggle.json()["eval_open"] is True

        # 3. Device 1 opens Maria's evaluation -> Acquires lock
        r_dev1_open = client.post("/api/evaluation/open", json={
            "student_id": "2024-COL-002",
            "session_id": "SESS-DEVICE-1",
            "token": maria_tok
        })
        assert r_dev1_open.status_code == 200 and r_dev1_open.json()["status"] == "success"

        # 4. Device 2 tries to open Maria's evaluation simultaneously -> Blocked (409 Lock Priority)
        r_dev2_open = client.post("/api/evaluation/open", json={
            "student_id": "2024-COL-002",
            "session_id": "SESS-DEVICE-2",
            "token": maria_tok
        })
        assert r_dev2_open.status_code == 409 and r_dev2_open.json()["status"] == "locked"
        print("  [PASS] Multi-Device Concurrency Lock (First Active Session Priority Verified)")

        # 5. Device 1 auto-saves partial answers, then closes modal (releases lock)
        partial_draft = {
            "q1_1": 5, "q1_2": 5, "q1_3": 4,
            "q2_1": "Learned about HIV prevention and testing."
        }
        r_save = client.post("/api/evaluation/autosave", json={
            "student_id": "2024-COL-002",
            "session_id": "SESS-DEVICE-1",
            "token": maria_tok,
            "draft": partial_draft
        })
        assert r_save.status_code == 200 and r_save.json()["status"] == "success"

        r_rel = client.post("/api/evaluation/release", json={
            "student_id": "2024-COL-002",
            "session_id": "SESS-DEVICE-1",
            "token": maria_tok,
            "draft": partial_draft
        })
        assert r_rel.status_code == 200

        # 6. Reopening on Device 2 now succeeds and restores the auto-saved draft
        r_reopen = client.post("/api/evaluation/open", json={
            "student_id": "2024-COL-002",
            "session_id": "SESS-DEVICE-2",
            "token": maria_tok
        })
        assert r_reopen.status_code == 200 and r_reopen.json()["draft"]["q1_1"] == 5
        assert "HIV prevention" in r_reopen.json()["draft"]["q2_1"]
        print("  [PASS] Auto-Save Draft Persistence Across Close & Reopen Verified")

        # 7. Complete and submit Maria's evaluation -> Locks out account & approves certificate
        full_answers = {
            "q1_1": 5, "q1_2": 5, "q1_3": 5, "q1_4": 5, "q1_5": 5,
            "q1_6": 4, "q1_7": 5, "q1_8": 5, "q1_9": 5, "q1_10": 5,
            "q2_1": "HIV transmission modes and modern prevention.",
            "q2_2": "HIV is not transmitted through casual contact.",
            "q2_3": "Promote awareness, respect, and regular testing.",
            "q3_1": "The Q&A discussion on stigma reduction.",
            "q3_2": "PrEP/PEP availability and community testing centers.",
            "q3_3": "More interactive case studies."
        }
        r_sub = client.post("/api/evaluation/submit", json={
            "student_id": "2024-COL-002",
            "session_id": "SESS-DEVICE-2",
            "token": maria_tok,
            "answers": full_answers
        })
        assert r_sub.status_code == 200 and r_sub.json()["status"] == "success"
        assert r_sub.json()["is_eligible"] is True and r_sub.json()["cert_status"] == "APPROVED"

        # Verify Maria is now locked out from reopening
        r_locked_out = client.post("/api/evaluation/open", json={
            "student_id": "2024-COL-002",
            "session_id": "SESS-DEVICE-2",
            "token": maria_tok
        })
        assert r_locked_out.json()["status"] == "already_submitted"
        print("  [PASS] Evaluation Submission, Account Lockout & Certificate Approval Verified")

        # 8. Test End-of-Day Admin Tally for Unfinished In-Progress Drafts (e.g. 2024-SHS-001)
        shs_tok = main.compute_pass_token("2024-SHS-001")
        client.post("/api/evaluation/open", json={
            "student_id": "2024-SHS-001",
            "session_id": "SESS-SHS-1",
            "token": shs_tok
        })
        client.post("/api/evaluation/autosave", json={
            "student_id": "2024-SHS-001",
            "session_id": "SESS-SHS-1",
            "token": shs_tok,
            "draft": {"q1_1": 5, "q1_2": 4, "q2_1": "Partial answer before leaving"}
        })
        r_tally = client.post("/api/admin/tally-evaluations")
        assert r_tally.status_code == 200 and r_tally.json()["tallied_count"] == 1
        print("  [PASS] Admin End-of-Day Tally & Finalization of Unfinished Drafts Verified")

        # 8b. Dynamic Quiz Schema Edge Cases: Disabled question, custom question, empty schema block, _ts & cleared draft count
        r_empty_schema = client.post("/api/admin/evaluation/schema", json={
            "part1": [{"id": "q1_1", "type": "rating", "text": "Disabled", "enabled": False}],
            "part2": [],
            "part3": []
        })
        assert r_empty_schema.status_code == 400, "Saving a schema with 0 active questions must be rejected"

        custom_schema = {
            "part1": [
                {"id": "q1_1", "type": "rating", "text": "1. Active rating", "enabled": True},
                {"id": "q1_2", "type": "rating", "text": "2. Disabled rating", "enabled": False}
            ],
            "part2": [
                {"id": "c_custom_text", "type": "text", "text": "Custom open feedback", "enabled": True}
            ],
            "part3": []
        }
        r_set_schema = client.post("/api/admin/evaluation/schema", json=custom_schema)
        assert r_set_schema.status_code == 200

        # Check-in guest attendee (temporarily set AFTERNOON_IN) to test dynamic schema & cleared draft
        database.set_setting("current_phase", "AFTERNOON_IN")
        r_dyn_in = client.post("/api/attendance/in", json={
            "student_id": "2024-DYN-001", "last_name": "Edge", "first_name": "Tester",
            "middle_name": "N/A", "sex": "Male", "age": 20, "email": "edge@aclc.edu.ph",
            "level": "COLLEGE", "section": "CS31A"
        })
        assert r_dyn_in.status_code == 200 and r_dyn_in.json()["status"] == "success", f"Unexpected: {r_dyn_in.text}"
        dyn_tok = r_dyn_in.json()["token"]
        database.set_setting("current_phase", "AFTERNOON_OUT")

        # Verify lowercase aclc: QR payload is accepted by /api/attendance/out
        r_qr_lower = client.post("/api/attendance/out", json={"student_id": f"aclc:2024-dyn-001:{dyn_tok.lower()}"})
        assert r_qr_lower.status_code == 200 and r_qr_lower.json()["status"] == "success", f"Lowercase QR failed: {r_qr_lower.text}"

        r_dyn_open = client.post("/api/evaluation/open", json={
            "student_id": "2024-DYN-001", "session_id": "SESS-DYN", "token": dyn_tok
        })
        assert r_dyn_open.status_code == 200

        # Cleared draft (0 answered) must NOT inflate eval_in_progress, and _ts must persist
        client.post("/api/evaluation/autosave", json={
            "student_id": "2024-DYN-001", "session_id": "SESS-DYN", "token": dyn_tok,
            "draft": {"_ts": 1700000000123, "q1_1": None}
        })
        r_stats_zero = client.get("/api/stats").json()
        assert r_stats_zero["eval_in_progress"] == 0, f"Cleared draft must not count as tallyable draft: {r_stats_zero}"
        r_dyn_reopen = client.post("/api/evaluation/open", json={
            "student_id": "2024-DYN-001", "session_id": "SESS-DYN", "token": dyn_tok
        })
        assert r_dyn_reopen.json()["draft"].get("_ts") == 1700000000123

        # Missing custom question must fail; answering active questions (without disabled q1_2) must succeed,
        # and custom text "5" must not skew q1_avg (should stay 4.0, not 4.5!)
        r_dyn_missing = client.post("/api/evaluation/submit", json={
            "student_id": "2024-DYN-001", "session_id": "SESS-DYN", "token": dyn_tok,
            "answers": {"q1_1": 4}
        })
        assert r_dyn_missing.json()["status"] == "error"
        r_dyn_ok = client.post("/api/evaluation/submit", json={
            "student_id": "2024-DYN-001", "session_id": "SESS-DYN", "token": dyn_tok,
            "answers": {"q1_1": 4, "c_custom_text": "5"}
        })
        assert r_dyn_ok.status_code == 200 and r_dyn_ok.json()["status"] == "success"
        assert r_dyn_ok.json()["q1_avg"] == 4.0, f"Text '5' should not skew rating avg: {r_dyn_ok.json()}"

        # Admin Eval detail & CSV export must include custom schema question
        r_dyn_detail = client.get("/api/admin/evaluation-detail?student_id=2024-DYN-001")
        assert r_dyn_detail.status_code == 200 and r_dyn_detail.json()["draft"].get("c_custom_text") == "5"
        r_dyn_csv = client.get("/api/export/evaluation-csv")
        assert r_dyn_csv.status_code == 200 and "Custom: Custom open feedback" in r_dyn_csv.text

        # Admin resetting eval_status back to IN_PROGRESS must unlock evaluations.status for reopening
        r_reset_eval = client.post("/api/admin/edit-attendee", json={
            "student_id": "2024-DYN-001", "last_name": "Edge", "first_name": "Tester",
            "middle_name": "N/A", "sex": "Male", "age": 20, "email": "edge@aclc.edu.ph",
            "level": "COLLEGE", "section": "CS31A", "eval_status": "IN_PROGRESS", "is_eligible": 0
        })
        assert r_reset_eval.status_code == 200
        r_reopen_unlocked = client.post("/api/evaluation/open", json={
            "student_id": "2024-DYN-001", "session_id": "SESS-DYN-2", "token": dyn_tok
        })
        assert r_reopen_unlocked.status_code == 200 and r_reopen_unlocked.json()["status"] == "success"

        # Reset quiz schema back to default and clean up temporary attendee
        assert client.post("/api/admin/evaluation/schema/reset").status_code == 200
        assert client.post("/api/admin/delete-attendee", json={"student_id": "2024-DYN-001"}).status_code == 200
        print("  [PASS] Dynamic Quiz Schema Validation, Custom Export, Admin Eval Reset & Edge Cases Verified")

        # 9. Verify Student View HTML Responsive Refactor & Try-Evaluation Preview Link
        r_stud_html = client.get("/")
        assert r_stud_html.status_code == 200
        assert "student-main-container" in r_stud_html.text
        assert "session-status-row" in r_stud_html.text
        assert "d-none d-sm-flex" in r_stud_html.text
        r_try_eval = client.get("/try-evaluation", follow_redirects=False)
        assert r_try_eval.status_code == 303 and "/?try_quiz=1" in r_try_eval.headers["location"]
        print("  [PASS] Student View Responsive Layout & Try-Evaluation Link Verified")

        # Test Scanner Protected Gate (Unauthenticated Redirects to Login)
        client.cookies.clear()
        r_scan_unauth = client.get("/scanner", follow_redirects=False)
        assert r_scan_unauth.status_code == 303 and "/admin/login" in r_scan_unauth.headers["location"]
        print("  [PASS] Scanner Protected Gate (Unauthenticated Blocked from Scanner)")

        # Test Scanner with Admin Auth
        client.cookies.set("aclc_admin_session", admin_cookie)
        r_scan = client.get("/scanner")
        assert r_scan.status_code == 200 and "Camera Scanner" in r_scan.text
        print("  [PASS] Scanner Endpoint Verified")

        # Test F: Admin Authentication & 4-Attempt Lockout Gate
        main.FAILED_ATTEMPTS = 0
        main.LOCKOUT_UNTIL = None

        for attempt in range(1, 4):
            r = client.post("/admin/login", data={"password": "wrong_password"})
            assert "attempts remaining" in r.text
        
        # 4th Attempt -> Triggers Lockout
        r = client.post("/admin/login", data={"password": "wrong_password"})
        assert "Temporarily Locked" in r.text or "4 attempts exceeded" in r.text
        print("  [PASS] Admin Security: 4 Unsuccessful Attempts Lockout Countdown")

        # Unlock and test successful login
        main.FAILED_ATTEMPTS = 0
        main.LOCKOUT_UNTIL = None
        r_login = client.post("/admin/login", data={"password": "nevergrowold"}, follow_redirects=False)
        assert r_login.status_code == 303
        cookie = r_login.cookies.get("aclc_admin_session")
        assert cookie is not None
        print("  [PASS] Admin Authentication with 'nevergrowold'")

        # Test Kiosk Config Broadcast API
        r_kiosk = client.post(
            "/api/admin/set-kiosk-config",
            json={
                "event_title": "ACLC Gender Seminar 2026",
                "active_session_label": "Afternoon Time In",
                "current_phase": "AFTERNOON_IN",
                "event_date": "November 12, 2026",
                "venue": "ACLC Auditorium",
                "kiosk_announcement": "Welcome back to the afternoon session!",
                "terms_and_conditions": "Sample terms"
            },
            cookies={"aclc_admin_session": cookie}
        )
        assert r_kiosk.status_code == 200
        
        # Test /api/kiosk-live reflects the changes immediately
        r_live = client.get("/api/kiosk-live")
        assert r_live.status_code == 200
        live_data = r_live.json()
        assert live_data["event_title"] == "ACLC Gender Seminar 2026"
        assert live_data["active_session_label"] == "Afternoon Time In"
        assert live_data["phase"] == "AFTERNOON_IN"
        assert live_data["session"] == "Afternoon"
        assert live_data["status"] == "Time In"
        assert live_data["kiosk_announcement"] == "Welcome back to the afternoon session!"
        print("  [PASS] Live Config & Session/Status Sync")

        # Test Projector Config API (3 Access Points + https:// normalization to plain http:// + bare IP display)
        r_proj = client.post(
            "/api/admin/set-projector-config",
            json={
                "proj_show_form_qr": True,
                "proj_show_wifi_qr": False,
                "wifi_ssid": "ACLC_Auditorium_WiFi",
                "wifi_ssid_2": "ACLC_Auditorium_WiFi_2",
                "wifi_ssid_3": "ACLC_Auditorium_WiFi_3",
                "wifi_password": "securepassword",
                "kiosk_url": "https://192.168.1.150:8080/",
                "proj_announcement": "Welcome to the Seminar! Please scan QR."
            },
            cookies={"aclc_admin_session": cookie}
        )
        assert r_proj.status_code == 200

        # Verify saving kiosk config with empty current_phase does not revert active phase
        r_kiosk_nophase = client.post(
            "/api/admin/set-kiosk-config",
            json={
                "event_title": "ACLC Gender Seminar 2026",
                "active_session_label": "Afternoon Time In",
                "current_phase": "",
                "event_date": "",
                "venue": "ACLC College of Mandaue",
                "kiosk_announcement": "Welcome back to the afternoon session!",
                "terms_and_conditions": ""
            },
            cookies={"aclc_admin_session": cookie}
        )
        assert r_kiosk_nophase.status_code == 200

        # Verify live sync on /api/kiosk-live & /projector HTML & /api/qr/wifi?ap=2
        r_proj_live = client.get("/api/kiosk-live")
        assert r_proj_live.status_code == 200
        p_data = r_proj_live.json()
        assert p_data["phase"] == "AFTERNOON_IN"
        assert p_data["proj_show_form_qr"] is True
        assert p_data["proj_show_wifi_qr"] is False
        assert p_data["wifi_ssid"] == "ACLC_Auditorium_WiFi"
        assert p_data["wifi_ssids"] == ["ACLC_Auditorium_WiFi", "ACLC_Auditorium_WiFi_2", "ACLC_Auditorium_WiFi_3"]
        assert p_data["wifi_password"] == "securepassword"
        assert p_data["kiosk_url"] == "http://192.168.1.150:8080"
        assert p_data["display_url"] == "192.168.1.150:8080"
        assert p_data["proj_announcement"] == "Welcome to the Seminar! Please scan QR."

        r_proj_html = client.get("/projector")
        assert r_proj_html.status_code == 200
        assert "STEP 1: CONNECT TO WI-FI" in r_proj_html.text
        assert "STEP 2: ATTENDANCE &amp; QUIZ" in r_proj_html.text
        assert "192.168.1.150:8080" in r_proj_html.text
        assert "ACLC_Auditorium_WiFi_2" in r_proj_html.text
        assert "ACLC_Auditorium_WiFi_3" in r_proj_html.text
        assert client.get("/api/qr/wifi?ap=2").status_code == 200
        r_cap = client.get("/captive", headers={"host": "connectivitycheck.gstatic.com"})
        assert r_cap.status_code == 200
        assert "http://192.168.1.150:8080/" in r_cap.text
        assert "connectivitycheck.gstatic.com" not in r_cap.text
        print("  [PASS] Projector Multi-AP, Bare-IP Plain HTTP, Captive Host & Settings API Verified")

        # Test Pass Lookup Endpoint (Name + Age, Duplicate Disambiguation & Legacy ID + Last Name)
        r_lookup_unverified = client.get("/api/attendance/pass-lookup?student_id=2024-SHS-001")
        assert r_lookup_unverified.status_code == 200 and r_lookup_unverified.json()["status"] == "error"
        r_lookup = client.get("/api/attendance/pass-lookup?student_id=2024-SHS-001&last_name=Dela+Cruz")
        assert r_lookup.status_code == 200
        lookup_data = r_lookup.json()
        assert lookup_data["status"] == "success"
        assert lookup_data["student_id"] == "2024-SHS-001"
        assert lookup_data["qr_data"].startswith("ACLC:2024-SHS-001:")

        # Name + Age recovery (without knowing ID or Email)
        r_lookup_name_age = client.get("/api/attendance/pass-lookup?q=Juan+Dela+Cruz&age=17")
        assert r_lookup_name_age.status_code == 200 and r_lookup_name_age.json()["status"] == "success"
        assert r_lookup_name_age.json()["student_id"] == "2024-SHS-001"

        # Duplicate Name + Age -> returns status='multiple' with selectable matches, and blank-ID manual add works
        r_dup_add = client.post(
            "/api/admin/manual-add",
            json={
                "student_id": "",
                "last_name": "Dela Cruz",
                "first_name": "Juan",
                "middle_name": "",
                "sex": "Male",
                "age": 17,
                "email": "shared@gmail.com",
                "level": "SHS",
                "section": "11-ABM-B",
                "phase": "MORNING_IN"
            },
            cookies={"aclc_admin_session": cookie}
        )
        assert r_dup_add.status_code == 200 and r_dup_add.json()["status"] == "success"
        dup_noid = r_dup_add.json()["student_id"]
        assert dup_noid.startswith("NOID-")

        r_lookup_multi = client.get("/api/attendance/pass-lookup?q=Juan+Dela+Cruz&age=17")
        assert r_lookup_multi.status_code == 200 and r_lookup_multi.json()["status"] == "multiple"
        assert len(r_lookup_multi.json()["matches"]) == 2
        assert client.post("/api/admin/delete-attendee", json={"student_id": dup_noid}, cookies={"aclc_admin_session": cookie}).status_code == 200
        print("  [PASS] Student Pass Lookup (Name+Age & Duplicate Disambiguation) + Blank-ID Manual Add Verified")

        # Test Admin Manual Add Attendee with custom time-in + recording missing Afternoon Time-In on existing attendee
        r_man_add = client.post(
            "/api/admin/manual-add",
            json={
                "student_id": "2024-MAN-999",
                "last_name": "Reyes",
                "first_name": "Maria",
                "middle_name": "Santos",
                "sex": "Female",
                "age": 18,
                "email": "maria.reyes@gmail.com",
                "level": "Senior High (SHS)",
                "section": "12-STEM-A",
                "phase": "MORNING_IN",
                "time_in": "2026-11-12 08:15:00"
            },
            cookies={"aclc_admin_session": cookie}
        )
        assert r_man_add.status_code == 200 and r_man_add.json()["status"] == "success"
        r_man_aft = client.post(
            "/api/admin/manual-add",
            json={
                "student_id": "2024-MAN-999",
                "last_name": "Reyes",
                "first_name": "Maria",
                "middle_name": "Santos",
                "sex": "Female",
                "age": 18,
                "email": "maria.reyes@gmail.com",
                "level": "Senior High (SHS)",
                "section": "12-STEM-A",
                "phase": "AFTERNOON_IN",
                "time_in": "2026-11-12 13:05:00"
            },
            cookies={"aclc_admin_session": cookie}
        )
        assert r_man_aft.status_code == 200 and r_man_aft.json()["status"] == "success"
        print("  [PASS] Admin/Officer Manual Add Attendee & Session Time-In Verified")

        # Test Admin Edit Attendee (modify records and mark eligible)
        r_edit = client.post(
            "/api/admin/edit-attendee",
            json={
                "student_id": "2024-MAN-999",
                "last_name": "Reyes-Cruz",
                "first_name": "Maria Clara",
                "middle_name": "Santos",
                "sex": "Female",
                "age": 19,
                "email": "maria.clara@gmail.com",
                "level": "Senior High (SHS)",
                "section": "12-STEM-B",
                "morning_in": "2026-11-12 08:15:00",
                "morning_out": "2026-11-12 11:30:00",
                "is_eligible": 1
            },
            cookies={"aclc_admin_session": cookie}
        )
        assert r_edit.status_code == 200 and r_edit.json()["status"] == "success"
        print("  [PASS] Admin Edit Attendee (Timestamps, Demographics & Eligibility) Verified")

        # Test Admin Delete Attendee
        r_del = client.post(
            "/api/admin/delete-attendee",
            json={"student_id": "2024-MAN-999"},
            cookies={"aclc_admin_session": cookie}
        )
        assert r_del.status_code == 200 and r_del.json()["status"] == "success"
        all_att = database.get_all_attendees()
        assert not any(a["student_id"] == "2024-MAN-999" for a in all_att)
        print("  [PASS] Admin Delete Attendee Verified")

        # ---------------------------------------------------------
        # 2. LATENCY & PERFORMANCE BENCHMARK (in milliseconds)
        # ---------------------------------------------------------
        print("\n[Phase 2] Running High-Concurrency Latency Benchmarks (in ms)...")

        # Switch back to Morning In for load test
        client.post("/api/admin/set-phase", json={"phase": "MORNING_IN"}, cookies={"aclc_admin_session": cookie})

        # Benchmark A: 100 Consecutive Student Check-Ins
        NUM_STUDENTS = 100
        checkin_latencies = []

        for i in range(1, NUM_STUDENTS + 1):
            s_payload = {
                "student_id": f"2024-BENCH-{i:04d}",
                "last_name": f"Student_{i}",
                "first_name": "Test",
                "middle_name": "A",
                "sex": "Female" if i % 2 == 0 else "Male",
                "age": 17 + (i % 5),
                "email": f"student{i}@aclc.edu.ph",
                "level": "COLLEGE" if i % 2 == 0 else "SHS",
                "shs_grade": "Grade 11",
                "strand": "STEM",
                "college_year": "2nd Year",
                "course": "BSIT",
                "section": f"SEC-{(i % 4) + 1}"
            }
            headers = {
                "x-forwarded-for": f"192.168.1.{10 + (i % 200)}",
                "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"
            }

            t0 = time.perf_counter()
            resp = client.post("/api/attendance/in", json=s_payload, headers=headers)
            t1 = time.perf_counter()

            assert resp.status_code == 200
            checkin_latencies.append((t1 - t0) * 1000.0)

        # Benchmark B: Statistics Aggregation Query Latency (Admin Auth Required)
        assert TestClient(main.app).get("/api/stats").status_code == 401
        stats_latencies = []
        for _ in range(50):
            t0 = time.perf_counter()
            r_stats = client.get("/api/stats")
            t1 = time.perf_counter()
            assert r_stats.status_code == 200
            stats_latencies.append((t1 - t0) * 1000.0)

        # Benchmark C: 100 Simultaneous Student Phones Polling /api/kiosk-live & /api/attendance/passes-status
        live_poll_latencies = []
        for i in range(1, 101):
            t0 = time.perf_counter()
            r_poll = client.get("/api/kiosk-live")
            r_pass_poll = client.post("/api/attendance/passes-status", json={"student_ids": [f"2024-BENCH-{i:04d}"]})
            t1 = time.perf_counter()
            assert r_poll.status_code == 200 and r_pass_poll.status_code == 200
            live_poll_latencies.append((t1 - t0) * 1000.0)

        # Benchmark D: QR Code Generation Latency
        qr_latencies = []
        for _ in range(30):
            t0 = time.perf_counter()
            r_qr = client.get("/api/qr")
            t1 = time.perf_counter()
            assert r_qr.status_code == 200
            qr_latencies.append((t1 - t0) * 1000.0)

        # Benchmark E: CSV Export Latency (Over 100+ Records)
        t0 = time.perf_counter()
        r_csv = client.get("/api/export/deped-csv", cookies={"aclc_admin_session": cookie})
        t1 = time.perf_counter()
        csv_latency = (t1 - t0) * 1000.0
        assert r_csv.status_code == 200

        r_eval_csv = client.get("/api/export/evaluation-csv", cookies={"aclc_admin_session": cookie})
        assert r_eval_csv.status_code == 200 and "HIV" in r_eval_csv.text
        r_cert_csv = client.get("/api/export/certificate-csv", cookies={"aclc_admin_session": cookie})
        assert r_cert_csv.status_code == 200 and "College (2nd Year - BSIT - BSIT-2B)" in r_cert_csv.text
        print("  [PASS] Evaluation & Certificate CSV Exports Verified")

        # Print Benchmark Report
        def print_stats_table(name, samples):
            avg = statistics.mean(samples)
            med = statistics.median(samples)
            p95 = statistics.quantiles(samples, n=20)[18] if len(samples) >= 20 else max(samples)
            mn = min(samples)
            mx = max(samples)
            throughput = 1000.0 / avg
            print(f"\nMetric: {name} (N={len(samples)})")
            print(f"  {'Average:':<15} {avg:.2f} ms")
            print(f"  {'Median (P50):':<15} {med:.2f} ms")
            print(f"  {'95th Percentile:':<15} {p95:.2f} ms")
            print(f"  {'Min / Max:':<15} {mn:.2f} ms / {mx:.2f} ms")
            print(f"  {'Est. Throughput:':<15} {throughput:.1f} req/second")

        print("\n" + "=" * 65)
        print("PERFORMANCE BENCHMARK RESULTS")
        print("=" * 65)
        print_stats_table("POST /api/attendance/in (Write to SQLite)", checkin_latencies)
        print_stats_table("Student Live Poll (/api/kiosk-live + /passes-status)", live_poll_latencies)
        print_stats_table("GET  /api/stats (Single-pass Aggregation)", stats_latencies)
        print_stats_table("GET  /api/qr (In-Memory Image Rendering)", qr_latencies)
        print(f"\nMetric: GET /api/export/deped-csv (100+ Attendees CSV Stream)")
        print(f"  {'Execution Time:':<15} {csv_latency:.2f} ms")

        print("\n" + "=" * 65)
        print("ALL TESTS & BENCHMARKS PASSED SUCCESSFULLY")
        print("=" * 65)

    finally:
        # ---------------------------------------------------------
        # 3. CLEANUP: Delete All Test Data & Temp Files (Windows & Linux safe)
        # ---------------------------------------------------------
        print("\n[Phase 3] Cleaning up test data...")
        import gc
        gc.collect()
        for p in (TEST_DB_PATH, f"{TEST_DB_PATH}-wal", f"{TEST_DB_PATH}-shm"):
            if os.path.exists(p):
                try:
                    os.remove(p)
                    if p == TEST_DB_PATH:
                        print(f"  [DELETED] Temporary database: {TEST_DB_PATH}")
                except PermissionError:
                    pass
        print("  [CLEAN] Production database was untouched. Clean slate preserved.")

if __name__ == "__main__":
    run_performance_and_unit_tests()
