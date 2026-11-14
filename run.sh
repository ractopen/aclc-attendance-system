#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "============================================================"
echo "  ACLC College of Mandaue - HIV Awareness Seminar"
echo "  Offline Attendance, Evaluation & Demographics System"
echo "============================================================"

# 1. Verify Python 3 is installed
if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 is not installed. Please install Python 3.10 or newer."
    exit 1
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
    ./venv/bin/python3 -m pip install --upgrade pip --quiet
    ./venv/bin/python3 -m pip install -r requirements.txt
fi

# 3. Verify all required packages are installed
if ! ./venv/bin/python3 -c "import fastapi, uvicorn, qrcode, PIL, jinja2, user_agents" >/dev/null 2>&1; then
    echo "[INFO] Installing required dependencies from requirements.txt..."
    ./venv/bin/python3 -m pip install -r requirements.txt
fi

# 4. Find available port (Default: 8080, Fallback: 8000, 8081)
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

LAN_IP=$(./venv/bin/python3 -c "from main import get_local_ip; print(get_local_ip())" 2>/dev/null || echo "192.168.1.100")

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
echo "  Press Ctrl+C at any time to stop the server."
echo ""

exec ./venv/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --reload
