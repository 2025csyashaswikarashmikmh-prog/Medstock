"""Business logic: inventory, FEFO billing, GST, suppliers & purchases, refills, returns."""
import math
from datetime import date, datetime, timedelta

import db
from db import EXPIRY_WARNING_DAYS, LOW_STOCK_LIMIT, RETURN_WINDOW_DAYS


class BusinessError(Exception):
    """Raised for rule violations; the API turns it into HTTP 400."""


def today_iso():
    return date.today().isoformat()


# ------------------------------------------------------------------- inventory
def enrich(row):
    days_left = (date.fromisoformat(row["expiry"]) - date.today()).days
    row["days_left"] = days_left
    row["status"] = "expired" if days_left < 0 else "expiring" if days_left <= EXPIRY_WARNING_DAYS else "ok"
    row["low_stock"] = row["quantity"] <= LOW_STOCK_LIMIT
    return row


def list_medicines():
    return [enrich(r) for r in db.q("SELECT * FROM medicines ORDER BY expiry ASC, name")]


def product_where(med):
    """SQL fragment + params selecting every batch of the same product (barcode, else name)."""
    if med.get("barcode"):
        return "barcode = ?", (med["barcode"],)
    return "lower(name) = lower(?)", (med["name"],)


def fefo_batches(conn, med):
    """Sellable batches of this product, earliest expiry first (FEFO)."""
    where, params = product_where(med)
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM medicines WHERE {where} AND quantity > 0 AND expiry >= ? "
            "ORDER BY expiry ASC, id ASC",
            (*params, today_iso()),
        )
    ]


def lookup(code):
    """Find a product by barcode (scanner) or name fragment. Returns product summaries."""
    code = code.strip()
    rows = db.q("SELECT * FROM medicines WHERE barcode = ? ORDER BY expiry", (code,)) if code else []
    if not rows:
        rows = db.q(
            "SELECT * FROM medicines WHERE name LIKE ? OR generic_name LIKE ? ORDER BY name, expiry",
            (f"%{code}%", f"%{code}%"),
        )
    products = {}
    for r in rows:
        key = r["barcode"] or r["name"].lower()
        p = products.setdefault(
            key,
            {
                "medicine_id": None, "name": r["name"], "barcode": r["barcode"], "price": r["price"],
                "gst_rate": r["gst_rate"], "schedule_h": r["schedule_h"], "generic_name": r["generic_name"],
                "stock": 0, "next_expiry": None,
            },
        )
        enrich(r)
        if r["status"] != "expired" and r["quantity"] > 0:
            p["stock"] += r["quantity"]
            if p["next_expiry"] is None or r["expiry"] < p["next_expiry"]:
                p["next_expiry"] = r["expiry"]
                p["price"] = r["price"]            # price of the batch FEFO will sell first
                p["medicine_id"] = r["id"]
        if p["medicine_id"] is None:
            p["medicine_id"] = r["id"]
    return list(products.values())


def substitutes(medicine_id):
    """Same generic (salt), different brand, in stock and not expired."""
    med = db.q1("SELECT * FROM medicines WHERE id=?", (medicine_id,))
    if not med:
        raise BusinessError("Medicine not found")
    if not med["generic_name"]:
        return []
    rows = db.q(
        "SELECT MIN(name) AS name, MIN(price) AS price, SUM(quantity) AS stock, MIN(expiry) AS next_expiry, "
        "MIN(id) AS medicine_id, MIN(gst_rate) AS gst_rate, MAX(schedule_h) AS schedule_h FROM medicines "
        "WHERE lower(generic_name)=lower(?) AND lower(name) != lower(?) AND quantity>0 AND expiry>=? "
        "GROUP BY lower(name) ORDER BY price",
        (med["generic_name"], med["name"], today_iso()),
    )
    return rows


# --------------------------------------------------------------------- GST math
def split_gst(total, rate):
    """Prices (MRP) include GST in India: pull the taxable value and GST out of the total."""
    taxable = round(total / (1 + rate / 100), 2)
    return taxable, round(total - taxable, 2)


# ---------------------------------------------------------------------- billing
def create_bill(user, data):
    """data: customer_name, customer_phone, doctor_name, payment_mode, discount_pct, items[{medicine_id, qty}]"""
    items = data["items"]
    if not items:
        raise BusinessError("Add at least one medicine to the bill")
    disc = float(data.get("discount_pct") or 0)
    if not 0 <= disc <= 100:
        raise BusinessError("Discount must be between 0 and 100")

    with db.tx() as conn:
        lines = []
        for it in items:
            med = conn.execute("SELECT * FROM medicines WHERE id=?", (it["medicine_id"],)).fetchone()
            if not med:
                raise BusinessError(f"Medicine id {it['medicine_id']} not found")
            med = dict(med)
            need = int(it["qty"])
            if need < 1:
                raise BusinessError("Quantity must be at least 1")
            batches = fefo_batches(conn, med)
            available = sum(b["quantity"] for b in batches)
            if need > available:
                raise BusinessError(f"{med['name']}: only {available} unexpired units in stock")
            for b in batches:                    # FEFO: take from the earliest expiry first
                if need == 0:
                    break
                take = min(need, b["quantity"])
                conn.execute("UPDATE medicines SET quantity = quantity - ? WHERE id=?", (take, b["id"]))
                lines.append((b, take))
                need -= take

        if any(b["schedule_h"] for b, _ in lines):
            if not (data.get("customer_name") or "").strip() or not (data.get("doctor_name") or "").strip():
                raise BusinessError(
                    "Schedule H medicine in the bill: patient name and doctor name are required"
                )

        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO bills (created_at, customer_name, customer_phone, doctor_name, payment_mode, "
            "discount_pct, taxable, cgst, sgst, round_off, total, created_by) VALUES (?,?,?,?,?,?,0,0,0,0,0,?)",
            (
                now, (data.get("customer_name") or "").strip(), (data.get("customer_phone") or "").strip(),
                (data.get("doctor_name") or "").strip(), data.get("payment_mode") or "Cash", disc, user["id"],
            ),
        )
        bill_id = cur.lastrowid
        sum_taxable = sum_gst = sum_total = 0.0
        for b, qty in lines:
            line_total = round(qty * b["price"] * (1 - disc / 100), 2)
            taxable, gst = split_gst(line_total, b["gst_rate"])
            conn.execute(
                "INSERT INTO bill_items (bill_id, medicine_id, name, batch, expiry, qty, price, cost_price, "
                "gst_rate, taxable, gst_amount, total, schedule_h) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bill_id, b["id"], b["name"], b["batch"], b["expiry"], qty, b["price"], b["cost_price"],
                 b["gst_rate"], taxable, gst, line_total, b["schedule_h"]),
            )
            if b["schedule_h"]:
                conn.execute(
                    "INSERT INTO schedule_h_register (bill_id, sold_on, patient_name, patient_address, doctor_name, "
                    "doctor_reg_no, medicine, batch, qty) VALUES (?,?,?,?,?,?,?,?,?)",
                    (bill_id, now[:10], data["customer_name"].strip(), (data.get("patient_address") or "").strip(),
                     data["doctor_name"].strip(), (data.get("doctor_reg_no") or "").strip(),
                     b["name"], b["batch"], qty),
                )
            sum_taxable += taxable
            sum_gst += gst
            sum_total += line_total
        grand = float(round(sum_total))
        conn.execute(
            "UPDATE bills SET bill_no=?, taxable=?, cgst=?, sgst=?, round_off=?, total=? WHERE id=?",
            (f"INV-{bill_id:05d}", round(sum_taxable, 2), round(sum_gst / 2, 2), round(sum_gst / 2, 2),
             round(grand - sum_total, 2), grand, bill_id),
        )
    return get_bill(bill_id)


def get_bill(bill_id):
    bill = db.q1("SELECT * FROM bills WHERE id=?", (bill_id,))
    if not bill:
        raise BusinessError("Bill not found")
    bill["items"] = db.q("SELECT * FROM bill_items WHERE bill_id=? ORDER BY id", (bill_id,))
    return bill


def list_bills(limit=100, day=None):
    if day:
        return db.q("SELECT * FROM bills WHERE date(created_at)=? ORDER BY id DESC", (day,))
    return db.q("SELECT * FROM bills ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------ suppliers/purchases
def supplier_balances():
    rows = db.q(
        "SELECT s.*, "
        " COALESCE((SELECT SUM(total) FROM purchases p WHERE p.supplier_id=s.id),0) AS purchased, "
        " COALESCE((SELECT SUM(amount) FROM supplier_payments x WHERE x.supplier_id=s.id),0) AS paid "
        "FROM suppliers s ORDER BY s.name"
    )
    for r in rows:
        r["owed"] = round(r["purchased"] - r["paid"], 2)
    return rows


def create_purchase(user, data):
    """Stock entry from a supplier: adds/increments batches and records what is owed."""
    sup = db.q1("SELECT * FROM suppliers WHERE id=?", (data["supplier_id"],))
    if not sup:
        raise BusinessError("Supplier not found")
    if not data["items"]:
        raise BusinessError("Add at least one item")
    with db.tx() as conn:
        total = 0.0
        pid = conn.execute(
            "INSERT INTO purchases (supplier_id, invoice_no, purchase_date, total, notes, created_by) "
            "VALUES (?,?,?,0,?,?)",
            (sup["id"], data.get("invoice_no", ""), data.get("purchase_date") or today_iso(),
             data.get("notes", ""), user["id"]),
        ).lastrowid
        for it in data["items"]:
            name, batch = it["name"].strip(), it["batch"].strip().upper()
            known = conn.execute(
                "SELECT * FROM medicines WHERE lower(name)=lower(?) ORDER BY id DESC LIMIT 1", (name,)
            ).fetchone()
            known = dict(known) if known else {}
            gst = it.get("gst_rate") if it.get("gst_rate") is not None else known.get("gst_rate", 12)
            mrp = it.get("price") if it.get("price") is not None else known.get("price", 0)
            row = conn.execute(
                "SELECT id FROM medicines WHERE lower(name)=lower(?) AND batch=? AND expiry=?",
                (name, batch, it["expiry"]),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE medicines SET quantity=quantity+?, cost_price=?, price=?, supplier_id=? WHERE id=?",
                    (it["qty"], it["cost_price"], mrp, sup["id"], row["id"]),
                )
                mid = row["id"]
            else:
                mid = conn.execute(
                    "INSERT INTO medicines (name,batch,quantity,price,expiry,barcode,generic_name,category,hsn,"
                    "gst_rate,cost_price,supplier_id,schedule_h) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, batch, it["qty"], mrp, it["expiry"],
                     it.get("barcode") or known.get("barcode", ""),
                     it.get("generic_name") or known.get("generic_name", ""),
                     it.get("category") or known.get("category", "General"),
                     known.get("hsn", "3004"), gst, it["cost_price"], sup["id"],
                     int(it["schedule_h"]) if it.get("schedule_h") is not None else known.get("schedule_h", 0)),
                ).lastrowid
            conn.execute(
                "INSERT INTO purchase_items (purchase_id, medicine_id, name, batch, qty, cost_price, gst_rate) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, mid, name, batch, it["qty"], it["cost_price"], gst),
            )
            total += it["qty"] * it["cost_price"] * (1 + gst / 100)   # cost price is ex-GST
        total = round(total, 2)
        conn.execute("UPDATE purchases SET total=? WHERE id=?", (total, pid))
        paid = float(data.get("paid_now") or 0)
        if paid > 0:
            conn.execute(
                "INSERT INTO supplier_payments (supplier_id, amount, paid_on, note) VALUES (?,?,?,?)",
                (sup["id"], paid, today_iso(), f"Paid on purchase #{pid}"),
            )
    return {"id": pid, "total": total}


# ---------------------------------------------------------- returns to supplier
def return_list(days=RETURN_WINDOW_DAYS):
    """Stock that is expired or about to expire, grouped by the supplier it came from."""
    rows = db.q(
        "SELECT m.*, COALESCE(s.name,'Unassigned supplier') AS supplier_name FROM medicines m "
        "LEFT JOIN suppliers s ON s.id=m.supplier_id WHERE m.quantity>0 AND m.expiry <= ? "
        "ORDER BY supplier_name, m.expiry",
        ((date.today() + timedelta(days=days)).isoformat(),),
    )
    out = []
    for r in rows:
        enrich(r)
        r["value"] = round(r["quantity"] * r["cost_price"], 2)
        out.append(r)
    return out


# ------------------------------------------------------------------- refills
def refill_status(r):
    r["next_due"] = (date.fromisoformat(r["last_date"]) + timedelta(days=r["every_days"])).isoformat()
    r["days_to_due"] = (date.fromisoformat(r["next_due"]) - date.today()).days
    return r


def list_refills(due_within=None):
    rows = [refill_status(r) for r in db.q("SELECT * FROM refills WHERE active=1 ORDER BY id")]
    if due_within is not None:
        rows = [r for r in rows if r["days_to_due"] <= due_within]
    rows.sort(key=lambda r: r["days_to_due"])
    return rows


# --------------------------------------------------------------------- summary
def summary():
    rows = list_medicines()
    return {
        "total_batches": len(rows),
        "expired": sum(r["status"] == "expired" for r in rows),
        "expiring": sum(r["status"] == "expiring" for r in rows),
        "low_stock": sum(r["low_stock"] and r["status"] != "expired" for r in rows),
        "loss_value": round(sum(r["quantity"] * r["cost_price"] or r["quantity"] * r["price"]
                                for r in rows if r["status"] == "expired"), 2),
        "stock_value": round(sum(r["quantity"] * r["price"] for r in rows if r["status"] != "expired"), 2),
    }
