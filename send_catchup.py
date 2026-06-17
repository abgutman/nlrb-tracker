#!/usr/bin/env python3
"""One-off: email an alert for every Region 4 filing on/after a given date.

Reads the committed nlrb_data/cases.json (kept current by the daily poll) and
sends one body_nlrb_filing email per filing to the full EMAIL_TO list. Intended
to be run via the catchup-emails.yml manual-dispatch workflow, where the Gmail
secrets live. Locally (no creds) it logs "would have sent" and sends nothing.

  python send_catchup.py --since=2026-06-15
"""
import json, sys, os, time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from email_utils import send_email, subject_nlrb_filing, body_nlrb_filing

CASES_FILE = HERE / "nlrb_data" / "cases.json"

def main():
    since = "2026-06-15"
    for a in sys.argv[1:]:
        if a.startswith("--since="):
            since = a.split("=", 1)[1]

    cases = json.loads(CASES_FILE.read_text()) if CASES_FILE.exists() else {}
    hits = [v for v in cases.values() if v.get("date_filed", "") >= since]
    hits.sort(key=lambda v: v.get("date_filed", ""))  # oldest first

    print(f"Catchup: {len(hits)} filing(s) on/after {since}", file=sys.stderr)
    sent = 0
    for c in hits:
        ok = send_email(
            subject_nlrb_filing(c.get("name", c["case_number"]), c.get("kind", "")),
            body_nlrb_filing(
                name=c.get("name", c["case_number"]),
                case_number=c["case_number"],
                kind=c.get("kind", ""),
                case_code=c.get("case_code", ""),
                employer=c.get("employer"),
                union=c.get("union"),
                city=c.get("city", ""),
                state=c.get("state", ""),
                date_filed=c.get("date_filed", ""),
                allegations=c.get("allegations", []),
                nlrb_url=c.get("nlrb_url", ""),
            ),
            log_fn=lambda m: print(m, file=sys.stderr),
        )
        if ok:
            sent += 1
            print(f"  sent: {c['case_number']} | {c.get('name','')[:40]}", file=sys.stderr)
        time.sleep(1.5)  # gentle on SMTP

    print(f"Catchup done: {sent}/{len(hits)} emails sent", file=sys.stderr)

if __name__ == "__main__":
    main()
