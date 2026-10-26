"""Daily backup: copies medstock.db into ../backup/ with the date in the name, keeps the last 30.
Run it by hand, or schedule it (Windows Task Scheduler / cron):   python backup.py"""
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import db

dest = Path(__file__).resolve().parent.parent / "backup"
dest.mkdir(exist_ok=True)
target = dest / f"medstock-{date.today()}.db"
src, out = sqlite3.connect(db.DB_PATH), sqlite3.connect(target)
src.backup(out)                    # safe even while the app is running
out.close(); src.close()
for old in sorted(dest.glob("medstock-*.db"))[:-30]:
    old.unlink()
print("Backup saved:", target)
