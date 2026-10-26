"""MedStock Pharmacy - API server (FastAPI).  Run:  uvicorn main:app --reload"""
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import alerts
import assistant
import db
import documents
import reports
import security
import services
from services import BusinessError, enrich

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@asynccontextmanager
async def lifespan(app):
    alerts.load_env_file()
    db.init_db()
    alerts.start_scheduler()
    yield


app = FastAPI(title="MedStock Pharmacy", lifespan=lifespan)
# Same-origin by default (the frontend is served by this app). Open CORS only if you host the UI elsewhere.
app.add_middleware(CORSMiddleware, allow_origins=[], allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------------ auth
def current_user(authorization: Optional[str] = Header(default=None), token: Optional[str] = Query(default=None)):
    raw = token
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:]
    user = security.read_token(raw) if raw else None
    if not user:
        raise HTTPException(status_code=401, detail="Please log in")
    return user


def owner_only(user=Depends(current_user)):
    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Owner access required")
    return user


def guard(fn, *args, **kwargs):
    """Turn business-rule errors into HTTP 400 responses."""
    try:
        return fn(*args, **kwargs)
    except BusinessError as e:
        raise HTTPException(status_code=400, detail=str(e))


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=100)


@app.post("/api/login")
def login(body: LoginIn):
    name = body.username.strip().lower()
    if security.locked(name):
        raise HTTPException(status_code=429, detail="Too many wrong attempts. Try again in 5 minutes.")
    row = db.q1("SELECT * FROM users WHERE lower(username)=? AND active=1", (name,))
    if not row or not db.verify_password(body.password, row["pw_hash"]):
        security.record_failure(name)
        raise HTTPException(status_code=401, detail="Wrong username or password")
    security.clear_failures(name)
    return {
        "token": security.make_token(row["id"]),
        "user": {"id": row["id"], "username": row["username"], "full_name": row["full_name"], "role": row["role"]},
        "default_password": body.password in ("admin123", "staff123"),
    }


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=100)


@app.post("/api/me/password")
def change_password(body: PasswordIn, user=Depends(current_user)):
    row = db.q1("SELECT pw_hash FROM users WHERE id=?", (user["id"],))
    if not db.verify_password(body.old_password, row["pw_hash"]):
        raise HTTPException(status_code=400, detail="Old password is wrong")
    db.execute("UPDATE users SET pw_hash=? WHERE id=?", (db.hash_password(body.new_password), user["id"]))
    return {"ok": True}


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    full_name: str = Field(default="", max_length=80)
    password: str = Field(min_length=6, max_length=100)
    role: str = Field(pattern="^(owner|staff)$")


@app.get("/api/users")
def list_users(_=Depends(owner_only)):
    return db.q("SELECT id, username, full_name, role, active FROM users ORDER BY id")


@app.post("/api/users")
def add_user(body: UserIn, _=Depends(owner_only)):
    if db.q1("SELECT 1 FROM users WHERE lower(username)=lower(?)", (body.username,)):
        raise HTTPException(status_code=400, detail="Username already exists")
    cur = db.execute(
        "INSERT INTO users (username, full_name, pw_hash, role) VALUES (?,?,?,?)",
        (body.username.strip(), body.full_name.strip(), db.hash_password(body.password), body.role),
    )
    return {"id": cur.lastrowid}


@app.put("/api/users/{uid}/active")
def toggle_user(uid: int, active: bool, user=Depends(owner_only)):
    if uid == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot disable your own account")
    db.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, uid))
    return {"ok": True}


# --------------------------------------------------------------------- settings
class SettingsIn(BaseModel):
    shop_name: str = Field(max_length=80)
    shop_address: str = Field(max_length=200)
    shop_phone: str = Field(default="", max_length=30)
    shop_gstin: str = Field(default="", max_length=20)
    shop_dl_no: str = Field(default="", max_length=60)
    invoice_footer: str = Field(default="", max_length=200)


@app.get("/api/settings")
def get_settings(_=Depends(current_user)):
    return {k: db.get_setting(k) for k in SettingsIn.model_fields}


@app.put("/api/settings")
def put_settings(body: SettingsIn, _=Depends(owner_only)):
    for k, v in body.model_dump().items():
        db.set_setting(k, v.strip())
    return {"ok": True}


# -------------------------------------------------------------------- inventory
class MedicineIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    batch: str = Field(min_length=1, max_length=30)
    quantity: int = Field(ge=0, le=100000)
    price: float = Field(ge=0, description="MRP / selling price per unit, GST included")
    expiry: date
    barcode: str = Field(default="", max_length=40)
    generic_name: str = Field(default="", max_length=80)
    category: str = Field(default="General", max_length=40)
    hsn: str = Field(default="3004", max_length=12)
    gst_rate: float = Field(default=12, ge=0, le=28)
    cost_price: float = Field(default=0, ge=0, description="Purchase price per unit, excluding GST")
    supplier_id: Optional[int] = None
    schedule_h: bool = False


def _med_params(m: MedicineIn):
    return (m.name.strip(), m.batch.strip().upper(), m.quantity, m.price, m.expiry.isoformat(),
            m.barcode.strip(), m.generic_name.strip().lower(), m.category.strip() or "General", m.hsn.strip(),
            m.gst_rate, m.cost_price, m.supplier_id, int(m.schedule_h))


@app.get("/api/medicines")
def list_medicines(_=Depends(current_user)):
    return services.list_medicines()          # earliest expiry first (FEFO order)


@app.post("/api/medicines")
def add_medicine(m: MedicineIn, _=Depends(owner_only)):
    cur = db.execute(
        "INSERT INTO medicines (name,batch,quantity,price,expiry,barcode,generic_name,category,hsn,gst_rate,"
        "cost_price,supplier_id,schedule_h) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", _med_params(m))
    return {"id": cur.lastrowid}


@app.put("/api/medicines/{med_id}")
def edit_medicine(med_id: int, m: MedicineIn, _=Depends(owner_only)):
    cur = db.execute(
        "UPDATE medicines SET name=?,batch=?,quantity=?,price=?,expiry=?,barcode=?,generic_name=?,category=?,hsn=?,"
        "gst_rate=?,cost_price=?,supplier_id=?,schedule_h=? WHERE id=?", (*_med_params(m), med_id))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Medicine not found")
    return {"ok": True}


@app.delete("/api/medicines/{med_id}")
def delete_medicine(med_id: int, _=Depends(owner_only)):
    cur = db.execute("DELETE FROM medicines WHERE id = ?", (med_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Medicine not found")
    return {"ok": True}


@app.get("/api/lookup")
def lookup(code: str = Query(min_length=1), _=Depends(current_user)):
    """Barcode scanner target: a USB scanner types the code + Enter like a keyboard."""
    return services.lookup(code)


@app.get("/api/substitutes/{med_id}")
def substitutes(med_id: int, _=Depends(current_user)):
    return guard(services.substitutes, med_id)


@app.get("/api/summary")
def summary(_=Depends(current_user)):
    return services.summary()


# ---------------------------------------------------------------------- billing
class BillItemIn(BaseModel):
    medicine_id: int
    qty: int = Field(ge=1, le=10000)


class BillIn(BaseModel):
    customer_name: str = Field(default="", max_length=80)
    customer_phone: str = Field(default="", max_length=20)
    doctor_name: str = Field(default="", max_length=80)
    doctor_reg_no: str = Field(default="", max_length=40)
    patient_address: str = Field(default="", max_length=160)
    payment_mode: str = Field(default="Cash", pattern="^(Cash|UPI|Card|Credit)$")
    discount_pct: float = Field(default=0, ge=0, le=100)
    items: List[BillItemIn] = Field(min_length=1, max_length=100)


@app.post("/api/bills")
def create_bill(body: BillIn, user=Depends(current_user)):
    return guard(services.create_bill, user, body.model_dump())


@app.get("/api/bills")
def list_bills(day: Optional[str] = None, limit: int = Query(default=100, le=500), _=Depends(current_user)):
    return services.list_bills(limit=limit, day=day)


@app.get("/api/bills/{bill_id}")
def get_bill(bill_id: int, _=Depends(current_user)):
    return guard(services.get_bill, bill_id)


@app.get("/api/bills/{bill_id}/pdf")
def bill_pdf(bill_id: int, _=Depends(current_user)):
    bill = guard(services.get_bill, bill_id)
    return Response(documents.invoice_pdf(bill), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{bill["bill_no"]}.pdf"'})


# ---------------------------------------------------------------------- reports
@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    data = reports.dashboard()
    if user["role"] != "owner":                       # staff never see profit
        for k in ("profit",):
            data["today"].pop(k, None)
            data["month"].pop(k, None)
        for row in data["sales_trend"]:
            row.pop("profit", None)
        for row in data["top_selling"]:
            row.pop("profit", None)
        data.pop("missing_cost_prices", None)
    return data


@app.get("/api/reports/sales")
def report_sales(period: str = Query(default="day", pattern="^(day|month)$"), days: int = Query(default=30, ge=1, le=366),
                 _=Depends(owner_only)):
    return reports.sales_by_day(days) if period == "day" else reports.sales_by_month()


@app.get("/api/reports/top")
def report_top(days: int = Query(default=30, ge=1, le=366), _=Depends(owner_only)):
    return reports.top_selling(days, 15)


@app.get("/api/reports/gst")
def report_gst(month: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"), _=Depends(owner_only)):
    return reports.gst_summary(month)


@app.get("/api/reorder")
def reorder(_=Depends(current_user)):
    return reports.reorder_suggestions()


# ---------------------------------------------------- suppliers & purchase entry
class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(default="", max_length=20)
    email: str = Field(default="", max_length=80)
    gstin: str = Field(default="", max_length=20)
    address: str = Field(default="", max_length=200)


@app.get("/api/suppliers")
def list_suppliers(_=Depends(owner_only)):
    return services.supplier_balances()


@app.post("/api/suppliers")
def add_supplier(s: SupplierIn, _=Depends(owner_only)):
    cur = db.execute("INSERT INTO suppliers (name, phone, email, gstin, address) VALUES (?,?,?,?,?)",
                     (s.name.strip(), s.phone, s.email, s.gstin, s.address))
    return {"id": cur.lastrowid}


class PurchaseItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    batch: str = Field(min_length=1, max_length=30)
    qty: int = Field(ge=1, le=100000)
    expiry: date
    cost_price: float = Field(ge=0, description="per unit, excluding GST")
    price: Optional[float] = Field(default=None, ge=0, description="MRP per unit")
    gst_rate: Optional[float] = Field(default=None, ge=0, le=28)
    barcode: str = ""
    generic_name: str = ""
    category: str = ""
    schedule_h: Optional[bool] = None


class PurchaseIn(BaseModel):
    supplier_id: int
    invoice_no: str = Field(default="", max_length=40)
    purchase_date: Optional[date] = None
    paid_now: float = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=200)
    items: List[PurchaseItemIn] = Field(min_length=1, max_length=200)


@app.post("/api/purchases")
def add_purchase(p: PurchaseIn, user=Depends(owner_only)):
    data = p.model_dump(mode="json")
    return guard(services.create_purchase, user, data)


@app.get("/api/purchases")
def list_purchases(_=Depends(owner_only)):
    return db.q("SELECT p.*, s.name AS supplier_name FROM purchases p JOIN suppliers s ON s.id=p.supplier_id "
                "ORDER BY p.id DESC LIMIT 100")


class PaymentIn(BaseModel):
    amount: float = Field(gt=0)
    note: str = Field(default="", max_length=100)


@app.post("/api/suppliers/{sid}/payments")
def pay_supplier(sid: int, body: PaymentIn, _=Depends(owner_only)):
    if not db.q1("SELECT 1 FROM suppliers WHERE id=?", (sid,)):
        raise HTTPException(status_code=404, detail="Supplier not found")
    db.execute("INSERT INTO supplier_payments (supplier_id, amount, paid_on, note) VALUES (?,?,?,?)",
               (sid, body.amount, date.today().isoformat(), body.note))
    return {"ok": True}


# ------------------------------------------------------------ returns to supplier
@app.get("/api/returns")
def returns(days: int = Query(default=90, ge=1, le=365), _=Depends(owner_only)):
    return services.return_list(days)


def _returns_rows(rows):
    return [[r["supplier_name"], r["name"], r["batch"], r["expiry"], r["quantity"], r["cost_price"], r["value"],
             "EXPIRED" if r["days_left"] < 0 else f"{r['days_left']} days"] for r in rows]


RETURN_HEAD = ["Supplier", "Medicine", "Batch", "Expiry", "Qty", "Cost/unit", "Value (Rs.)", "Status"]


@app.get("/api/returns/pdf")
def returns_pdf(days: int = Query(default=90, ge=1, le=365), _=Depends(owner_only)):
    rows = services.return_list(days)
    pdf = documents.table_pdf(
        "Return-to-supplier list", RETURN_HEAD, _returns_rows(rows),
        f"Generated {date.today():%d %b %Y}. Stock expired or expiring within {days} days. "
        "Check each supplier's return policy - not every supplier accepts every batch.")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="return-to-supplier.pdf"'})


# ---------------------------------------------------------------------- refills
class RefillIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=80)
    phone: str = Field(default="", max_length=20)
    medicine: str = Field(min_length=1, max_length=80)
    every_days: int = Field(default=30, ge=1, le=365)
    last_date: Optional[date] = None
    notes: str = Field(default="", max_length=120)


@app.get("/api/refills")
def list_refills(_=Depends(current_user)):
    return services.list_refills()


@app.post("/api/refills")
def add_refill(r: RefillIn, _=Depends(current_user)):
    cur = db.execute(
        "INSERT INTO refills (customer_name, phone, medicine, every_days, last_date, notes) VALUES (?,?,?,?,?,?)",
        (r.customer_name.strip(), r.phone.strip(), r.medicine.strip(), r.every_days,
         (r.last_date or date.today()).isoformat(), r.notes))
    return {"id": cur.lastrowid}


@app.put("/api/refills/{rid}/refilled")
def mark_refilled(rid: int, _=Depends(current_user)):
    cur = db.execute("UPDATE refills SET last_date=? WHERE id=?", (date.today().isoformat(), rid))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"ok": True}


@app.delete("/api/refills/{rid}")
def delete_refill(rid: int, _=Depends(current_user)):
    db.execute("UPDATE refills SET active=0 WHERE id=?", (rid,))
    return {"ok": True}


# ----------------------------------------------------------------- schedule H
@app.get("/api/schedule-h")
def schedule_h(_=Depends(current_user)):
    return db.q("SELECT * FROM schedule_h_register ORDER BY id DESC LIMIT 500")


SCHH_HEAD = ["Date", "Patient", "Doctor", "Doctor reg. no.", "Medicine", "Batch", "Qty"]


def _schh_rows():
    return [[r["sold_on"], r["patient_name"], r["doctor_name"], r["doctor_reg_no"], r["medicine"], r["batch"],
             r["qty"]] for r in db.q("SELECT * FROM schedule_h_register ORDER BY id")]


# ----------------------------------------------------------------------- alerts
@app.get("/api/alerts/preview")
def alert_preview(_=Depends(current_user)):
    data = alerts.build_alert()
    data["email_configured"] = alerts.smtp_configured()
    return data


@app.post("/api/alerts/send")
def alert_send(_=Depends(owner_only)):
    return guard(alerts.send_daily_alert)


# -------------------------------------------------------------------- assistant
class AskIn(BaseModel):
    question: str = Field(max_length=300)


@app.post("/api/assistant")
def ask(body: AskIn, user=Depends(current_user)):
    return assistant.ask(body.question, user["role"])


@app.post("/api/assistant/bill-photo")
async def bill_photo(file: UploadFile = File(...), _=Depends(owner_only)):
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Photo is larger than 8 MB")
    return guard(assistant.read_bill_photo, data, file.content_type or "")


# ---------------------------------------------------------------------- exports
def _xlsx(name, headers, rows):
    return Response(documents.xlsx_bytes(name, headers, rows), media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{name}-{date.today()}.xlsx"'})


@app.get("/api/export/inventory.xlsx")
def export_inventory(_=Depends(current_user)):
    rows = [[m["name"], m["generic_name"], m["category"], m["barcode"], m["batch"], m["quantity"], m["cost_price"],
             m["price"], m["gst_rate"], m["expiry"], m["status"]] for m in services.list_medicines()]
    return _xlsx("inventory", ["Medicine", "Generic", "Category", "Barcode", "Batch", "Qty", "Cost (ex-GST)",
                               "MRP", "GST %", "Expiry", "Status"], rows)


@app.get("/api/export/sales.xlsx")
def export_sales(_=Depends(owner_only)):
    rows = [[b["bill_no"], b["created_at"], b["customer_name"], b["payment_mode"], b["taxable"], b["cgst"],
             b["sgst"], b["total"]] for b in db.q("SELECT * FROM bills ORDER BY id")]
    return _xlsx("sales", ["Bill", "Date", "Customer", "Payment", "Taxable", "CGST", "SGST", "Total"], rows)


@app.get("/api/export/schedule-h.xlsx")
def export_schh(_=Depends(current_user)):
    return _xlsx("schedule-h-register", SCHH_HEAD, _schh_rows())


@app.get("/api/export/returns.xlsx")
def export_returns(days: int = Query(default=90, ge=1, le=365), _=Depends(owner_only)):
    return _xlsx("return-to-supplier", RETURN_HEAD, _returns_rows(services.return_list(days)))


@app.get("/api/export/schedule-h.pdf")
def export_schh_pdf(_=Depends(current_user)):
    pdf = documents.table_pdf("Schedule H register", SCHH_HEAD, _schh_rows(), "Printed " + date.today().isoformat())
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="schedule-h-register.pdf"'})


@app.get("/api/export/inventory.pdf")
def export_inventory_pdf(_=Depends(current_user)):
    rows = [[m["name"], m["batch"], m["quantity"], m["price"], m["gst_rate"], m["expiry"], m["status"]]
            for m in services.list_medicines()]
    pdf = documents.table_pdf("Stock list", ["Medicine", "Batch", "Qty", "MRP", "GST %", "Expiry", "Status"], rows,
                              "Printed " + date.today().isoformat())
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="stock-list.pdf"'})


# ----------------------------------------------------------------------- static
# Mounted last so /api/* routes win.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
