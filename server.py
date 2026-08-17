from __future__ import annotations

import cgi
import html
import io
import json
import socketserver
from http.server import BaseHTTPRequestHandler

from reconciliation_engine import MatchConfig, STATUS_COLORS, reconcile, standardize_transactions, to_excel_bytes


PORT = 8501
LAST_REPORT = b""


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Intelligent Bank Reconciliation</title>
  <style>
    :root { --ink:#172033; --muted:#5b6475; --line:#d9dee8; --panel:#ffffff; --bg:#f5f7fb; --blue:#215db0; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; color:var(--ink); background:var(--bg); }
    header { background:#fff; border-bottom:1px solid var(--line); padding:22px 32px; }
    h1 { margin:0 0 4px; font-size:26px; letter-spacing:0; }
    .sub { color:var(--muted); font-size:14px; }
    main { padding:24px 32px; display:grid; gap:18px; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; }
    .grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:16px; }
    label { display:block; font-weight:600; margin-bottom:8px; }
    input[type=file], input[type=number] { width:100%; padding:10px; border:1px solid var(--line); border-radius:6px; background:#fff; }
    .rules { display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:12px; margin-top:16px; }
    button, .download { background:var(--blue); color:#fff; border:0; border-radius:6px; padding:11px 16px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-block; }
    button:disabled { opacity:.55; cursor:wait; }
    .metrics { display:grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap:12px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:14px; background:#fff; }
    .metric span { display:block; color:var(--muted); font-size:12px; text-transform:uppercase; }
    .metric b { font-size:24px; }
    table { width:100%; border-collapse:collapse; background:#fff; font-size:13px; }
    th, td { border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }
    th { position:sticky; top:0; background:#eef2f8; z-index:1; }
    .table-wrap { max-height:560px; overflow:auto; border:1px solid var(--line); border-radius:8px; }
    .legend { display:flex; flex-wrap:wrap; gap:8px; color:var(--muted); font-size:13px; }
    .swatch { width:14px; height:14px; border:1px solid #9ca3af; display:inline-block; vertical-align:-2px; margin-right:4px; }
    @media (max-width:800px) { .grid, .rules, .metrics { grid-template-columns:1fr; } main, header { padding-left:16px; padding-right:16px; } }
  </style>
</head>
<body>
<header>
  <h1>Intelligent Bank Reconciliation</h1>
  <div class="sub">Automated exact matching, fuzzy matching, duplicate detection, and missing-entry review for bank vs MSD/F&O data.</div>
</header>
<main>
  <section class="panel">
    <form id="reconForm">
      <div class="grid">
        <div><label>Bank statement workbook or PDF</label><input name="bank" type="file" accept=".xlsx,.pdf" required></div>
        <div><label>MSD/F&O ledger or reconciliation workbook/PDF</label><input name="ledger" type="file" accept=".xlsx,.pdf" required></div>
      </div>
      <div class="rules">
        <div><label>Date tolerance (+/- days)</label><input name="date_tolerance" type="number" min="0" max="10" value="3"></div>
        <div><label>Amount tolerance</label><input name="amount_tolerance" type="number" min="0" step="0.01" value="0.05"></div>
        <div><label>Fuzzy threshold</label><input name="fuzzy_threshold" type="number" min="50" max="100" value="72"></div>
      </div>
      <p><button id="runBtn" type="submit">Run reconciliation</button> <a id="download" class="download" style="display:none" href="/download">Download Excel report</a></p>
      <div class="legend">
        <span><i class="swatch" style="background:#b7f7c4"></i>Exact</span>
        <span><i class="swatch" style="background:#fff3a3"></i>Fuzzy</span>
        <span><i class="swatch" style="background:#ffd49a"></i>Duplicate</span>
        <span><i class="swatch" style="background:#dfc7ff"></i>Missing</span>
      </div>
    </form>
  </section>
  <section id="summary" class="metrics"></section>
  <section id="result" class="table-wrap" style="display:none"></section>
</main>
<script>
const form = document.getElementById('reconForm');
const runBtn = document.getElementById('runBtn');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  runBtn.disabled = true;
  runBtn.textContent = 'Reconciling...';
  document.getElementById('download').style.display = 'none';
  const response = await fetch('/reconcile', { method: 'POST', body: new FormData(form) });
  const data = await response.json();
  runBtn.disabled = false;
  runBtn.textContent = 'Run reconciliation';
  if (!response.ok) { alert(data.error || 'Reconciliation failed'); return; }
  renderSummary(data.summary);
  renderTable(data.rows);
  document.getElementById('download').style.display = 'inline-block';
});
function renderSummary(summary) {
  const items = [['Total','total'], ['Exact','Exact Match'], ['Fuzzy','Fuzzy Match'], ['Duplicates','Duplicate Entry'], ['Exceptions','exceptions']];
  document.getElementById('summary').innerHTML = items.map(([label,key]) => `<div class="metric"><span>${label}</span><b>${summary[key] || 0}</b></div>`).join('');
}
function esc(v) { return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function renderTable(rows) {
  const columns = ['status','score','note','bank_date','bank_amount','bank_reference','bank_description','ledger_date','ledger_amount','ledger_reference','ledger_description'];
  const colors = {""" + json.dumps(STATUS_COLORS)[1:-1] + """};
  const head = columns.map(c => `<th>${esc(c.replaceAll('_',' '))}</th>`).join('');
  const body = rows.map(row => `<tr style="background:${colors[row.status] || '#fff'}">${columns.map(c => `<td>${esc(row[c])}</td>`).join('')}</tr>`).join('');
  document.getElementById('result').style.display = 'block';
  document.getElementById('result').innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/download":
            self._send_download()
            return
        self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/reconcile":
            self._send_json({"error": "Not found"}, 404)
            return
        try:
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            bank_item = form["bank"]
            ledger_item = form["ledger"]
            config = MatchConfig(
                date_tolerance_days=int(form.getfirst("date_tolerance", "3")),
                amount_tolerance=float(form.getfirst("amount_tolerance", "0.05")),
                fuzzy_threshold=int(form.getfirst("fuzzy_threshold", "72")),
            )
            bank = standardize_transactions(bank_item.file.read(), "Bank", bank_item.filename)
            ledger = standardize_transactions(ledger_item.file.read(), "Ledger", ledger_item.filename)
            result, summary = reconcile(bank, ledger, config)
            global LAST_REPORT
            LAST_REPORT = to_excel_bytes(result, summary)
            rows = _json_rows(result)
            self._send_json({"summary": summary, "rows": rows})
        except Exception as exc:
            self._send_json({"error": html.escape(str(exc))}, 500)

    def _send_download(self):
        if not LAST_REPORT:
            self._send(404, b"No report generated yet.", "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", "attachment; filename=reconciliation_report.xlsx")
        self.send_header("Content-Length", str(len(LAST_REPORT)))
        self.end_headers()
        self.wfile.write(LAST_REPORT)

    def _send_json(self, payload, status=200):
        self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _json_rows(df):
    rows = []
    for record in df.astype(object).where(df.notna(), "").to_dict(orient="records"):
        rows.append({key: str(value) for key, value in record.items()})
    return rows


if __name__ == "__main__":
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"Open http://localhost:{PORT}")
        httpd.serve_forever()
