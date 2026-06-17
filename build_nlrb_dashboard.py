#!/usr/bin/env python3
"""Generate nlrb_dashboard.html from nlrb_data/cases.json + decisions.json.

NLRB Region 4 (Philadelphia) tracker: union election petitions, unfair-labor-
practice charges, and Board/ALJ/Regional Director decisions. Client-side tabs
toggle between All / ULP / Representation / Decisions.
"""
import json, html as html_mod
from pathlib import Path
from datetime import datetime, timezone, timedelta

import sys, os
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))   # prefer the local business auth_gate (biztools2026)
from auth_gate import inject_auth

ET = timezone(timedelta(hours=-4))
ND = HERE / "nlrb_data"
CASES_FILE = ND / "cases.json"
DECISIONS_FILE = ND / "decisions.json"
OUT_FILE = HERE / "nlrb_dashboard.html"

PRIMARY = "#2c3e50"   # slate

def esc(s):
    return html_mod.escape(str(s) if s is not None else "")

def fmt_date(iso):
    if not iso:
        return "—"
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%b %-d, %Y")
    except Exception:
        return iso[:10]

def build():
    cases = json.loads(CASES_FILE.read_text()) if CASES_FILE.exists() else {}
    decisions = json.loads(DECISIONS_FILE.read_text()) if DECISIONS_FILE.exists() else {}

    today = datetime.now(ET).date()
    seven = (today - timedelta(days=7)).isoformat()

    filings = sorted(cases.values(), key=lambda c: c.get("date_filed", ""), reverse=True)
    decs = sorted(decisions.values(), key=lambda d: d.get("date", ""), reverse=True)

    n_ulp = sum(1 for c in filings if c.get("kind") == "ULP")
    n_rep = sum(1 for c in filings if c.get("kind") == "Representation")
    new_filings = sum(1 for c in filings if c.get("date_filed", "") >= seven)
    new_decs = sum(1 for d in decs if d.get("date", "") >= seven)

    # ── filings rows ──
    filing_rows = []
    for c in filings:
        kind = c.get("kind", "Other")
        data_kind = "ulp" if kind == "ULP" else "rep" if kind == "Representation" else "other"
        is_new = c.get("date_filed", "") >= seven
        badge = ' <span class="new-badge">NEW</span>' if is_new else ""
        pill_cls = "pill-ulp" if kind == "ULP" else "pill-rep"
        pill = f'<span class="pill {pill_cls}">{esc(c.get("case_code",""))}</span>'

        parties = []
        if c.get("employer"):
            parties.append(f'<span class="muted">Emp:</span> {esc(c["employer"])}')
        if c.get("union"):
            parties.append(f'<span class="muted">Union:</span> {esc(c["union"])}')
        parties_html = "<br>".join(parties) if parties else "—"

        loc = ", ".join(p for p in [c.get("city",""), c.get("state","")] if p) or "—"
        latest = c.get("latest_action") or "—"
        cn = c.get("case_number", "")

        filing_rows.append(f"""      <tr data-kind="{data_kind}" class="filing-row">
        <td>{esc(fmt_date(c.get("date_filed")))}{badge}</td>
        <td class="primary-name">{esc(c.get("name",""))}</td>
        <td><a href="{esc(c.get("nlrb_url",""))}" target="_blank">{esc(cn)}</a> {pill}</td>
        <td>{parties_html}</td>
        <td>{esc(loc)}</td>
        <td>{esc(c.get("status",""))}</td>
        <td>{esc(latest)}</td>
      </tr>""")

    # ── decision rows ──
    dec_rows = []
    for d in decs:
        is_new = d.get("date", "") >= seven
        badge = ' <span class="new-badge">NEW</span>' if is_new else ""
        cn = d.get("case_number", "")
        dec_rows.append(f"""      <tr>
        <td>{esc(fmt_date(d.get("date")))}{badge}</td>
        <td class="primary-name">{esc(d.get("name",""))}</td>
        <td><a href="{esc(d.get("nlrb_url",""))}" target="_blank">{esc(cn)}</a></td>
        <td><strong>{esc(d.get("decision_type",""))}</strong></td>
        <td>{esc(d.get("actor","") or "—")}</td>
      </tr>""")

    updated = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    empty_f = "    <tr><td colspan='7' style='text-align:center;padding:40px;color:#6c757d;'>No filings tracked yet.</td></tr>"
    empty_d = "    <tr><td colspan='5' style='text-align:center;padding:40px;color:#6c757d;'>No decisions tracked yet.</td></tr>"

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NLRB Region 4 Tracker — Av's Tools</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif; background:#eef0f3; color:#1a1a2e; }}
  .header {{ background:{PRIMARY}; color:white; padding:28px 32px; }}
  .header h1 {{ font-size:24px; font-weight:700; margin-bottom:4px; }}
  .header p {{ font-size:14px; opacity:0.85; }}
  .container {{ max-width:1180px; margin:24px auto; padding:0 16px; }}
  .summary {{ display:flex; gap:16px; margin-bottom:20px; flex-wrap:wrap; }}
  .stat-card {{ background:white; border-radius:8px; padding:18px 24px; flex:1; min-width:140px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
  .stat-card .num {{ font-size:28px; font-weight:700; color:{PRIMARY}; }}
  .stat-card .label {{ font-size:13px; color:#6c757d; margin-top:4px; }}
  .tabs {{ display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; }}
  .tab {{ background:white; border:1px solid #dde1e6; color:#495057; padding:8px 16px; border-radius:20px; font-size:13px; font-weight:600; cursor:pointer; }}
  .tab.active {{ background:{PRIMARY}; color:white; border-color:{PRIMARY}; }}
  table {{ width:100%; background:white; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.06); border-collapse:collapse; }}
  thead {{ background:#f8f9fa; }}
  th {{ padding:12px 14px; text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:0.5px; color:#6c757d; border-bottom:2px solid #e9ecef; }}
  td {{ padding:11px 14px; font-size:14px; border-bottom:1px solid #f0f0f0; vertical-align:top; }}
  tr:hover {{ background:#fafbfc; }}
  .primary-name {{ font-weight:600; }}
  .muted {{ color:#9aa0a6; font-size:12px; }}
  .new-badge {{ display:inline-block; background:#d4380d; color:white; font-size:10px; font-weight:700; padding:2px 6px; border-radius:3px; vertical-align:middle; margin-left:6px; }}
  .pill {{ display:inline-block; font-size:10px; font-weight:700; padding:2px 7px; border-radius:10px; vertical-align:middle; }}
  .pill-ulp {{ background:#fde2e0; color:#8e1600; }}
  .pill-rep {{ background:#dde9f5; color:#1c4e80; }}
  a {{ color:{PRIMARY}; text-decoration:none; font-weight:500; }}
  a:hover {{ text-decoration:underline; }}
  .panel.hidden {{ display:none; }}
  .footer {{ margin:24px 0; text-align:center; font-size:12px; color:#adb5bd; }}
  .footer a {{ color:#adb5bd; }}
  @media (max-width:760px) {{ th, td {{ padding:8px 10px; font-size:13px; }} .summary {{ flex-direction:column; }} }}
</style>
</head>
<body>

<div style="position:fixed;right:0;top:50%;transform:translateY(-50%);background:#c0392b;color:white;padding:12px 8px;font-size:11px;font-weight:700;letter-spacing:1px;writing-mode:vertical-rl;text-orientation:mixed;z-index:9999;border-radius:4px 0 0 4px;box-shadow:-2px 0 8px rgba(0,0,0,0.2);">AI-BUILT DASHBOARD &mdash; NEVER CITE DIRECTLY &mdash; ALWAYS CHECK THE CASE RECORD</div>

<div class="header">
  <h1>NLRB Region 4 Labor Tracker</h1>
  <p>Union petitions, unfair-labor-practice charges &amp; Board/ALJ decisions &middot; NLRB Region 4 (Philadelphia): eastern PA, southern NJ, Delaware &middot; Updated {updated}</p>
</div>

<div class="container">

<div class="summary">
  <div class="stat-card"><div class="num">{len(filings)}</div><div class="label">Cases tracked</div></div>
  <div class="stat-card"><div class="num">{new_filings}</div><div class="label">New filings (7 days)</div></div>
  <div class="stat-card"><div class="num">{new_decs}</div><div class="label">New decisions (7 days)</div></div>
  <div class="stat-card"><div class="num">{n_ulp} / {n_rep}</div><div class="label">ULP / Representation</div></div>
</div>

<div class="tabs">
  <button class="tab active" data-tab="all">All filings</button>
  <button class="tab" data-tab="ulp">ULP charges</button>
  <button class="tab" data-tab="rep">Representation</button>
  <button class="tab" data-tab="decisions">Decisions &amp; rulings</button>
</div>

<div class="panel" id="panel-filings">
<table>
  <thead><tr>
    <th>Date Filed</th><th>Party</th><th>Case No.</th><th>Employer / Union</th><th>Location</th><th>Status</th><th>Latest Action</th>
  </tr></thead>
  <tbody>
{"".join(filing_rows) if filing_rows else empty_f}
  </tbody>
</table>
</div>

<div class="panel hidden" id="panel-decisions">
<table>
  <thead><tr>
    <th>Date</th><th>Party</th><th>Case No.</th><th>Decision Type</th><th>Issued By</th>
  </tr></thead>
  <tbody>
{"".join(dec_rows) if dec_rows else empty_d}
  </tbody>
</table>
</div>

<div class="footer">
  <p>Av's Tools &middot; Newsroom monitor &middot; Built with <a href="https://claude.ai">Claude</a> (Anthropic AI)</p>
  <p style="margin-top:4px;">Source: <a href="https://github.com/labordata/nlrb-data">labordata/nlrb-data</a> + <a href="https://www.nlrb.gov">nlrb.gov</a> &middot; Always verify against the official case record before reporting.</p>
</div>

</div>

<script>
(function(){{
  var tabs = document.querySelectorAll('.tab');
  var pFil = document.getElementById('panel-filings');
  var pDec = document.getElementById('panel-decisions');
  var rows = document.querySelectorAll('.filing-row');
  tabs.forEach(function(t){{
    t.addEventListener('click', function(){{
      tabs.forEach(function(x){{ x.classList.remove('active'); }});
      t.classList.add('active');
      var tab = t.getAttribute('data-tab');
      if (tab === 'decisions') {{
        pFil.classList.add('hidden'); pDec.classList.remove('hidden'); return;
      }}
      pDec.classList.add('hidden'); pFil.classList.remove('hidden');
      rows.forEach(function(r){{
        var k = r.getAttribute('data-kind');
        r.style.display = (tab === 'all' || tab === k) ? '' : 'none';
      }});
    }});
  }});
}})();
</script>

</body>
</html>"""

    page_html = inject_auth(page_html)
    OUT_FILE.write_text(page_html)
    print(f"Written {OUT_FILE.name} — {len(filings)} filings, {len(decs)} decisions, updated {updated}")

if __name__ == "__main__":
    build()
