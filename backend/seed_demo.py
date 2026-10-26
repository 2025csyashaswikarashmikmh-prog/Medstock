"""OPTIONAL: fills the app with ~45 days of fake sales so charts and reports look alive for a demo.
   python seed_demo.py            -> add demo sales
Do NOT run this on a real shop's database (the fake bills would mix with real ones). Make a copy of
medstock.db first if unsure."""
import random
from datetime import datetime, timedelta

import db
import services

db.init_db()
random.seed(7)
user = db.q1("SELECT id, username, full_name, role FROM users WHERE role='owner' LIMIT 1")
products = [m for m in db.q("SELECT * FROM medicines WHERE quantity>200 AND expiry > date('now','+60 day')")]
made = 0
for back in range(45, 0, -1):
    for _ in range(random.randint(3, 9)):
        p = random.choice(products)
        try:
            bill = services.create_bill(user, {"items": [{"medicine_id": p["id"], "qty": random.randint(1, 6)}],
                                               "payment_mode": random.choice(["Cash", "UPI", "Cash"])})
        except services.BusinessError:
            continue
        when = (datetime.now() - timedelta(days=back, hours=random.randint(0, 8))).isoformat(timespec="seconds")
        db.execute("UPDATE bills SET created_at=? WHERE id=?", (when, bill["id"]))
        made += 1
print(f"Added {made} demo bills.")
