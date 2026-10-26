# MedStock Pharmacy – billing, stock & expiry system (v2)

FastAPI + SQLite + plain HTML/JS. Works on one shop PC; other devices on the same Wi-Fi can open it too.

## Run it
- **Windows:** double-click `run.bat`  •  **Mac/Linux:** `./run.sh`
- Open <http://localhost:8000>. Other devices: `http://<shop-pc-ip>:8000`.
- First logins: **admin / admin123** (owner) and **staff / staff123** (staff). **Change both in Settings first.**
- Your old `medstock.db` works: copy it into `backend/` and the new columns are added automatically.
- Optional demo data for presentations: `cd backend && python seed_demo.py` (adds fake sales – never on a real shop DB).

## What's inside
| Area | What it does |
|---|---|
| Login & roles | Owner: everything. Staff: billing, stock view, refills, Schedule H, assistant (no cost/profit, no purchases/reports/users). 12-hour sessions, lock-out after 5 wrong passwords. |
| GST billing | Multi-medicine bill, customer/doctor, discount %, payment mode, **GST % column per medicine**, CGST/SGST split, GST summary by rate, round-off, PDF invoice with shop name/GSTIN/drug licence. MRP is treated as GST-inclusive (Indian norm). |
| FEFO selling | The earliest-expiry batch is used automatically; one line can span batches; expired stock can't be sold; failed bills change nothing. |
| Barcode | USB scanner types the code + Enter in the Billing box. Phones/webcams can use the 📷 button (Chrome/Android). |
| Dashboard | Sales trend, expiry timeline, category doughnut (Chart.js, bundled – works offline), top sellers. |
| Reports (owner) | Daily/monthly sales and profit (profit = taxable value − cost × qty), top sellers, GST collected, Excel export. |
| Suppliers & purchases | Record stock received (creates/increases batches), cost price, what you owe each supplier, payments. |
| Reorder suggestions | From the last 30 days of sales: "Crocin will run out in 3 days – order ~40". |
| Return to supplier | Expired / near-expiry stock grouped by supplier; PDF + Excel. |
| Refill reminders | Monthly-medicine customers; one-click WhatsApp message. |
| Substitutes | Out of stock? Shows same-salt brands that are in stock (fill the "Generic / salt" field). |
| Schedule H register | Filled automatically when a Schedule H item is billed (patient + doctor required). |
| Alerts | Daily summary: WhatsApp share link (free) and e-mail (`backend/.env`, see `.env.example`). |
| AI assistant | Ask "which medicines expire next month?" (works offline). Upload a supplier-bill photo to pre-fill a purchase (needs `ANTHROPIC_API_KEY`; you review every line before saving). |
| Export / PWA | Excel + PDF exports; installable on phone/PC ("Install app" in the browser). |
| Backup | `python backend/backup.py` → `backup/medstock-DATE.db` (keeps 30). Schedule it daily. |

