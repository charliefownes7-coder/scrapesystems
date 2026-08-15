"""
diagnose_dupes2.py — Look at exactly why a specific business still has
multiple rows, to figure out what the name+address dedup check is
missing (e.g. inconsistent address formatting between scrapes, blank
addresses, near-duplicate business names).

Run: python diagnose_dupes2.py "MJR Flooring"
(or with no argument, defaults to MJR Flooring)
"""

import sqlite3
import sys

DB_PATH = "leads.db"


def main():
    search_term = sys.argv[1] if len(sys.argv) > 1 else "MJR Flooring"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, business_name, address, phone, city, niche, status, source_job_id "
        "FROM leads WHERE business_name LIKE ?",
        (f"%{search_term}%",),
    ).fetchall()

    print(f"Found {len(rows)} rows matching {search_term!r}:\n")
    for r in rows:
        print(
            f"  id={r['id']:>5}  name={r['business_name']!r:55}  "
            f"address={r['address']!r:30}  phone={r['phone']!r:20}  "
            f"city={r['city']!r:15}  status={r['status']!r}  job={r['source_job_id']!r}"
        )

    # Also check overall scale: how many businesses have a NULL/blank address
    # AND how many have a NULL/blank phone too - if a lead is missing
    # BOTH, neither of our matching keys can catch it as a duplicate.
    total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    blank_address = conn.execute(
        "SELECT COUNT(*) c FROM leads WHERE address IS NULL OR TRIM(address) = ''"
    ).fetchone()["c"]
    blank_phone = conn.execute(
        "SELECT COUNT(*) c FROM leads WHERE phone IS NULL OR TRIM(phone) = ''"
    ).fetchone()["c"]
    blank_both = conn.execute(
        "SELECT COUNT(*) c FROM leads WHERE (address IS NULL OR TRIM(address) = '') "
        "AND (phone IS NULL OR TRIM(phone) = '')"
    ).fetchone()["c"]
    print(f"\nOverall: {total} total leads")
    print(f"  blank address: {blank_address} ({round(100*blank_address/total, 1) if total else 0}%)")
    print(f"  blank phone: {blank_phone} ({round(100*blank_phone/total, 1) if total else 0}%)")
    print(f"  blank BOTH (can't be deduped by either key): {blank_both} "
          f"({round(100*blank_both/total, 1) if total else 0}%)")

    conn.close()


if __name__ == "__main__":
    main()
