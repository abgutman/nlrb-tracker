#!/usr/bin/env python3
"""NLRB Region 4 (Philadelphia) tracker — new filings + decisions/rulings.

Region 4 covers eastern Pennsylvania, southern New Jersey, and Delaware
(New Castle County). Cases are identified by the "04-" case-number prefix.

Modes:
  python poll_nlrb.py backfill                  # 2026-01-01 → today
  python poll_nlrb.py backfill --start=2025-01-01 --end=2025-12-31
  python poll_nlrb.py poll                      # since last scan (with lookback)
  python poll_nlrb.py poll --live              # also send email alerts

Data source: labordata/nlrb-data nightly SQLite database, which mirrors the
NLRB's own case data (https://github.com/labordata/nlrb-data). We download the
nightly DB, then read it locally with stdlib sqlite3. The DB lags the live
nlrb.gov site by ~24-36h; a lookback window absorbs that lag so late-arriving
cases are not missed.

Two event streams are tracked against the same Region 4 cases:
  - FILINGS    : new ULP charges (C cases) + representation/election petitions
                 (R cases), keyed by case_number in cases.json
  - DECISIONS  : ALJ / Board / Regional Director decisions + election
                 certifications, pulled from the docket table and keyed by
                 "<case_number>::<date>::<hash>" in decisions.json

NOTE on the "hybrid" same-day scrape: the labordata docket table already
carries each case's latest docket action (the same data the public
nlrb.gov/case page shows), so we derive `latest_action` from the DB rather
than scraping the undocumented nlrb.gov SPA endpoint. `enrich_from_case_page`
is a documented, best-effort opt-in stub for future same-day enrichment.
"""
import hashlib, json, os, re, subprocess, sys, zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from email_utils import (
    send_email,
    subject_nlrb_filing, body_nlrb_filing,
    subject_nlrb_decision, body_nlrb_decision,
)

HERE = Path(__file__).parent
ND = HERE / "nlrb_data"
ND.mkdir(exist_ok=True)
CASES_FILE = ND / "cases.json"
DECISIONS_FILE = ND / "decisions.json"
STATE_FILE = ND / "scan_state.json"
LOG_FILE = ND / "poll_nlrb_log.txt"
DB_FILE = ND / "nlrb.db"        # gitignored — large, never committed

NLRB_DB_URL = "https://github.com/labordata/nlrb-data/releases/download/nightly/nlrb.db.zip"
UA = "Inquirer Newsroom agutman@inquirer.com"

REGION_PREFIX = "04-"
DEFAULT_BACKFILL_START = "2026-01-01"
FILING_LOOKBACK_DAYS = 7        # absorbs labordata nightly lag for filings
DECISION_LOOKBACK_DAYS = 14     # decisions are low-volume; wider window is cheap
STALE_DB_WARN_DAYS = 3          # warn if the source DB hasn't updated recently

ET = timezone(timedelta(hours=-4))

# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ── Case-type classification (by case-number infix) ──────────────────────────

C_CASE_RE = re.compile(r"^04-C[A-Z]-", re.I)   # CA/CB/CC/CD/CE/CG/CP = ULP charges
R_CASE_RE = re.compile(r"^04-[RU][A-Z]-", re.I)  # RC/RD/RM/UC/UD = representation

def classify_kind(case_number):
    if C_CASE_RE.match(case_number):
        return "ULP"
    if R_CASE_RE.match(case_number):
        return "Representation"
    return "Other"

def case_code(case_number):
    """Return the two-letter infix, e.g. 'CA' or 'RC'."""
    parts = case_number.split("-")
    return parts[1].upper() if len(parts) >= 2 else ""

# ── Decision classification (whitelist; tuned against real docket strings) ────
# Each (label, regex) is checked in order; first match wins. Patterns are
# deliberately specific to avoid procedural traps like "RD Order to Reschedule
# Hearing", "Post-Hearing Brief to ALJ", "Exceptions to ALJD", court orders,
# and party-filed requests for review. Validated against Region 4 docket data.
DECISION_PATTERNS = [
    ("ALJ Decision",
     re.compile(r"administrative law judge.*decision|\balj decision\b", re.I)),
    ("Board Decision",
     re.compile(r"board decision|order adopting.*absence of exceptions", re.I)),
    ("Regional Director Decision",
     re.compile(r"decision and direction of election|post-election rd decision|^\s*rd\b.*decision", re.I)),
    ("Certification of Representative",
     re.compile(r"^\s*certification of representative", re.I)),
    ("Election Results Certified",
     re.compile(r"^\s*certification of results", re.I)),
]

def classify_decision(document):
    """Return a decision-type label for a docket document, or None if it is
    not a substantive ruling/decision."""
    if not document:
        return None
    for label, rx in DECISION_PATTERNS:
        if rx.search(document):
            return label
    return None

# ── State ────────────────────────────────────────────────────────────────────

def _load(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default

def load_cases():     return _load(CASES_FILE, {})
def load_decisions(): return _load(DECISIONS_FILE, {})
def load_state():     return _load(STATE_FILE, {})

def save_cases(c):     CASES_FILE.write_text(json.dumps(c, indent=1))
def save_decisions(d): DECISIONS_FILE.write_text(json.dumps(d, indent=1))
def save_state(s):     STATE_FILE.write_text(json.dumps(s, indent=1))

# ── DB acquisition ───────────────────────────────────────────────────────────

def download_db():
    """Download + unzip the labordata nightly SQLite DB into nlrb_data/nlrb.db.
    Returns True on success."""
    zip_path = ND / "nlrb.db.zip"
    log(f"  downloading nightly DB from {NLRB_DB_URL}")
    out = subprocess.run(
        ["curl", "-sL", "-H", f"User-Agent: {UA}", "--max-time", "900",
         NLRB_DB_URL, "-o", str(zip_path)],
        capture_output=True, text=True, timeout=960,
    )
    if out.returncode != 0 or not zip_path.exists():
        log(f"  ERROR: download failed (curl exit {out.returncode})")
        return False
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(ND)
    except zipfile.BadZipFile:
        log("  ERROR: downloaded file is not a valid zip")
        return False
    finally:
        zip_path.unlink(missing_ok=True)
    ok = DB_FILE.exists()
    if ok:
        size_mb = DB_FILE.stat().st_size / 1e6
        log(f"  DB ready: {DB_FILE.name} ({size_mb:.0f} MB)")
    return ok

def connect():
    import sqlite3
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn

def db_freshness(conn):
    """Max filing.updated_at — used to warn if the source DB has gone stale."""
    row = conn.execute("SELECT max(updated_at) m FROM filing WHERE case_number LIKE ?",
                       (REGION_PREFIX + "%",)).fetchone()
    return row["m"] if row else None

# ── Detail lookups ───────────────────────────────────────────────────────────

def get_participants(conn, case_number):
    """Return (employer, union) display names from the participant table."""
    rows = conn.execute(
        "SELECT participant, type, subtype FROM participant WHERE case_number = ?",
        (case_number,)).fetchall()
    employer = union = None
    for r in rows:
        sub = (r["subtype"] or "").strip()
        name = (r["participant"] or "").strip()
        if not name:
            continue
        if sub == "Employer" and not employer:
            employer = name
        elif sub == "Union" and not union:
            union = name
    return employer, union

def get_allegations(conn, case_number):
    rows = conn.execute(
        "SELECT DISTINCT allegation FROM allegation WHERE case_number = ? AND allegation <> ''",
        (case_number,)).fetchall()
    return [r["allegation"] for r in rows]

def get_latest_action(conn, case_number):
    """Most recent docket entry (document, date) for a case — the same 'latest
    activity' the public case page shows."""
    row = conn.execute(
        "SELECT document, date FROM docket WHERE case_number = ? "
        "AND date IS NOT NULL AND date <> '' ORDER BY date DESC, rowid DESC LIMIT 1",
        (case_number,)).fetchone()
    if row:
        return row["document"], row["date"]
    return None, None

def enrich_from_case_page(case_number):
    """OPT-IN STUB for future same-day scraping of nlrb.gov/case/<n>.

    The labordata docket table already provides each case's latest action, so
    this is intentionally a no-op for v1. If wired up later, it must be
    best-effort (try/except, throttled ~1 req/s, capped per run) and never the
    sole source — the DB stays authoritative.
    """
    return {}

# ── Scans ────────────────────────────────────────────────────────────────────

def scan_filings(conn, filed_after, filed_before, live):
    cases = load_cases()
    new_hits = 0
    sql = ("SELECT case_number, name, case_type, url, city, state, date_filed, "
           "region_assigned, status, date_closed FROM filing "
           "WHERE case_number LIKE ? AND date_filed >= ?")
    params = [REGION_PREFIX + "%", filed_after]
    if filed_before:
        sql += " AND date_filed <= ?"
        params.append(filed_before)
    sql += " ORDER BY date_filed ASC"

    for r in conn.execute(sql, params).fetchall():
        cn = r["case_number"]
        if cn in cases:
            continue
        kind = classify_kind(cn)
        employer, union = get_participants(conn, cn)
        allegations = get_allegations(conn, cn) if kind == "ULP" else []
        latest_doc, latest_date = get_latest_action(conn, cn)
        nlrb_url = r["url"] or f"https://www.nlrb.gov/case/{cn}"

        cases[cn] = {
            "case_number": cn,
            "name": r["name"] or "",
            "kind": kind,
            "case_type": r["case_type"] or "",
            "case_code": case_code(cn),
            "employer": employer or (r["name"] if kind == "ULP" else None),
            "union": union,
            "city": r["city"] or "",
            "state": r["state"] or "",
            "region_assigned": r["region_assigned"] or "",
            "date_filed": r["date_filed"] or "",
            "status": r["status"] or "",
            "date_closed": r["date_closed"] or None,
            "allegations": allegations,
            "nlrb_url": nlrb_url,
            "latest_action": latest_doc or "",
            "latest_action_date": latest_date or "",
            "captured_at": datetime.now(ET).isoformat(timespec="seconds"),
        }
        new_hits += 1
        log(f"  + FILING: {cn} | {case_code(cn)} | {(r['name'] or '')[:50]}")

        if live:
            try:
                send_email(
                    subject_nlrb_filing(r["name"] or cn, kind),
                    body_nlrb_filing(
                        name=r["name"] or cn, case_number=cn, kind=kind,
                        case_code=case_code(cn), employer=employer,
                        union=union, city=r["city"] or "", state=r["state"] or "",
                        date_filed=r["date_filed"] or "", allegations=allegations,
                        nlrb_url=nlrb_url,
                    ),
                    log_fn=log,
                )
            except Exception as e:
                log(f"  email error (filing {cn}): {e}")

    save_cases(cases)
    log(f"  filings: {new_hits} new")
    return new_hits

def scan_decisions(conn, since_date, live):
    decisions = load_decisions()
    new_hits = 0
    # SQLite has no regexp; pull candidate docket rows by date and classify
    # the document string in Python via the whitelist.
    sql = ("SELECT d.case_number, d.date, d.document, d.actor, d.url, "
           "f.name, f.case_type FROM docket d JOIN filing f "
           "ON f.case_number = d.case_number "
           "WHERE d.case_number LIKE ? AND d.date >= ? "
           "ORDER BY d.date ASC")
    for r in conn.execute(sql, (REGION_PREFIX + "%", since_date)).fetchall():
        dtype = classify_decision(r["document"])
        if not dtype:
            continue
        cn = r["case_number"]
        date = r["date"] or ""
        h = hashlib.sha1((r["document"] or "").encode()).hexdigest()[:4]
        key = f"{cn}::{date}::{h}"
        if key in decisions:
            continue
        nlrb_url = r["url"] or f"https://www.nlrb.gov/case/{cn}"
        decisions[key] = {
            "case_number": cn,
            "name": r["name"] or "",
            "kind": classify_kind(cn),
            "case_code": case_code(cn),
            "decision_type": dtype,
            "date": date,
            "actor": r["actor"] or "",
            "document": r["document"] or "",
            "nlrb_url": nlrb_url,
            "captured_at": datetime.now(ET).isoformat(timespec="seconds"),
        }
        new_hits += 1
        log(f"  + DECISION: {cn} | {dtype} | {date} | {(r['name'] or '')[:40]}")

        if live:
            try:
                send_email(
                    subject_nlrb_decision(r["name"] or cn, dtype),
                    body_nlrb_decision(
                        name=r["name"] or cn, case_number=cn,
                        kind=classify_kind(cn), decision_type=dtype,
                        actor=r["actor"] or "", date=date, nlrb_url=nlrb_url,
                    ),
                    log_fn=log,
                )
            except Exception as e:
                log(f"  email error (decision {key}): {e}")

    save_decisions(decisions)
    log(f"  decisions: {new_hits} new")
    return new_hits

# ── Main ─────────────────────────────────────────────────────────────────────

def _date_minus(date_str, days):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        d = datetime.now(ET)
    return (d - timedelta(days=days)).strftime("%Y-%m-%d")

def main():
    args = sys.argv[1:]
    mode = args[0] if args and not args[0].startswith("--") else "poll"
    live = "--live" in args
    skip_download = "--no-download" in args  # dev: reuse existing local DB

    if skip_download and DB_FILE.exists():
        log(f"  --no-download: reusing existing {DB_FILE.name}")
    elif not download_db():
        log("ERROR: could not obtain NLRB database; aborting")
        sys.exit(1)

    conn = connect()
    state = load_state()

    fresh = db_freshness(conn)
    if fresh:
        log(f"  source DB freshness (max filing.updated_at): {fresh}")
        try:
            fdate = datetime.fromisoformat(str(fresh)[:10])
            if (datetime.now() - fdate).days > STALE_DB_WARN_DAYS:
                log(f"  ⚠ WARNING: source DB appears stale (>{STALE_DB_WARN_DAYS}d old)")
        except ValueError:
            pass

    today = datetime.now(ET).strftime("%Y-%m-%d")

    if mode == "backfill":
        start = DEFAULT_BACKFILL_START
        end = None
        for a in args:
            if a.startswith("--start="):
                start = a.split("=", 1)[1]
            elif a.startswith("--end="):
                end = a.split("=", 1)[1]
        log(f"=== NLRB backfill (filings+decisions {start} → {end or 'today'}, live={live}) ===")
        f = scan_filings(conn, filed_after=start, filed_before=end, live=live)
        d = scan_decisions(conn, since_date=start, live=live)
        state["backfill_done"] = True
        state["backfill_through"] = today
        state["last_poll_date"] = today
        state["db_updated_at"] = fresh
        save_state(state)
        log(f"=== Backfill done. {f} filings, {d} decisions. ===\n")

    elif mode == "poll":
        last = state.get("last_poll_date") or _date_minus(today, 1)
        filed_after = _date_minus(last, FILING_LOOKBACK_DAYS)
        dec_after = _date_minus(last, DECISION_LOOKBACK_DAYS)
        log(f"=== NLRB poll (since {last}; filings≥{filed_after}, decisions≥{dec_after}, live={live}) ===")
        f = scan_filings(conn, filed_after=filed_after, filed_before=None, live=live)
        d = scan_decisions(conn, since_date=dec_after, live=live)
        state["last_poll_date"] = today
        state["db_updated_at"] = fresh
        save_state(state)
        log(f"=== Poll done. {f} new filings, {d} new decisions. ===\n")

    else:
        print(f"Unknown mode: {mode}. Use 'backfill' or 'poll'.", file=sys.stderr)
        sys.exit(1)

    conn.close()

if __name__ == "__main__":
    main()
