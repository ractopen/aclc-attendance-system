"""
Unit tests for Printable PDF Suite (blank attendance sheet, blank evaluation form,
filled attendance master printout, filled evaluation answers & tally printout).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import main
import database


class TestPrintablePDFs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()
        cls.client = TestClient(main.app)
        pw = database.get_setting("admin_password", "nevergrowold")
        resp = cls.client.post("/admin/login", data={"password": pw}, follow_redirects=False)
        assert resp.status_code in (200, 303), f"Admin login failed: {resp.status_code}"

    def test_01_blank_attendance_and_evaluation_pdfs(self):
        r1 = self.client.get("/api/print/attendance-sheet.pdf?pages=2&session=Morning")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.headers.get("content-type"), "application/pdf")
        self.assertTrue(r1.content.startswith(b"%PDF-"))
        self.assertGreater(len(r1.content), 1500)

        r2 = self.client.get("/api/print/evaluation-form.pdf")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.headers.get("content-type"), "application/pdf")
        self.assertTrue(r2.content.startswith(b"%PDF-"))
        self.assertGreater(len(r2.content), 1500)

    def test_02_filled_reports_pdfs(self):
        r3 = self.client.get("/api/print/attendance-records.pdf")
        self.assertEqual(r3.status_code, 200)
        self.assertTrue(r3.content.startswith(b"%PDF-"))

        r4 = self.client.get("/api/print/evaluation-answers.pdf")
        self.assertEqual(r4.status_code, 200)
        self.assertTrue(r4.content.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
