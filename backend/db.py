"""Database layer: connection helpers, schema, migration of the old v1 table, seed data."""
import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("MEDSTOCK_DB", BASE_DIR / "medstock.db"))

EXPIRY_WARNING_DAYS = 30   # flag medicines expiring within this many days
LOW_STOCK_LIMIT = 10       # flag batches with this many units or fewer
RETURN_WINDOW_DAYS = 90    # "return to supplier" list looks this far ahead


# ---------------------------------------------------------------- connections
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    """One transaction: commits on success, rolls back on any error."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q(sql, params=()):
    """SELECT -> list of dicts."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    """INSERT/UPDATE/DELETE -> cursor (lastrowid, rowcount)."""
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur
    finally:
        conn.close()


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


# --------------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS medicines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    batch TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    expiry TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL DEFAULT '',
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner','staff')),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, phone TEXT DEFAULT '', email TEXT DEFAULT '',
    gstin TEXT DEFAULT '', address TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    invoice_no TEXT DEFAULT '', purchase_date TEXT NOT NULL,
    total REAL NOT NULL DEFAULT 0, notes TEXT DEFAULT '', created_by INTEGER
);
CREATE TABLE IF NOT EXISTS purchase_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    medicine_id INTEGER, name TEXT NOT NULL, batch TEXT NOT NULL,
    qty INTEGER NOT NULL, cost_price REAL NOT NULL, gst_rate REAL NOT NULL DEFAULT 12
);
CREATE TABLE IF NOT EXISTS supplier_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    amount REAL NOT NULL, paid_on TEXT NOT NULL, note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    customer_name TEXT DEFAULT '', customer_phone TEXT DEFAULT '', doctor_name TEXT DEFAULT '',
    payment_mode TEXT DEFAULT 'Cash',
    discount_pct REAL NOT NULL DEFAULT 0,
    taxable REAL NOT NULL, cgst REAL NOT NULL, sgst REAL NOT NULL,
    round_off REAL NOT NULL DEFAULT 0, total REAL NOT NULL,
    created_by INTEGER
);
CREATE TABLE IF NOT EXISTS bill_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    medicine_id INTEGER, name TEXT NOT NULL, batch TEXT NOT NULL, expiry TEXT NOT NULL,
    qty INTEGER NOT NULL, price REAL NOT NULL, cost_price REAL NOT NULL DEFAULT 0,
    gst_rate REAL NOT NULL DEFAULT 0,
    taxable REAL NOT NULL, gst_amount REAL NOT NULL, total REAL NOT NULL,
    schedule_h INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS schedule_h_register (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER, sold_on TEXT NOT NULL,
    patient_name TEXT NOT NULL, patient_address TEXT DEFAULT '',
    doctor_name TEXT NOT NULL, doctor_reg_no TEXT DEFAULT '',
    medicine TEXT NOT NULL, batch TEXT NOT NULL, qty INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS refills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL, phone TEXT DEFAULT '',
    medicine TEXT NOT NULL, every_days INTEGER NOT NULL DEFAULT 30,
    last_date TEXT NOT NULL, notes TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_bills_created ON bills(created_at);
CREATE INDEX IF NOT EXISTS idx_bill_items_bill ON bill_items(bill_id);
"""

# columns added to the old v1 `medicines` table (name, SQL type + default)
MED_NEW_COLUMNS = [
    ("barcode", "TEXT NOT NULL DEFAULT ''"),
    ("generic_name", "TEXT NOT NULL DEFAULT ''"),
    ("category", "TEXT NOT NULL DEFAULT 'General'"),
    ("hsn", "TEXT NOT NULL DEFAULT '3004'"),
    ("gst_rate", "REAL NOT NULL DEFAULT 12"),     # % GST for this medicine (separate column)
    ("cost_price", "REAL NOT NULL DEFAULT 0"),    # purchase price per unit, excluding GST
    ("supplier_id", "INTEGER"),
    ("schedule_h", "INTEGER NOT NULL DEFAULT 0"),
]

DEFAULT_SETTINGS = {
    "shop_name": "MedStock Pharmacy",
    "shop_address": "Your shop address, City - 000000",
    "shop_phone": "",
    "shop_gstin": "",
    "shop_dl_no": "",
    "invoice_footer": "Thank you! Get well soon. Goods once sold will not be taken back.",
    "alert_last_sent": "",
}


def get_setting(key, default=""):
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def init_db():
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(medicines)")}
        for col, ddl in MED_NEW_COLUMNS:          # upgrade an old v1 database in place
            if col not in existing:
                conn.execute(f"ALTER TABLE medicines ADD COLUMN {col} {ddl}")
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('secret_key',?)",
            (secrets.token_hex(32),),
        )
        if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            conn.execute(
                "INSERT INTO users(username,full_name,pw_hash,role) VALUES(?,?,?,?)",
                ("admin", "Shop Owner", hash_password("admin123"), "owner"),
            )
            conn.execute(
                "INSERT INTO users(username,full_name,pw_hash,role) VALUES(?,?,?,?)",
                ("staff", "Counter Staff", hash_password("staff123"), "staff"),
            )
        conn.commit()
    finally:
        conn.close()
    seed_medicines()


# Demo catalogue: (name, generic, category, barcode, gst, price/MRP, cost, schedule_h, batch, qty, days to expiry)
DEMO = [
    ("Paracetamol 500mg", "paracetamol", "Fever & Pain", "8901001000011", 12, 2.5, 1.6, 0, "PCM2301", 120, 400),
    ("Crocin 500", "paracetamol", "Fever & Pain", "8901001000028", 12, 3.0, 2.0, 0, "CRC2402", 50, 300),
    ("Amoxicillin 250mg", "amoxicillin", "Antibiotic", "8901001000035", 12, 8.0, 5.5, 1, "AMX2209", 40, 15),
    ("Azithromycin 500mg", "azithromycin", "Antibiotic", "8901001000042", 12, 22.0, 15.0, 1, "AZT2105", 25, -20),
    ("Cetirizine 10mg", "cetirizine", "Allergy", "8901001000059", 12, 3.0, 1.8, 0, "CTZ2108", 8, 90),
    ("ORS Sachet", "ors", "Nutrition", "8901001000066", 5, 12.0, 8.5, 0, "ORS2302", 60, 200),
    ("Metformin 500mg", "metformin", "Diabetes", "8901001000073", 12, 2.0, 1.2, 1, "MET2401", 200, 365),
    ("Amlodipine 5mg", "amlodipine", "Blood Pressure", "8901001000080", 12, 2.8, 1.7, 1, "AML2403", 150, 500),
]


def seed_medicines():
    today = date.today()
    # 1) fresh install -> demo catalogue
    if not q1("SELECT id FROM medicines LIMIT 1"):
        for n, g, c, bc, gst, price, cost, sh, batch, qty, days in DEMO:
            execute(
                "INSERT INTO medicines (name,batch,quantity,price,expiry,barcode,generic_name,category,"
                "gst_rate,cost_price,schedule_h) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (n, batch, qty, price, (today + timedelta(days=days)).isoformat(), bc, g, c, gst, cost, sh),
            )
        return
    # 2) upgraded v1 database: fill the new columns for the 5 original demo medicines only
    for n, g, c, bc, gst, price, cost, sh, batch, qty, days in DEMO:
        execute(
            "UPDATE medicines SET generic_name=?, category=?, barcode=?, gst_rate=?, cost_price=?, schedule_h=? "
            "WHERE lower(name)=lower(?) AND batch=? AND barcode='' AND cost_price=0",
            (g, c, bc, gst, cost, sh, n, batch),
        )
