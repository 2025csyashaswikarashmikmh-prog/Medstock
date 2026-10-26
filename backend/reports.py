"""Sales reports, profit, dashboard chart data and smart reorder suggestions."""
import math
from collections import defaultdict
from datetime import date, timedelta

import db
from db import LOW_STOCK_LIMIT
from services import enrich, today_iso


def sales_by_day(days=30):
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = {
        r["d"]: r
        for r in db.q(
            "SELECT date(b.created_at) AS d, COUNT(*) AS bills, SUM(b.total) AS sales, "
            "COALESCE((SELECT SUM(i.taxable - i.qty*i.cost_price) FROM bill_items i "
            "          JOIN bills bb ON bb.id=i.bill_id WHERE date(bb.created_at)=date(b.created_at)),0) AS profit "
            "FROM bills b WHERE date(b.created_at) >= ? GROUP BY d",
            (start,),
        )
    }
    out = []
    for n in range(days):
        d = (date.today() - timedelta(days=days - 1 - n)).isoformat()
        r = rows.get(d)
        out.append({
            "date": d, "bills": r["bills"] if r else 0,
            "sales": round(r["sales"], 2) if r else 0, "profit": round(r["profit"], 2) if r else 0,
        })
    return out


def sales_by_month(months=12):
    return db.q(
        "SELECT strftime('%Y-%m', b.created_at) AS month, COUNT(*) AS bills, ROUND(SUM(b.total),2) AS sales, "
        "ROUND((SELECT SUM(i.taxable - i.qty*i.cost_price) FROM bill_items i JOIN bills bb ON bb.id=i.bill_id "
        "       WHERE strftime('%Y-%m', bb.created_at)=strftime('%Y-%m', b.created_at)),2) AS profit "
        "FROM bills b GROUP BY month ORDER BY month DESC LIMIT ?",
        (months,),
    )[::-1]


def top_selling(days=30, limit=10):
    since = (date.today() - timedelta(days=days)).isoformat()
    return db.q(
        "SELECT MIN(i.name) AS name, SUM(i.qty) AS units, ROUND(SUM(i.total),2) AS revenue, "
        "ROUND(SUM(i.taxable - i.qty*i.cost_price),2) AS profit FROM bill_items i "
        "JOIN bills b ON b.id=i.bill_id WHERE date(b.created_at) >= ? "
        "GROUP BY lower(i.name) ORDER BY units DESC LIMIT ?",
        (since, limit),
    )


def missing_cost_count():
    return db.q1("SELECT COUNT(*) AS n FROM medicines WHERE cost_price=0 AND quantity>0")["n"]


def totals_since(start_iso):
    r = db.q1(
        "SELECT COUNT(*) AS bills, COALESCE(SUM(total),0) AS sales FROM bills WHERE date(created_at) >= ?",
        (start_iso,),
    )
    p = db.q1(
        "SELECT COALESCE(SUM(i.taxable - i.qty*i.cost_price),0) AS profit FROM bill_items i "
        "JOIN bills b ON b.id=i.bill_id WHERE date(b.created_at) >= ?",
        (start_iso,),
    )
    return {"bills": r["bills"], "sales": round(r["sales"], 2), "profit": round(p["profit"], 2)}


def gst_summary(month=None):
    """GST collected per rate for a month (YYYY-MM) - handy at return-filing time."""
    month = month or today_iso()[:7]
    return db.q(
        "SELECT i.gst_rate AS gst_rate, ROUND(SUM(i.taxable),2) AS taxable, ROUND(SUM(i.gst_amount)/2,2) AS cgst, "
        "ROUND(SUM(i.gst_amount)/2,2) AS sgst, ROUND(SUM(i.total),2) AS total FROM bill_items i "
        "JOIN bills b ON b.id=i.bill_id WHERE strftime('%Y-%m', b.created_at)=? GROUP BY i.gst_rate ORDER BY i.gst_rate",
        (month,),
    )


def dashboard():
    meds = [enrich(r) for r in db.q("SELECT * FROM medicines WHERE quantity>0")]
    # expiry timeline: stock value expiring in each of the next 6 months (+ already expired)
    buckets = [("Expired", 0)] + [(f"+{m}m", 0) for m in range(1, 7)] + [("Later", 0)]
    timeline = defaultdict(float)
    for r in meds:
        d = r["days_left"]
        label = "Expired" if d < 0 else "Later" if d > 180 else f"+{max(1, math.ceil(d / 30))}m"
        timeline[label] += r["quantity"] * r["price"]
    cats = defaultdict(float)
    for r in meds:
        if r["status"] != "expired":
            cats[r["category"] or "General"] += r["quantity"] * r["price"]
    return {
        "sales_trend": sales_by_day(30),
        "expiry_timeline": [{"label": l, "value": round(timeline[l], 2)} for l, _ in buckets],
        "category_split": [{"label": k, "value": round(v, 2)} for k, v in sorted(cats.items(), key=lambda x: -x[1])],
        "top_selling": top_selling(30, 5),
        "today": totals_since(today_iso()),
        "month": totals_since(today_iso()[:8] + "01"),
        "missing_cost_prices": missing_cost_count(),
    }


def reorder_suggestions(window=30, cover_days=30, alert_days=10):
    """From the last `window` days of sales: how long will current stock last?"""
    since = (date.today() - timedelta(days=window)).isoformat()
    sold = {
        r["k"]: r["units"]
        for r in db.q(
            "SELECT lower(i.name) AS k, SUM(i.qty) AS units FROM bill_items i JOIN bills b ON b.id=i.bill_id "
            "WHERE date(b.created_at) >= ? GROUP BY lower(i.name)",
            (since,),
        )
    }
    stock = db.q(
        "SELECT lower(name) AS k, MIN(name) AS name, SUM(quantity) AS stock FROM medicines "
        "WHERE expiry >= ? GROUP BY lower(name)",
        (today_iso(),),
    )
    out = []
    for s in stock:
        velocity = sold.get(s["k"], 0) / window            # units per day
        if velocity > 0:
            days_cover = s["stock"] / velocity
            if days_cover > alert_days and s["stock"] > LOW_STOCK_LIMIT:
                continue
            order_qty = max(1, math.ceil(velocity * cover_days - s["stock"]))
            msg = (f"{s['name']} will run out in about {int(days_cover)} days - order ~{order_qty} units now"
                   if s["stock"] > 0 else f"{s['name']} is OUT of stock - order ~{order_qty} units")
        elif s["stock"] <= LOW_STOCK_LIMIT:
            days_cover, order_qty = None, LOW_STOCK_LIMIT * 2
            msg = f"{s['name']} is low ({s['stock']} left) and has no recent sales data - consider reordering"
        else:
            continue
        out.append({"name": s["name"], "stock": s["stock"], "daily_sales": round(velocity, 2),
                    "days_left": None if days_cover is None else round(days_cover, 1),
                    "suggested_order": order_qty, "message": msg})
    out.sort(key=lambda r: (r["days_left"] is None, r["days_left"] or 0))
    return out
