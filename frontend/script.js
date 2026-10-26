"use strict";
const $ = (id) => document.getElementById(id);
const rupees = (n) => "₹" + Number(n || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

let token = null, user = null, medicines = [], cart = [], charts = {};
try { token = localStorage.getItem("ms_token"); } catch (e) { /* storage blocked: fine, user logs in each time */ }

/* ------------------------------------------------------------ tiny helpers */
// h("tag", {attrs}, ...children) – builds DOM with textContent only (no innerHTML = no XSS from medicine names)
function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked") e[k] = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
const clear = (el) => { el.replaceChildren(); return el; };
const cell = (...c) => h("td", {}, ...c);
const badge = (cls, text) => h("span", { class: `badge ${cls}` }, text);
const isOwner = () => user && user.role === "owner";

let toastTimer;
function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 5000);
}

function errText(data) {
  if (data && typeof data.detail === "string") return data.detail;
  if (data && Array.isArray(data.detail) && data.detail[0]) {
    const d = data.detail[0];
    return `${(d.loc || []).slice(-1)[0] || "field"}: ${d.msg}`;
  }
  return "Something went wrong";
}

async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (token) headers.Authorization = "Bearer " + token;
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetch("/api" + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (res.status === 401 && path !== "/login") { logout(); throw new Error("Session expired – please log in again"); }
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(errText(data));
  return data;
}

// download / open a protected file (PDF, Excel) – sends the login token in a header
async function openFile(path, filename) {
  try {
    const res = await fetch("/api" + path, { headers: { Authorization: "Bearer " + token } });
    if (!res.ok) throw new Error(errText(await res.json().catch(() => null)));
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    if (blob.type === "application/pdf") window.open(url, "_blank");
    else { const a = h("a", { href: url, download: filename }); document.body.append(a); a.click(); a.remove(); }
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) { toast(e.message, true); }
}

const run = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };
const table = (tbody, rows, empty = "Nothing to show") => {
  clear(tbody);
  if (!rows.length) tbody.append(h("tr", {}, h("td", { colspan: 12, class: "muted" }, empty)));
  rows.forEach((r) => tbody.append(r));
};

/* --------------------------------------------------------------- auth/tabs */
function logout() {
  token = null; user = null;
  try { localStorage.removeItem("ms_token"); } catch (e) { /* ignore */ }
  $("app").classList.add("hidden");
  $("loginScreen").classList.remove("hidden");
}

$("loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("loginError").textContent = "";
  try {
    const r = await api("/login", { method: "POST", body: { username: $("loginUser").value, password: $("loginPass").value } });
    token = r.token;
    try { localStorage.setItem("ms_token", token); } catch (e) { /* ignore */ }
    $("loginPass").value = "";
    await startApp(r.default_password);
  } catch (e) { $("loginError").textContent = e.message; }
});
$("logoutBtn").addEventListener("click", logout);

async function startApp(defaultPw = false) {
  user = await api("/me");
  $("loginScreen").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("whoami").textContent = `${user.full_name || user.username} (${user.role})`;
  document.querySelectorAll("[data-role='owner']").forEach((el) => el.classList.toggle("hidden", !isOwner()));
  document.querySelectorAll(".owner-col").forEach((el) => el.classList.toggle("hidden", !isOwner()));
  $("pwWarning").classList.toggle("hidden", !defaultPw);
  const s = await api("/settings");
  $("shopTitle").textContent = s.shop_name || "MedStock Pharmacy";
  document.title = (s.shop_name || "MedStock") + " – MedStock";
  show("dashboard");
}

const loaders = {
  dashboard: loadDashboard, billing: loadBilling, inventory: loadInventory, purchases: loadPurchases,
  reports: loadReports, returns: loadReturns, refills: loadRefills, schedh: loadSchedH,
  assistant: loadAssistant, settings: loadSettings,
};
function show(view) {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $("view-" + view).classList.remove("hidden");
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  run(loaders[view])();
}
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

/* --------------------------------------------------------------- dashboard */
function makeCard(label, value, color = "") {
  return h("div", { class: `card ${color}` }, h("div", { class: "label" }, label), h("div", { class: "value" }, value));
}

function drawChart(id, config) {
  if (typeof Chart === "undefined") return;          // Chart.js CDN not reachable (offline)
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), config);
}
const PALETTE = ["#0d9488", "#16a34a", "#d97706", "#2563eb", "#9333ea", "#dc2626", "#64748b", "#0891b2"];

async function loadDashboard() {
  const [sum, dash, reorder, alert] = await Promise.all([
    api("/summary"), api("/dashboard"), api("/reorder"), api("/alerts/preview"),
  ]);
  const cards = clear($("cards"));
  cards.append(
    makeCard("Today's sales", rupees(dash.today.sales), "green"),
    makeCard("This month", rupees(dash.month.sales), "green"),
  );
  if (isOwner()) cards.append(makeCard("Profit this month", rupees(dash.month.profit), "green"));
  cards.append(
    makeCard("Expired batches", sum.expired, "red"),
    makeCard("Expiring in 30 days", sum.expiring, "amber"),
    makeCard("Low stock", sum.low_stock, "amber"),
    makeCard("Loss (expired stock)", rupees(sum.loss_value), "red"),
    makeCard("Valid stock value", rupees(sum.stock_value)),
  );
  const cw = $("costWarn");
  cw.classList.toggle("hidden", !(isOwner() && dash.missing_cost_prices));
  cw.textContent = `${dash.missing_cost_prices} stocked batch(es) have no cost price, so profit looks too high. Edit them in Inventory.`;

  drawChart("chSales", {
    type: "line",
    data: { labels: dash.sales_trend.map((d) => d.date.slice(5)),
      datasets: [{ label: "Sales (₹)", data: dash.sales_trend.map((d) => d.sales), borderColor: "#0d9488", backgroundColor: "rgba(13,148,136,.15)", fill: true, tension: 0.3 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } } },
  });
  drawChart("chExpiry", {
    type: "bar",
    data: { labels: dash.expiry_timeline.map((d) => d.label),
      datasets: [{ label: "Stock value (₹)", data: dash.expiry_timeline.map((d) => d.value),
        backgroundColor: dash.expiry_timeline.map((d) => (d.label === "Expired" ? "#dc2626" : d.label === "+1m" ? "#d97706" : "#0d9488")) }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } } },
  });
  drawChart("chCat", {
    type: "doughnut",
    data: { labels: dash.category_split.map((d) => d.label),
      datasets: [{ data: dash.category_split.map((d) => d.value), backgroundColor: PALETTE }] },
    options: { maintainAspectRatio: false },
  });

  const top = clear($("topList"));
  if (!dash.top_selling.length) top.append(h("p", { class: "muted" }, "No sales yet – make a bill to see this."));
  dash.top_selling.forEach((t, i) => top.append(h("div", { class: "line" }, `${i + 1}. ${t.name}`, h("b", {}, `${t.units} units`))));

  const ro = clear($("reorderList"));
  if (!reorder.length) ro.append(h("p", { class: "muted" }, "Nothing needs reordering right now 👍"));
  reorder.forEach((r) => ro.append(h("div", { class: "line warnline" }, r.message)));

  $("alertText").textContent = alert.text;
  $("waLink").href = alert.whatsapp_link;
  $("alertNote").textContent = alert.email_configured
    ? "Daily e-mail is switched on (sent once a day at the hour set in .env)."
    : "E-mail is not set up yet – see README (SMTP settings in backend/.env).";
}
$("sendAlertBtn").addEventListener("click", run(async () => {
  await api("/alerts/send", { method: "POST" });
  toast("Alert e-mail sent ✔");
}));

/* ----------------------------------------------------------------- billing */
async function loadBilling() {
  $("scanInput").focus();
  renderCart();
  const bills = await api("/bills?limit=15");
  table($("billRows"), bills.map((b) => h("tr", {},
    cell(b.bill_no), cell(b.created_at.replace("T", " ")), cell(b.customer_name || "Walk-in"),
    cell(b.payment_mode), cell(rupees(b.total)),
    cell(h("button", { class: "ghost", onclick: () => openFile(`/bills/${b.id}/pdf`) }, "PDF")))), "No bills yet");
}

async function doScan() {
  const code = $("scanInput").value.trim();
  if (!code) return;
  const res = await api("/lookup?code=" + encodeURIComponent(code));
  const box = clear($("scanResults"));
  $("scanInput").value = "";
  if (!res.length) { box.append(h("p", { class: "muted" }, `Nothing found for "${code}"`)); return; }
  if (res.length === 1 && res[0].stock > 0) { addToCart(res[0]); return; }   // exact scan: straight into the bill
  res.forEach((p) => box.append(productRow(p)));
}

function productRow(p) {
  const row = h("div", { class: "line" },
    h("span", {}, h("b", {}, p.name), ` · ${rupees(p.price)} · GST ${p.gst_rate}% · `,
      p.stock > 0 ? `${p.stock} in stock` : badge("expired", "Out of stock"),
      p.schedule_h ? badge("expiring", "Sch H") : null),
    p.stock > 0
      ? h("button", { onclick: () => addToCart(p) }, "Add")
      : h("button", { class: "ghost", onclick: run(() => showSubs(p)) }, "Substitutes"));
  return row;
}

async function showSubs(p) {
  const subs = await api("/substitutes/" + p.medicine_id);
  const box = clear($("scanResults"));
  box.append(h("p", {}, subs.length ? `Same salt as ${p.name}, in stock:` : `No in-stock substitute for ${p.name}.`));
  subs.forEach((s) => box.append(productRow(s)));
}

function addToCart(p) {
  const existing = cart.find((c) => c.name === p.name);
  if (existing) {
    if (existing.qty + 1 > p.stock) { toast(`Only ${p.stock} of ${p.name} in stock`, true); return; }
    existing.qty += 1;
  } else cart.push({ medicine_id: p.medicine_id, name: p.name, price: p.price, gst_rate: p.gst_rate, stock: p.stock, qty: 1, schedule_h: p.schedule_h });
  clear($("scanResults"));
  renderCart();
  $("scanInput").focus();
}

function renderCart() {
  table($("cartRows"), cart.map((c, i) => {
    const qty = h("input", { type: "number", min: 1, max: c.stock, value: c.qty, class: "qty" });
    qty.addEventListener("change", () => { c.qty = Math.max(1, Math.min(c.stock, parseInt(qty.value) || 1)); renderCart(); });
    return h("tr", {}, cell(c.name, c.schedule_h ? badge("expiring", "Sch H") : null), cell(rupees(c.price)), cell(c.gst_rate + "%"),
      cell(qty), cell(rupees(c.price * c.qty * (1 - discountPct() / 100))),
      cell(h("button", { class: "del", onclick: () => { cart.splice(i, 1); renderCart(); } }, "✕")));
  }), "Scan or search a medicine to start a bill");
  const d = 1 - discountPct() / 100;
  let taxable = 0, gst = 0, total = 0;
  cart.forEach((c) => {
    const line = Math.round(c.price * c.qty * d * 100) / 100;
    const t = Math.round((line / (1 + c.gst_rate / 100)) * 100) / 100;
    taxable += t; gst += line - t; total += line;
  });
  $("tTaxable").textContent = rupees(taxable);
  $("tCgst").textContent = rupees(gst / 2);
  $("tSgst").textContent = rupees(gst / 2);
  $("tTotal").textContent = "₹" + Math.round(total).toLocaleString("en-IN");
  $("schHFields").classList.toggle("hidden", !cart.some((c) => c.schedule_h));
}
const discountPct = () => Math.min(100, Math.max(0, parseFloat($("discount").value) || 0));
$("discount").addEventListener("input", renderCart);
$("scanInput").addEventListener("keydown", (e) => { if (e.key === "Enter") run(doScan)(); });
$("scanBtn").addEventListener("click", run(doScan));
$("clearCart").addEventListener("click", () => { cart = []; renderCart(); });

$("makeBill").addEventListener("click", run(async () => {
  if (!cart.length) { toast("Add at least one medicine", true); return; }
  const bill = await api("/bills", { method: "POST", body: {
    customer_name: $("custName").value, customer_phone: $("custPhone").value, doctor_name: $("doctorName").value,
    doctor_reg_no: $("doctorReg").value, patient_address: $("patientAddr").value,
    payment_mode: $("payMode").value, discount_pct: discountPct(),
    items: cart.map((c) => ({ medicine_id: c.medicine_id, qty: c.qty })),
  } });
  toast(`Bill ${bill.bill_no} saved – total ${rupees(bill.total)}`);
  cart = [];
  ["custName", "custPhone", "doctorName", "doctorReg", "patientAddr", "discount"].forEach((id) => ($(id).value = ""));
  renderCart();
  openFile(`/bills/${bill.id}/pdf`);
  loadBilling();
}));

// Optional camera barcode scanning (Chrome / Android supports BarcodeDetector)
if ("BarcodeDetector" in window) {
  $("camBtn").classList.remove("hidden");
  let stream = null, timer = null;
  const stopCam = () => { clearInterval(timer); if (stream) stream.getTracks().forEach((t) => t.stop()); stream = null; $("camVideo").classList.add("hidden"); };
  $("camBtn").addEventListener("click", async () => {
    if (stream) { stopCam(); return; }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
      const v = $("camVideo"); v.srcObject = stream; v.classList.remove("hidden"); await v.play();
      const det = new BarcodeDetector();
      timer = setInterval(async () => {
        const codes = await det.detect(v).catch(() => []);
        if (codes.length) { $("scanInput").value = codes[0].rawValue; stopCam(); run(doScan)(); }
      }, 400);
    } catch (e) { toast("Camera not available: " + e.message, true); stopCam(); }
  });
}

/* --------------------------------------------------------------- inventory */
let editingId = null;
async function loadInventory() {
  medicines = await api("/medicines");
  renderRows();
}
function renderRows() {
  const filter = $("filter").value, term = $("invSearch").value.trim().toLowerCase();
  table($("rows"), medicines
    .filter((m) => {
      if (term && !(m.name + " " + m.batch + " " + m.barcode + " " + m.generic_name).toLowerCase().includes(term)) return false;
      if (filter === "all") return true;
      if (filter === "low") return m.low_stock && m.status !== "expired";
      return m.status === filter;
    })
    .map((m) => {
      const label = m.status === "expired" ? `Expired ${-m.days_left}d ago` : m.status === "expiring" ? `${m.days_left}d left` : "OK";
      const ownerCells = isOwner()
        ? cell(h("button", { class: "ghost", onclick: () => startEdit(m) }, "Edit"), " ",
               h("button", { class: "del", onclick: run(async () => { if (confirm(`Delete ${m.name} (${m.batch})?`)) { await api("/medicines/" + m.id, { method: "DELETE" }); loadInventory(); } }) }, "✕"))
        : cell("");
      return h("tr", {}, cell(m.name, m.schedule_h ? badge("expiring", "Sch H") : null), cell(m.batch),
        cell(m.quantity, m.low_stock && m.status !== "expired" ? h("span", { class: "low" }, "LOW") : null),
        cell(rupees(m.price)), cell(m.gst_rate + "%"),
        isOwner() ? cell(m.cost_price ? rupees(m.cost_price) : h("span", { class: "low" }, "not set")) : null,
        cell(m.expiry), cell(badge(m.status, label)), ownerCells);
    }));
}
$("filter").addEventListener("change", renderRows);
$("invSearch").addEventListener("input", renderRows);

const medFields = { name: "name", batch: "batch", quantity: "quantity", price: "price", costPrice: "cost_price", gstRate: "gst_rate",
  expiry: "expiry", barcode: "barcode", generic: "generic_name", category: "category" };
function startEdit(m) {
  editingId = m.id;
  for (const [id, key] of Object.entries(medFields)) $(id).value = m[key];
  $("schedH").checked = !!m.schedule_h;
  $("medFormTitle").textContent = `Edit: ${m.name} (${m.batch})`;
  $("addBtn").textContent = "Save changes";
  $("cancelEdit").classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function resetMedForm() {
  editingId = null;
  Object.keys(medFields).forEach((id) => ($(id).value = ""));
  $("gstRate").value = "12";
  $("schedH").checked = false;
  $("medFormTitle").textContent = "Add stock";
  $("addBtn").textContent = "Add";
  $("cancelEdit").classList.add("hidden");
}
$("cancelEdit").addEventListener("click", resetMedForm);
$("addBtn").addEventListener("click", run(async () => {
  const payload = {
    name: $("name").value.trim(), batch: $("batch").value.trim(), quantity: Number($("quantity").value),
    price: Number($("price").value), cost_price: Number($("costPrice").value || 0), gst_rate: Number($("gstRate").value),
    expiry: $("expiry").value, barcode: $("barcode").value.trim(), generic_name: $("generic").value.trim(),
    category: $("category").value.trim() || "General", schedule_h: $("schedH").checked,
  };
  await api(editingId ? "/medicines/" + editingId : "/medicines", { method: editingId ? "PUT" : "POST", body: payload });
  toast(editingId ? "Saved" : "Stock added");
  resetMedForm();
  loadInventory();
}));
// a USB scanner "types" the code + Enter: don't let Enter submit anything else, just move on
$("barcode").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("generic").focus(); } });
$("expInvXlsx").addEventListener("click", () => openFile("/export/inventory.xlsx", "inventory.xlsx"));
$("expInvPdf").addEventListener("click", () => openFile("/export/inventory.pdf"));

/* ---------------------------------------------------- suppliers & purchases */
let suppliers = [];
async function loadPurchases() {
  suppliers = await api("/suppliers");
  const sel = clear($("purSupplier"));
  sel.append(h("option", { value: "" }, "Choose supplier…"), ...suppliers.map((s) => h("option", { value: s.id }, s.name)));
  const list = clear($("supList"));
  if (!suppliers.length) list.append(h("p", { class: "muted" }, "No suppliers yet"));
  suppliers.forEach((s) => list.append(h("div", { class: "line" },
    h("span", {}, h("b", {}, s.name), s.phone ? ` · ${s.phone}` : ""),
    h("span", {}, s.owed > 0 ? badge("expiring", `Owe ${rupees(s.owed)}`) : badge("ok", "Settled"), " ",
      s.owed > 0 ? h("button", { class: "ghost", onclick: run(async () => {
        const a = parseFloat(prompt(`Payment to ${s.name} (₹)?`, s.owed));
        if (a > 0) { await api(`/suppliers/${s.id}/payments`, { method: "POST", body: { amount: a } }); loadPurchases(); }
      }) }, "Pay") : null))));
  if (!$("purRows").children.length) addPurRow();
  const hist = await api("/purchases");
  table($("purHistory"), hist.map((p) => h("tr", {}, cell(p.purchase_date), cell(p.supplier_name), cell(p.invoice_no), cell(rupees(p.total)))), "No purchases yet");
}
$("addSupplier").addEventListener("click", run(async () => {
  await api("/suppliers", { method: "POST", body: { name: $("supName").value, phone: $("supPhone").value, gstin: $("supGstin").value } });
  ["supName", "supPhone", "supGstin"].forEach((id) => ($(id).value = ""));
  loadPurchases();
}));

function addPurRow(v = {}) {
  const inp = (cls, type, val, ph, extra = {}) => h("input", { class: cls, type, value: val ?? "", placeholder: ph, ...extra });
  const tr = h("tr", {},
    cell(inp("p-name", "text", v.name, "Medicine")), cell(inp("p-batch", "text", v.batch, "Batch")),
    cell(inp("p-exp", "date", v.expiry, "")), cell(inp("p-qty", "number", v.qty, "Qty", { min: 1 })),
    cell(inp("p-cost", "number", v.cost_price, "₹", { min: 0, step: "0.01" })), cell(inp("p-mrp", "number", v.mrp, "₹ (optional)", { min: 0, step: "0.01" })),
    cell(inp("p-gst", "number", v.gst_rate, "auto", { min: 0, max: 28 })),
    cell(h("button", { class: "del", onclick: () => tr.remove() }, "✕")));
  $("purRows").append(tr);
}
$("addPurRow").addEventListener("click", () => addPurRow());
$("savePurchase").addEventListener("click", run(async () => {
  const items = [...$("purRows").children].map((tr) => {
    const g = (c) => tr.querySelector("." + c).value;
    const num = (c) => (g(c) === "" ? null : Number(g(c)));
    return { name: g("p-name").trim(), batch: g("p-batch").trim(), expiry: g("p-exp"), qty: Number(g("p-qty")),
      cost_price: Number(g("p-cost")), price: num("p-mrp"), gst_rate: num("p-gst") };
  }).filter((i) => i.name);
  if (!$("purSupplier").value) throw new Error("Choose a supplier first");
  const r = await api("/purchases", { method: "POST", body: { supplier_id: Number($("purSupplier").value),
    invoice_no: $("purInvoice").value, paid_now: Number($("purPaid").value || 0), items } });
  toast(`Purchase saved – ${rupees(r.total)} added to what you owe`);
  clear($("purRows")); $("purInvoice").value = ""; $("purPaid").value = ""; $("photoNote").textContent = "";
  loadPurchases();
}));

$("billPhoto").addEventListener("change", run(async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  $("photoNote").textContent = "Reading the bill… this takes ~10–20 seconds.";
  const fd = new FormData(); fd.append("file", f);
  const res = await fetch("/api/assistant/bill-photo", { method: "POST", headers: { Authorization: "Bearer " + token }, body: fd });
  const data = await res.json().catch(() => null);
  ev.target.value = "";
  if (!res.ok) { $("photoNote").textContent = ""; throw new Error(errText(data)); }
  clear($("purRows"));
  (data.items || []).forEach((i) => addPurRow({ name: i.name, batch: i.batch, expiry: i.expiry, qty: i.qty, cost_price: i.cost_price, mrp: i.mrp, gst_rate: i.gst_rate }));
  if (data.invoice_no) $("purInvoice").value = data.invoice_no;
  $("photoNote").textContent = data.note;
}));

/* ----------------------------------------------------------------- reports */
async function loadReports() {
  const [top, gst] = await Promise.all([api("/reports/top"), api("/reports/gst")]);
  table($("topRows"), top.map((t) => h("tr", {}, cell(t.name), cell(t.units), cell(rupees(t.revenue)), cell(rupees(t.profit)))), "No sales yet");
  table($("gstRows"), gst.map((g) => h("tr", {}, cell(g.gst_rate + "%"), cell(rupees(g.taxable)), cell(rupees(g.cgst)), cell(rupees(g.sgst)))), "No sales this month");
  await loadSalesTable();
}
async function loadSalesTable() {
  const period = $("repPeriod").value;
  const rows = await api("/reports/sales?period=" + period);
  table($("repRows"), rows.slice().reverse().map((r) => h("tr", {},
    cell(r.date || r.month), cell(r.bills), cell(rupees(r.sales)), cell(rupees(r.profit)))));
}
$("repPeriod").addEventListener("change", run(loadSalesTable));
$("expSales").addEventListener("click", () => openFile("/export/sales.xlsx", "sales.xlsx"));

/* ----------------------------------------------------------------- returns */
async function loadReturns() {
  const rows = await api("/returns?days=" + $("retDays").value);
  table($("retRows"), rows.map((r) => h("tr", {}, cell(r.supplier_name), cell(r.name), cell(r.batch), cell(r.expiry), cell(r.quantity),
    cell(rupees(r.value)), cell(badge(r.status, r.days_left < 0 ? "Expired" : `${r.days_left}d left`)))), "No stock near expiry 🎉");
}
$("retDays").addEventListener("change", run(loadReturns));
$("retPdf").addEventListener("click", () => openFile("/returns/pdf?days=" + $("retDays").value));
$("retXlsx").addEventListener("click", () => openFile("/export/returns.xlsx?days=" + $("retDays").value, "return-to-supplier.xlsx"));

/* ----------------------------------------------------------------- refills */
function waLink(phone, text) {
  let d = (phone || "").replace(/\D/g, "");
  if (d.length === 10) d = "91" + d;
  return `https://wa.me/${d}?text=${encodeURIComponent(text)}`;
}
async function loadRefills() {
  const rows = await api("/refills");
  table($("rfRows"), rows.map((r) => {
    const due = r.days_to_due;
    const msg = `Hello ${r.customer_name}, this is a reminder from ${$("shopTitle").textContent}: your ${r.medicine} refill is due. Please visit us or reply to order.`;
    return h("tr", {}, cell(r.customer_name), cell(r.medicine), cell(r.every_days + " days"), cell(r.next_due),
      cell(badge(due < 0 ? "expired" : due <= 3 ? "expiring" : "ok", due < 0 ? `Overdue ${-due}d` : due === 0 ? "Due today" : `in ${due}d`)),
      cell(r.phone ? h("a", { class: "btn", target: "_blank", rel: "noopener", href: waLink(r.phone, msg) }, "WhatsApp") : null, " ",
        h("button", { class: "ghost", onclick: run(async () => { await api(`/refills/${r.id}/refilled`, { method: "PUT" }); loadRefills(); }) }, "Refilled"), " ",
        h("button", { class: "del", onclick: run(async () => { await api("/refills/" + r.id, { method: "DELETE" }); loadRefills(); }) }, "✕")));
  }), "No reminders yet – add customers on monthly medicines (BP, diabetes…)");
}
$("addRefill").addEventListener("click", run(async () => {
  await api("/refills", { method: "POST", body: { customer_name: $("rfName").value, phone: $("rfPhone").value, medicine: $("rfMed").value,
    every_days: Number($("rfDays").value || 30), last_date: $("rfLast").value || null } });
  ["rfName", "rfPhone", "rfMed", "rfLast"].forEach((id) => ($(id).value = ""));
  loadRefills();
}));

/* -------------------------------------------------------------- schedule H */
async function loadSchedH() {
  const rows = await api("/schedule-h");
  table($("schRows"), rows.map((r) => h("tr", {}, cell(r.sold_on), cell(r.patient_name), cell(r.doctor_name), cell(r.doctor_reg_no),
    cell(r.medicine), cell(r.batch), cell(r.qty))), "No Schedule H sales recorded yet");
}
$("schXlsx").addEventListener("click", () => openFile("/export/schedule-h.xlsx", "schedule-h.xlsx"));
$("schPdf").addEventListener("click", () => openFile("/export/schedule-h.pdf"));

/* --------------------------------------------------------------- assistant */
const CHIPS = ["Which medicines expire next month?", "Low stock", "What should I reorder?", "Sales today", "Top selling medicines", "Refills due"];
function loadAssistant() {
  const box = clear($("askChips"));
  CHIPS.forEach((c) => box.append(h("button", { class: "chip", onclick: () => { $("askInput").value = c; run(ask)(); } }, c)));
  $("askInput").focus();
}
async function ask() {
  const q = $("askInput").value.trim();
  if (!q) return;
  const r = await api("/assistant", { method: "POST", body: { question: q } });
  $("askAnswer").textContent = r.answer;
  const head = clear($("askHead")), body = clear($("askRows"));
  if (r.rows.length) {
    const keys = Object.keys(r.rows[0]);
    head.append(h("tr", {}, keys.map((k) => h("th", {}, k))));
    r.rows.forEach((row) => body.append(h("tr", {}, keys.map((k) => cell(row[k])))));
  }
}
$("askBtn").addEventListener("click", run(ask));
$("askInput").addEventListener("keydown", (e) => { if (e.key === "Enter") run(ask)(); });

/* ---------------------------------------------------------------- settings */
async function loadSettings() {
  if (isOwner()) {
    const s = await api("/settings");
    $("setName").value = s.shop_name; $("setAddr").value = s.shop_address; $("setPhone").value = s.shop_phone;
    $("setGstin").value = s.shop_gstin; $("setDl").value = s.shop_dl_no; $("setFooter").value = s.invoice_footer;
    const users = await api("/users");
    const list = clear($("userList"));
    users.forEach((u) => list.append(h("div", { class: "line" }, h("span", {}, h("b", {}, u.username), ` · ${u.full_name} · ${u.role}`),
      u.id === user.id ? badge("ok", "you") : h("button", { class: "ghost", onclick: run(async () => {
        await api(`/users/${u.id}/active?active=${u.active ? "false" : "true"}`, { method: "PUT" }); loadSettings();
      }) }, u.active ? "Disable" : "Enable"))));
  }
}
$("saveSettings").addEventListener("click", run(async () => {
  await api("/settings", { method: "PUT", body: { shop_name: $("setName").value, shop_address: $("setAddr").value, shop_phone: $("setPhone").value,
    shop_gstin: $("setGstin").value, shop_dl_no: $("setDl").value, invoice_footer: $("setFooter").value } });
  $("shopTitle").textContent = $("setName").value;
  toast("Shop details saved");
}));
$("changePw").addEventListener("click", run(async () => {
  await api("/me/password", { method: "POST", body: { old_password: $("pwOld").value, new_password: $("pwNew").value } });
  $("pwOld").value = ""; $("pwNew").value = ""; $("pwWarning").classList.add("hidden");
  toast("Password changed");
}));
$("addUser").addEventListener("click", run(async () => {
  await api("/users", { method: "POST", body: { username: $("nuName").value, full_name: $("nuFull").value, password: $("nuPass").value, role: $("nuRole").value } });
  ["nuName", "nuFull", "nuPass"].forEach((id) => ($(id).value = ""));
  toast("User added"); loadSettings();
}));

/* -------------------------------------------------------------------- boot */
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => { /* PWA is optional */ });
(async () => {
  if (token) { try { await startApp(); return; } catch (e) { /* token expired */ } }
  $("loginScreen").classList.remove("hidden");
})();
