"""Daily alerts: build the message, send e-mail (smtplib, free) and WhatsApp click-to-chat links."""
import os
import smtplib
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import db
import reports
import services


def load_env_file():
    """Tiny .env loader (backend/.env) so students don't need python-dotenv."""
    path = db.BASE_DIR / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_alert():
    meds = [services.enrich(r) for r in db.q("SELECT * FROM medicines WHERE quantity>0")]
    expired = [m for m in meds if m["status"] == "expired"]
    week = [m for m in meds if 0 <= m["days_left"] <= 7]
    month = [m for m in meds if 7 < m["days_left"] <= services.EXPIRY_WARNING_DAYS]
    low = [m for m in meds if m["low_stock"] and m["status"] != "expired"]
    refills = services.list_refills(due_within=3)
    reorder = reports.reorder_suggestions()

    def names(rows, extra=lambda m: ""):
        return ", ".join(f"{m['name']} ({m['batch']}{extra(m)})" for m in rows[:8]) + (
            f" +{len(rows) - 8} more" if len(rows) > 8 else "")

    lines = [f"{db.get_setting('shop_name')} - daily alert, {date.today():%d %b %Y}", ""]
    lines.append(f"{len(week)} medicine batch(es) expire within 7 days" + (f": {names(week, lambda m: f', {m['days_left']}d')}" if week else ""))
    lines.append(f"{len(month)} more expire within {services.EXPIRY_WARNING_DAYS} days" + (f": {names(month, lambda m: f', {m['days_left']}d')}" if month else ""))
    lines.append(f"{len(expired)} batch(es) already EXPIRED - remove from shelf" + (f": {names(expired)}" if expired else ""))
    lines.append(f"{len(low)} batch(es) low on stock" + (f": {names(low, lambda m: f', {m['quantity']} left')}" if low else ""))
    if reorder:
        lines.append("Reorder: " + "; ".join(r["message"] for r in reorder[:5]))
    if refills:
        lines.append("Refill reminders due: " + ", ".join(f"{r['customer_name']} - {r['medicine']}" for r in refills[:8]))
    text = "\n".join(lines)
    return {
        "text": text,
        "counts": {"expiring_7d": len(week), "expiring_30d": len(month), "expired": len(expired),
                   "low_stock": len(low), "refills_due": len(refills), "reorder": len(reorder)},
        "whatsapp_link": "https://wa.me/?text=" + urllib.parse.quote(text),
    }


def smtp_configured():
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_TO"))


def send_email(subject, body, to=None):
    if not smtp_configured():
        raise services.BusinessError(
            "E-mail is not configured. Copy backend/.env.example to backend/.env and fill in SMTP_* and ALERT_TO."
        )
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, os.environ["SMTP_USER"]
    msg["To"] = to or os.environ["ALERT_TO"]
    msg.set_content(body)
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise services.BusinessError(f"Could not send e-mail: {e}")


def send_daily_alert():
    alert = build_alert()
    send_email(f"MedStock alert - {date.today():%d %b %Y}", alert["text"])
    db.set_setting("alert_last_sent", date.today().isoformat())
    return alert


def _loop():
    hour = int(os.environ.get("ALERT_HOUR", "9"))
    while True:
        try:
            if (smtp_configured() and datetime.now().hour >= hour
                    and db.get_setting("alert_last_sent") != date.today().isoformat()):
                send_daily_alert()
        except Exception as e:           # never let the scheduler thread die
            print("[alerts] daily e-mail failed:", e)
        time.sleep(300)


def start_scheduler():
    threading.Thread(target=_loop, daemon=True, name="daily-alerts").start()
