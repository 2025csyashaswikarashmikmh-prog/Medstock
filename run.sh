#!/usr/bin/env bash
cd "$(dirname "$0")/backend"
[ -d ../.venv ] || python3 -m venv ../.venv
source ../.venv/bin/activate
pip install -r requirements.txt
echo; echo "Open http://localhost:8000 in your browser (login: admin / admin123)"
uvicorn main:app --host 0.0.0.0 --port 8000
