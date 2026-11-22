import io
import os
import sys
import time
import smtplib
import tempfile
from unittest.mock import patch, MagicMock
from PIL import Image

current_dir = os.path.dirname(os.path.abspath(__file__))
app_dir = os.path.dirname(current_dir)
sys.path.insert(0, app_dir)

import database
TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f"aclc_cert_test_{int(time.time())}.db")
database.DB_PATH = TEST_DB_PATH

import certificate_service
from main import app
from fastapi.testclient import TestClient


def run_certificate_tests():
    print("=" * 65)
    print("ACLC Certificate Studio & Gmail SMTP Test Suite")
    print("=" * 65)

    database.init_db()
    client = TestClient(app)
    tmp_dir = tempfile.mkdtemp(prefix="aclc_certs_")
    certificate_service.CERT_OUTPUT_DIR = tmp_dir

    try:
        # 1. Built-in PDF generation + long name auto-fit + Ñ/ñ handling
        attendee = {
            "student_id": "2024-CERT-001",
            "full_name": "PEÑAFLORIDA-DELA CRUZ, MARIA THERESA CONCEPCION EVANGELISTA JR.",
            "email": "maria@aclc.edu.ph",
            "level": "COLLEGE",
            "college_year": "3rd Year",
            "course": "BSCS",
            "section": "CS31A",
        }
        pdf_bytes = certificate_service.generate_certificate_pdf(attendee)
        assert pdf_bytes.startswith(b"%PDF-"), "Expected valid PDF header"
        assert len(pdf_bytes) > 1000, "PDF seems unexpectedly empty"
        print("  [PASS] Built-in PDF generation with long name & Latin-1 chars")

        # 2. Canva RGBA PNG upload + PDF generation
        r_login = client.post("/admin/login", data={"password": "nevergrowold"}, follow_redirects=False)
        assert r_login.status_code == 303

        rgba_buf = io.BytesIO()
        img = Image.new("RGBA", (2000, 1414), (245, 245, 250, 200))
        img.save(rgba_buf, format="PNG")
        rgba_buf.seek(0)

        tmpl_override = os.path.join(tmp_dir, "canva_template.png")
        certificate_service.CERT_TEMPLATE_FILE = tmpl_override
        with patch("main.CERT_TEMPLATE_FILE", tmpl_override):
            r_up = client.post(
                "/api/admin/certificate/upload-template",
                files={"file": ("canva.png", rgba_buf.getvalue(), "image/png")},
            )
            assert r_up.status_code == 200 and r_up.json()["width"] == 2000

        r_prev = client.get("/api/admin/certificate/preview?sample_name=SANTOS,%20JOSE")
        assert r_prev.status_code == 200 and r_prev.content.startswith(b"%PDF-")
        print("  [PASS] Canva RGBA PNG upload & PDF preview stream")

        # 3. Atomic claim idempotency + crash recovery
        for sid, ln, fn, em, sec in [
            ("2024-ELIG-01", "Reyes", "Ana", "ana.reyes@gmail.com", "BSIT-2A"),
            ("2024-ELIG-BAD", "Typo", "BadEmail", "not-an-email", "BSIT-2A"),
        ]:
            database.admin_manual_add_attendee({
                "student_id": sid,
                "last_name": ln,
                "first_name": fn,
                "middle_name": "N/A",
                "sex": "Female",
                "age": 20,
                "email": em,
                "level": "COLLEGE",
                "section": sec,
            })
            database.admin_edit_attendee(sid, {
                "morning_in": "2026-11-15 08:00:00",
                "morning_out": "2026-11-15 11:00:00",
                "eval_status": "SUBMITTED",
                "is_eligible": 1,
            })

        claim1 = database.claim_certificate_for_sending("2024-ELIG-01")
        claim2 = database.claim_certificate_for_sending("2024-ELIG-01")
        assert claim1 is not None and claim2 is None, "Second concurrent claim must be blocked"
        # Simulate crash recovery via init_db()
        database.init_db()
        summary = database.get_certificate_delivery_summary()
        assert summary["sending"] == 0 and summary["failed"] == 1, f"Expected SENDING recovered to FAILED, got {summary}"
        print("  [PASS] Atomic claim idempotency & init_db() crash recovery")

        # 4. Mocked Gmail SMTP Batch Send (valid -> SENT, invalid -> FAILED without aborting)
        database.set_setting("smtp_user", "sender@gmail.com")
        database.set_setting("smtp_app_password", "abcd efgh ijkl mnop")

        mock_smtp = MagicMock()
        with patch("certificate_service.smtplib.SMTP", return_value=mock_smtp):
            res = certificate_service.start_batch_send(include_failed=True)
            assert res["status"] == "started", f"Expected started, got {res}"
            for _ in range(40):
                st = certificate_service.get_batch_status()
                if not st["running"]:
                    break
                time.sleep(0.1)

        st = certificate_service.get_batch_status()
        assert not st["running"], "Batch should have finished"
        assert st["sent"] == 1 and st["failed"] == 1, f"Expected 1 sent & 1 failed, got {st}"
        assert mock_smtp.send_message.call_count == 1
        print("  [PASS] Batch Gmail SMTP send (1 SENT, 1 invalid-email FAILED)")

        # 5. Auth error aborts cleanly
        database.admin_manual_add_attendee({
            "student_id": "2024-ELIG-02",
            "last_name": "Cruz",
            "first_name": "Leo",
            "middle_name": "N/A",
            "sex": "Male",
            "age": 21,
            "email": "leo.cruz@gmail.com",
            "level": "COLLEGE",
            "section": "CS31A",
        })
        database.admin_edit_attendee("2024-ELIG-02", {
            "morning_in": "2026-11-15 08:00:00",
            "morning_out": "2026-11-15 11:00:00",
            "eval_status": "SUBMITTED",
            "is_eligible": 1,
        })
        bad_smtp = MagicMock()
        bad_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")
        with patch("certificate_service.smtplib.SMTP", return_value=bad_smtp):
            res_auth = certificate_service.send_single_certificate("2024-ELIG-02", force=False)
            assert res_auth["status"] == "error" and "authentication" in res_auth["message"].lower()
        print("  [PASS] Gmail SMTPAuthenticationError handled cleanly")

        # 6. /quality — Troll detection, Near-Miss (2 mins short) rescue, Stash All Clean, Send Staged, CSV Report
        database.admin_manual_add_attendee({
            "student_id": "2024-TROLL-1",
            "last_name": "Skibidi",
            "first_name": "Batman99",
            "middle_name": "N/A",
            "sex": "Male",
            "age": 19,
            "email": "troll@gmial.com",
            "level": "COLLEGE",
            "section": "CS31A",
        })
        database.admin_edit_attendee("2024-TROLL-1", {
            "morning_in": "2026-11-15 08:00:00",
            "morning_out": "2026-11-15 11:00:00",
            "eval_status": "SUBMITTED",
            "is_eligible": 1,
        })
        database.admin_manual_add_attendee({
            "student_id": "2024-NEAR-2MIN",
            "last_name": "Navarro",
            "first_name": "Grace",
            "middle_name": "N/A",
            "sex": "Female",
            "age": 20,
            "email": "grace.navarro@gmail.com",
            "level": "COLLEGE",
            "section": "CS31A",
        })
        # 148 mins = 2 mins short of 150 min threshold
        database.admin_edit_attendee("2024-NEAR-2MIN", {
            "morning_in": "2026-11-15 08:00:00",
            "morning_out": "2026-11-15 10:28:00",
            "eval_status": "SUBMITTED",
            "is_eligible": 0,
        })

        r_qpage = client.get("/quality")
        assert r_qpage.status_code == 200 and "Certificate Quality Check" in r_qpage.text

        r_qdata = client.get("/api/admin/quality/data?grace_minutes=10")
        assert r_qdata.status_code == 200
        qjson = r_qdata.json()
        troll_rows = [x for x in qjson["eligible"] if x["student_id"] == "2024-TROLL-1"]
        assert len(troll_rows) == 1 and troll_rows[0]["quality"]["severity"] == "critical"
        near_rows = [x for x in qjson["near_miss"] if x["student_id"] == "2024-NEAR-2MIN"]
        assert len(near_rows) == 1 and near_rows[0]["shortfall_min"] == 2.0

        # Rescue near-miss -> becomes eligible & STAGED
        r_res = client.post("/api/admin/quality/rescue-near-miss", json={"student_id": "2024-NEAR-2MIN"})
        assert r_res.status_code == 200 and r_res.json()["status"] == "success"

        # Stash all clean -> must stage 2024-ELIG-02 (clean), NOT 2024-TROLL-1 (critical)
        r_stash = client.post("/api/admin/quality/stash-all-clean")
        assert r_stash.status_code == 200 and r_stash.json()["staged_count"] == 1

        # Send staged pool -> sends only STAGED (2024-NEAR-2MIN & 2024-ELIG-02), skips 2024-TROLL-1
        mock_smtp2 = MagicMock()
        with patch("certificate_service.smtplib.SMTP", return_value=mock_smtp2):
            r_send = client.post("/api/admin/certificate/send-staged", json={"include_failed": True})
            assert r_send.status_code == 200 and r_send.json()["status"] == "started"
            for _ in range(40):
                st2 = certificate_service.get_batch_status()
                if not st2["running"]:
                    break
                time.sleep(0.1)
        assert mock_smtp2.send_message.call_count == 2, f"Expected 2 staged sends, got {mock_smtp2.send_message.call_count}"

        # Delivery CSV Report
        r_csv = client.get("/api/export/certificate-delivery-csv")
        assert r_csv.status_code == 200
        assert "OK - Sent without error" in r_csv.text
        assert "2024-TROLL-1" in r_csv.text

        # Duplicate-email warning across two eligible attendees
        database.admin_manual_add_attendee({
            "student_id": "2024-DUP-EM-1", "last_name": "Santos", "first_name": "Ana",
            "middle_name": "N/A", "sex": "Female", "age": 19, "email": "shared.inbox@gmail.com",
            "level": "COLLEGE", "section": "CS31A",
        })
        database.admin_edit_attendee("2024-DUP-EM-1", {
            "morning_in": "2026-11-15 08:00:00", "morning_out": "2026-11-15 11:00:00",
            "eval_status": "SUBMITTED", "is_eligible": 1,
        })
        database.admin_manual_add_attendee({
            "student_id": "2024-DUP-EM-2", "last_name": "Santos", "first_name": "Ben",
            "middle_name": "N/A", "sex": "Male", "age": 19, "email": "shared.inbox@gmail.com",
            "level": "COLLEGE", "section": "CS31A",
        })
        database.admin_edit_attendee("2024-DUP-EM-2", {
            "morning_in": "2026-11-15 08:00:00", "morning_out": "2026-11-15 11:00:00",
            "eval_status": "SUBMITTED", "is_eligible": 1,
        })
        q_dup = client.get("/api/admin/quality/data").json()
        dup_rows = [x for x in q_dup["eligible"] if x["student_id"] in ("2024-DUP-EM-1", "2024-DUP-EM-2")]
        assert len(dup_rows) == 2
        assert all(any(f["code"] == "duplicate_email" for f in r["quality"]["flags"]) for r in dup_rows)
        assert all(r["quality"]["severity"] == "warning" for r in dup_rows)
        print("  [PASS] /quality troll detection, duplicate-email warning, 2-min near-miss rescue, stash pool & delivery CSV")

        print("=" * 65)
        print("ALL CERTIFICATE STUDIO & EMAIL TESTS PASSED")
        print("=" * 65)
    finally:
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        for fn in os.listdir(tmp_dir):
            try:
                os.remove(os.path.join(tmp_dir, fn))
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


if __name__ == "__main__":
    run_certificate_tests()
