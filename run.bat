@echo off
cd /d "%~dp0backend"
if not exist ..\.venv ( python -m venv ..\.venv )
call ..\.venv\Scripts\activate
pip install -r requirements.txt
echo.
echo Open http://localhost:8000 in your browser (login: admin / admin123)
uvicorn main:app --host 0.0.0.0 --port 8000
