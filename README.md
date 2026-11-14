# ACLC College Seminar Attendance, Evaluation & Demographics System
### HIV Awareness Seminar &middot; DepEd &amp; Gender and Development (GAD) Compliant &middot; 100% Offline-Ready

Built with **FastAPI**, **SQLite (WAL Mode)**, and **Tabler UI** in official **ACLC College Royal Blue, Gold, and HIV Awareness Crimson**. Designed to run out-of-the-box on **Windows** and **Linux** over any local Wi-Fi router or mobile hotspot with zero internet dependency.

---

## 1. Quick Start (Windows & Linux)

### On Windows:
Double-click `run.bat` or run in Command Prompt / PowerShell:
```cmd
cd aclc-attendance-system
run.bat
```
*(Automatically detects Python 3 / `py -3`, builds or repairs the Windows virtual environment even if copied from Linux or another folder, detects your laptop's active Wi-Fi/LAN IP address, and launches on port `8080`).*

### On Linux:
```bash
cd aclc-attendance-system
./run.sh
```
*(Automatically verifies or builds the Linux virtual environment, checks port `8080`, detects your active LAN IP address, and launches the server).*

### Direct Python Launch (Any OS):
```bash
python main.py
```

---

## 2. System URLs & Access Roles

| URL | View | Description |
| :--- | :--- | :--- |
| `http://<HOST_IP>:8080/` | **Student Kiosk & Pass Wallet** | Self-service Time-In, multi-pass wallet (shared phone support), and Seminar Evaluation Form |
| `http://<HOST_IP>:8080/projector` | **Projector Big Screen** | Symmetrical dual QR display (Guest Wi-Fi QR + Attendance & Evaluation QR) with live status |
| `http://localhost:8080/admin` | **Admin Control Dashboard** | Live session/phase controls, Evaluation Open/Close toggle, attendee editor, tally, and CSV exports |
| `http://localhost:8080/admin/scanner` | **Officer Exit QR Scanner** | Camera, Snap-Photo, and USB/Manual Student ID scanner for rapid Time-Out verification |
| `http://localhost:8080/try-evaluation` | **Evaluation Preview** | Direct preview shortcut to inspect and test-drive the Seminar Evaluation Form |

---

## 3. Complete Feature Checklist

### A. Admin Security & Lockout Gate
* **Master Admin Password:** `nevergrowold`
* **Anti-Brute Force Lockout:** 4 failed login attempts lock the login gate for **5 minutes** with a live countdown timer.
* **Protected Officer Scanner:** Time-Out recording (`/api/attendance/out` and `/admin/scanner`) requires active admin authentication so students cannot self-checkout early.

### B. Student Form UX & Smart Auto-Detection
* **Academic Category Toggles:** `Senior High (SHS)`, `College Level`, and `Guest / Other`.
* **Smart Section Keyword Auto-Detection:**
  * Typing `BSCS`, `CS`, `BSIT`, `IT`, `BSBA`, `BA`, `BSHM`, `HM`, or `ACT` (e.g., `CS31A`, `BSIT-2B`) automatically selects **College Level** and infers year and course.
  * Typing `STEM`, `ICT`, `TVL`, `ABM`, `HUMSS`, `GAS`, `11`, or `12` (e.g., `11-ICT-A`, `12-STEM-B`) automatically selects **Senior High (SHS)** and infers grade and strand.
  * Selecting `Guest / Other` switches fields to **ID / Control Number** and **Role / Designation**.
* **Age Stepper (`10` to `120`):** One-tap `[-]` and `[+]` buttons with numeric validation.
* **Optional Middle Name:** Defaults to `N/A` when left blank.
* **Salted HMAC-SHA256 QR Pass (`ACLC:<ID>:<TOKEN>`):** Generates a cryptographic QR pass upon Time-In to prevent forged checkouts.
* **Shared Phone / Classmate Pass Support:** Multiple students can save passes and take their individual Seminar Evaluation Forms on a single phone.
* **RA 10173 Data Privacy & Seminar Terms Modal:** Built-in clickable privacy and certificate guidelines notice.

### C. Concurrency-Safe Seminar Evaluation Form (HIV Awareness)
* **Part I (10 Likert Statements, 5 to 1 Scale)** + **Part II (3 Learning Outcome Questions)** + **Part III (3 Overall Assessment Questions)**.
* **Server-Start Safety:** Evaluation defaults to **Closed** at server start until opened by the organizer in `/admin`.
* **First-Active-Session Lock Priority:** Prevents simultaneous overwrites if the same Student ID is opened on two devices at once (35-second lease with automatic heartbeat and `sendBeacon` release on close).
* **Real-Time Auto-Save:** Saves drafts both locally (`localStorage`) and to SQLite as the student types or selects ratings.
* **End-of-Day Admin Draft Tally:** One-click button in `/admin` (`Tally & Finalize All Unsubmitted Drafts`) finalizes any in-progress drafts where a student answered at least one item before leaving.

### D. Certificate Eligibility & CSV Exports
* **Dual Eligibility Requirement:** Attendees qualify for `APPROVED` certificate status when they meet the required attendance duration (default **150 minutes / 2 hrs 30 mins**, adjustable live in `/admin`) **AND** submit the Seminar Evaluation Form.
* **Three Instant CSV Exports in `/admin`:**
  1. **Evaluation Responses CSV** (`/api/export/evaluation-csv`)
  2. **Attendance Master CSV (DepEd & GAD)** (`/api/export/deped-csv`)
  3. **Certificate Eligible CSV (AutoCrat / Mail Merge Ready)** (`/api/export/certificate-csv`)

---

## 4. Automated Verification & Latency Benchmarks

* **On Linux:** `./run_tests.sh`
* **On Windows:** `run_tests.bat`

---

### Prepared by: CS31A ACLC College of Mandaue
