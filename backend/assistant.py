"""AI assistant.

1. Ask in plain English ("which medicines expire next month?") - answered offline by rules, no API key needed.
2. Upload a photo of a supplier bill - read with Claude's vision API when ANTHROPIC_API_KEY is set. The result is a
   DRAFT purchase list that the user reviews before anything is saved.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta

import db
import reports
import services


def _month_range(offset):
    first = date.today().replace(day=1)
    for _ in range(abs(offset)):
        first = (first + timedelta(days=32 if offset > 0 else -1)).replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


def _expiring_between(a, b):
    return [m for m in services.list_medicines()
            if m["quantity"] > 0 and a.isoformat() <= m["expiry"] <= b.isoformat()]


def _fmt(rows):
    return [{"name": m["name"], "batch": m["batch"], "qty": m["quantity"], "expiry": m["expiry"]} for m in rows]


def ask(question, role):
    qn = question.lower().strip()
    today = date.today()
    if not qn:
        return {"answer": "Ask me something like: which medicines expire next month?", "rows": []}

    if "expired" in qn and "expire" not in qn.replace("expired", ""):
        rows = [m for m in services.list_medicines() if m["status"] == "expired" and m["quantity"] > 0]
        return {"answer": f"{len(rows)} expired batch(es) still in stock.", "rows": _fmt(rows)}

    if "expir" in qn:
        m = re.search(r"(\d+)\s*days?", qn)
        if "next month" in qn:
            a, b = _month_range(1)
            label = f"in {a:%B %Y}"
        elif "this month" in qn:
            a, b = today, _month_range(0)[1]
            label = "by the end of this month"
        elif "week" in qn:
            a, b, label = today, today + timedelta(days=7), "within 7 days"
        elif m:
            a, b, label = today, today + timedelta(days=int(m.group(1))), f"within {m.group(1)} days"
        else:
            a, b, label = today, today + timedelta(days=services.EXPIRY_WARNING_DAYS), "within 30 days"
        rows = _expiring_between(a, b)
        return {"answer": f"{len(rows)} batch(es) expire {label}.", "rows": _fmt(rows)}

    if "out of stock" in qn or "low stock" in qn or "running low" in qn:
        rows = [m for m in services.list_medicines() if m["low_stock"] and m["status"] != "expired"]
        return {"answer": f"{len(rows)} batch(es) are low on stock (≤ {services.LOW_STOCK_LIMIT} units).", "rows": _fmt(rows)}

    if "reorder" in qn or "order" in qn or "run out" in qn:
        sug = reports.reorder_suggestions()
        return {"answer": "\n".join(s["message"] for s in sug) or "Nothing needs reordering right now.",
                "rows": []}

    sub = re.search(r"(?:substitute|alternative|replace(?:ment)?)\s+(?:for|of)\s+(.+)", qn)
    if sub:
        found = services.lookup(sub.group(1).strip("?. "))
        if not found:
            return {"answer": "I could not find that medicine.", "rows": []}
        alts = services.substitutes(found[0]["medicine_id"])
        return {"answer": (f"{len(alts)} in-stock alternative(s) with the same salt as {found[0]['name']}."
                           if alts else f"No in-stock alternative for {found[0]['name']}."),
                "rows": [{"name": a["name"], "qty": a["stock"], "price": a["price"]} for a in alts]}

    if "sales" in qn or "sold" in qn or "revenue" in qn:
        if "top" in qn or "best" in qn or "most" in qn:
            rows = reports.top_selling(30, 5)
            return {"answer": "Top sellers in the last 30 days.",
                    "rows": [{"name": r["name"], "units": r["units"], "revenue": r["revenue"]} for r in rows]}
        if "month" in qn:
            t = reports.totals_since(today.replace(day=1).isoformat())
            return {"answer": f"This month: ₹{t['sales']:,.2f} from {t['bills']} bills.", "rows": []}
        t = reports.totals_since(today.isoformat())
        return {"answer": f"Today: ₹{t['sales']:,.2f} from {t['bills']} bills.", "rows": []}

    if "top" in qn and ("sell" in qn or "medicine" in qn):
        rows = reports.top_selling(30, 5)
        return {"answer": "Top sellers in the last 30 days.",
                "rows": [{"name": r["name"], "units": r["units"], "revenue": r["revenue"]} for r in rows]}

    if "profit" in qn:
        if role != "owner":
            return {"answer": "Only the owner can see profit.", "rows": []}
        t = reports.totals_since(today.replace(day=1).isoformat())
        return {"answer": f"Profit this month: ₹{t['profit']:,.2f} (on ₹{t['sales']:,.2f} sales).", "rows": []}

    if re.search(r"\bowe\b|\bpayable\b", qn) or ("due" in qn and "supplier" in qn):
        if role != "owner":
            return {"answer": "Only the owner can see supplier dues.", "rows": []}
        rows = [s for s in services.supplier_balances() if s["owed"] > 0]
        return {"answer": f"You owe ₹{sum(s['owed'] for s in rows):,.2f} to {len(rows)} supplier(s).",
                "rows": [{"supplier": s["name"], "owed": s["owed"]} for s in rows]}

    if "refill" in qn:
        rows = services.list_refills(due_within=7)
        return {"answer": f"{len(rows)} refill reminder(s) due within 7 days.",
                "rows": [{"customer": r["customer_name"], "medicine": r["medicine"], "due": r["next_due"]} for r in rows]}

    stock = re.search(r"(?:stock|how many|quantity|have)\s+(?:of\s+|do we have\s+)?(.+)", qn)
    if stock:
        found = services.lookup(stock.group(1).strip("?. "))
        if found:
            return {"answer": "\n".join(f"{p['name']}: {p['stock']} units in stock" for p in found[:5]), "rows": []}

    found = services.lookup(qn.strip("?. "))
    if found:
        return {"answer": "\n".join(f"{p['name']}: {p['stock']} units, ₹{p['price']}" for p in found[:5]), "rows": []}
    return {"answer": "I didn't understand. Try: 'medicines expiring next month', 'low stock', 'stock of Paracetamol', "
                      "'substitute for Crocin', 'sales today', 'top selling', 'what should I reorder'.", "rows": []}


# ------------------------------------------------------------ supplier bill photo
def read_bill_photo(image_bytes, media_type):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise services.BusinessError(
            "Bill-photo reading needs an Anthropic API key. Set ANTHROPIC_API_KEY in backend/.env."
        )
    if media_type not in ("image/jpeg", "image/png", "image/webp"):
        raise services.BusinessError("Upload a JPG, PNG or WEBP photo")
    prompt = (
        "This is a pharmacy supplier invoice. Extract every medicine line as JSON only, no commentary: "
        '{"supplier":"", "invoice_no":"", "items":[{"name":"","batch":"","expiry":"YYYY-MM-DD",'
        '"qty":0,"cost_price":0,"mrp":0,"gst_rate":12}]}. cost_price = purchase rate per single unit excluding GST. '
        "Use the last day of the month when only MM/YY expiry is printed. Use null for anything unreadable."
    )
    body = json.dumps({
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image_bytes).decode()}},
            {"type": "text", "text": prompt}]}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            text = json.loads(r.read())["content"][0]["text"]
        draft = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (urllib.error.URLError, ValueError, KeyError, IndexError) as e:
        raise services.BusinessError(f"Could not read the bill photo: {e}")
    draft["note"] = "AI draft - check every line (batch, expiry, quantity, price) before saving."
    return draft
