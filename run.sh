#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "============================================================"
echo "  ACLC College of Mandaue - HIV Awareness Seminar"
echo "  Offline Attendance, Evaluation & Demographics System"
echo "============================================================"

# 1. Verify Python 3 is installed (auto-install attempt on supported Linux distros if missing)
if ! command -v python3 >/dev/null 2>&1; then
    echo "[WARN] python3 is not installed. Attempting automatic installation..."
    if command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 python3-pip || true
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip || true
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -Sy --noconfirm python python-pip || true
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        echo "[ERROR] python3 is not installed. Please install Python 3.10 or newer."
        exit 1
    fi
fi

# 2. Check if venv exists and is valid for this machine/directory
NEED_VENV_BUILD=0
if [ ! -x "./venv/bin/python3" ]; then
    NEED_VENV_BUILD=1
elif ! ./venv/bin/python3 -c "import sys" >/dev/null 2>&1; then
    NEED_VENV_BUILD=1
fi

if [ "$NEED_VENV_BUILD" -eq 1 ]; then
    echo "[INFO] Setting up clean Linux virtual environment..."
    rm -rf ./venv
    python3 -m venv venv
    ./venv/bin/python3 -m pip install --upgrade pip --quiet || echo "[WARN] Offline or pip upgrade skipped."
    ./venv/bin/python3 -m pip install -r requirements.txt
else
    # Keep pip updated to latest stable when internet is available (offline-safe)
    ./venv/bin/python3 -m pip install --upgrade pip --quiet >/dev/null 2>&1 || true
fi

# 3. Verify all required packages are installed
if ! ./venv/bin/python3 -c "import fastapi, uvicorn, qrcode, PIL, jinja2, user_agents" >/dev/null 2>&1; then
    echo "[INFO] Installing required dependencies from requirements.txt..."
    ./venv/bin/python3 -m pip install -r requirements.txt
fi

# 4. Find available port (Default: 8080, Fallback: 8000, 8081, 8888)
PORT=${PORT:-8080}
PORT=$(./venv/bin/python3 -c "
import socket, sys
pref = int(sys.argv[1])
for p in (pref, 8080, 8000, 8081, 8888):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('0.0.0.0', p))
            print(p)
            break
        except OSError:
            continue
else:
    print(pref)
" "$PORT")
export PORT

# 5. Gather System & Network Diagnostics
PY_VER=$(./venv/bin/python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
NET_DIAG=$(./venv/bin/python3 -c "
from main import get_network_status_info
info = get_network_status_info()
print(f\"{info['active_ssid']}|{info['target_ssid']}|{'1' if info['is_event_wifi'] else '0'}|{info['lan_ip']}\")
" 2>/dev/null || echo "Unknown|HIV Awareness Attendance|0|192.168.0.100")

IFS='|' read -r ACTIVE_SSID TARGET_SSID IS_EVENT_WIFI LAN_IP <<< "$NET_DIAG"

echo ""
echo "[SYSTEM CHECK]"
echo "  * Python Version:        $PY_VER (Virtualenv Ready)"
echo "  * Required Packages:     Verified (Offline-ready)"
echo ""
echo "[WI-FI & NETWORK STATUS]"
echo "  * Active Wi-Fi SSID:     $ACTIVE_SSID"
echo "  * Target Event Router:   $TARGET_SSID"
echo "  * Detected LAN IP:       $LAN_IP"
if [ "$IS_EVENT_WIFI" = "1" ]; then
    echo "  * Connection Status:     [OK] Ready on Event Network"
else
    echo "  * Connection Status:     [NOTICE] Not connected to '$TARGET_SSID'"
    echo "    -> To connect now:     nmcli con up \"$TARGET_SSID\""
fi
echo ""
echo "============================================================"
echo "  SERVER READY & LISTENING ON PORT $PORT"
echo "============================================================"
echo "  * Student Page (Local):       http://localhost:$PORT/"
echo "  * Student Page (Wi-Fi/LAN):   http://$LAN_IP:$PORT/"
echo "  * Projector Big Screen:       http://localhost:$PORT/projector"
echo "  * Admin Control Dashboard:    http://localhost:$PORT/admin"
echo "  * Exit QR Pass Scanner:       http://localhost:$PORT/admin/scanner"
echo "  * Admin Master Password:      nevergrowold"
echo "============================================================"
echo "  OPERATING INSTRUCTIONS:"
echo "  1. Ensure host laptop is connected to '$TARGET_SSID'."
echo "  2. Open http://localhost:$PORT/projector and press F11."
echo "  3. Students scan Left QR (Wi-Fi), then Right QR (Student Portal)."
echo "  4. Press Ctrl+C at any time to stop the server cleanly."
echo "============================================================"
echo ""

exec ./venv/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
