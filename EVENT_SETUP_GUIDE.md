# ACLC College Event Attendance, Evaluation & Demographics System
## Event Setup and Operations Manual (Windows & Linux)

This document provides step-by-step instructions for deploying and operating the attendance and evaluation system during the **HIV Awareness Seminar** at **ACLC College of Mandaue**.

---

## 1. System Architecture Overview

The system operates in a **100% offline environment** with zero internet dependency:
* **Host Laptop (Windows or Linux):** Runs the FastAPI server, SQLite WAL database, Admin Dashboard, and Projector view.
* **Local Wi-Fi Router or Hotspot:** Any offline Wi-Fi router or laptop/phone hotspot. No WAN or internet cable is required.
* **Client Devices:** Any student or faculty smartphone, tablet, or laptop connected to the local Wi-Fi.

---

## 2. Pre-Event Network Setup

### Option A: Automatic IP Detection (Plug-and-Play on Any Router or Hotspot)
1. Connect the host laptop to your Wi-Fi router or hotspot.
2. Start the server (`run.bat` on Windows or `./run.sh` on Linux).
3. The server automatically detects your laptop's active LAN IPv4 address (e.g. `192.168.1.X`, `192.168.0.X`, or `192.168.137.X`) and embeds it directly into the Projector QR code!
4. Keep **Student Page URL** blank in `/admin` -> **3. Display Settings** so it uses the auto-detected IP automatically.

### Option B: Fixed Static IP (`192.168.1.100`) on Dedicated Offline Router
1. Power on the offline Wi-Fi router and set SSID to `Event_Attendance`.
2. Assign the host laptop the static IP `192.168.1.100` (Subnet mask `255.255.255.0`, Gateway `192.168.1.1`):
   * **Windows:** `Control Panel` -> `Network Connections` -> Right-click Wi-Fi/Ethernet -> `Properties` -> `IPv4` -> Use `192.168.1.100`.
   * **Linux:** `nmcli con mod "Wired connection 1" ipv4.addresses 192.168.1.100/24 ipv4.gateway 192.168.1.1 ipv4.method manual`
3. *Tip for Mobile Phones on Offline Wi-Fi:* If Android or iOS displays *"This network has no internet access — Stay connected?"*, students should tap **Stay Connected / Keep Trying Wi-Fi**.

---

## 3. Event Day Step-by-Step Operations

### Step 1: Run System Diagnostics (Optional Pre-Check)
* **Windows:** Double-click `run_tests.bat`
* **Linux:** Run `./run_tests.sh`
* Verifies all unit tests, HMAC QR security, evaluation concurrency locks, and SQLite latency benchmarks without touching production data.

### Step 2: Start the Server
* **Windows:** Double-click `run.bat`
* **Linux:** Run `./run.sh`
* The terminal window will display the active port (`8080`) and your laptop's detected Wi-Fi/LAN URL.

### Step 3: Open Admin Dashboard & Projector Display
1. Open a browser on the host laptop and go to:
   ```
   http://localhost:8080/admin
   ```
2. Log in with the Master Admin Password:
   ```
   nevergrowold
   ```
3. In **3. Display Settings**, enter your router's **Wi-Fi Name (SSID)** and **Wi-Fi Password** (leave password blank if open network) and click **Save**.
4. Open `http://localhost:8080/projector`, drag the window to the projector screen, and press **F11** for fullscreen.
   * **Left QR (Guest Wi-Fi):** Students scan to join the Wi-Fi network automatically.
   * **Right QR (Attendance & Evaluation):** Students scan to open the Student Kiosk in their phone browser.

### Step 4: Managing Event Phases Live in `/admin`
In **1. Event & Evaluation Control**:
1. **Registration / Arrival:** Set Session to `Morning` (or `Afternoon`) and Attendance Status to `Time In`.
2. **During Lecture (Open Evaluation Quiz):** Click **Open Quiz** under *Seminar Evaluation Form*. Every student's saved pass will immediately display the **Take Seminar Evaluation** button without refreshing their page.
3. **Departure / Dismissal (Exit Scanning):**
   * Switch Attendance Status to `Time Out`.
   * Open the Officer Scanner (`http://localhost:8080/admin/scanner` or on an officer's phone logged into `/admin/scanner`).
   * Scan student QR passes using **Start Camera** (live webcam), **Snap QR Photo** (works on any mobile browser over plain HTTP), or **Manual Entry / USB Barcode Scanner**.

---

## 4. End-of-Event Tally & CSV Exports

1. In `/admin` -> **4. Reports, Seminar Evaluation Tally & Exports**, click:
   * **[Tally & Finalize All Unsubmitted Drafts]** — Finalizes any in-progress evaluation drafts where a student answered at least one question before leaving and recomputes certificate eligibility.
2. Download the three official CSV reports:
   * **[Download Evaluation Responses CSV]** — Full ratings (Q1.1–Q1.10), averages, and open-ended responses (Q2.1–Q3.3).
   * **[Download Attendance Master CSV]** — Complete DepEd & GAD attendance and demographic log with IP and device telemetry.
   * **[Download Certificate Eligible CSV]** — Filtered strictly for approved attendees (`is_eligible = 1`), formatted with merge headers (`<<Full_Name>>`, `<<Level_Strand_Course>>`, `<<Event_Title>>`, `<<Event_Date>>`) for Google Sheets AutoCrat / Document Studio.
3. Press **Ctrl+C** in the server terminal window to shut down cleanly and copy `data/attendance.db` + your exported CSV files for backup.

---

### Prepared by: CS31A ACLC College of Mandaue
