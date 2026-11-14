#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ -x "./venv/bin/python3" ] && ./venv/bin/python3 -c "import fastapi, httpx" >/dev/null 2>&1; then
    ./venv/bin/python3 tests/test_performance.py
else
    python3 tests/test_performance.py
fi
