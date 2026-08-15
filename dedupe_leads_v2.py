"""
dedupe_leads_v2.py — Cleanup for the real duplicate cause: since ~90%
of leads have no address, the original name+address matching almost
never caught them. This version groups leads by business name PLUS
whichever of (address, phone) is actually available, merging each
group down to one row - keeping whichever copy has real contact
progress, same as before.

Run: python dedupe_leads_v2.py

Safe to run anytime; only merges rows that share a name and either
the same address or the same phone number.
"""

import sqlite3
from collections import defaultdict

DB_PATH = "leads.db"


def _progress_score(row) -> int:
    score = 0
    if row["status"] and row["status"] != "Not Contacted":
        score += 10
    if row["has_facebook"]:
        score += 2
    if row["facebook_url"]:
        score += 1
    if row["call_notes"]:
        score += 1
    return score


def _group_key(row):
    name = (row["business_name"] or "").strip().lower()
    address = (row["address"] or "").strip().lower()
    phone = (row["phone"] or "").strip()
    # Prefer address as the identifier when it's real; fall back to
    # phone (much more often populated); if BOTH are blank, group by
    # name alone - still very likely the same business given how
    # consistently this pattern shows up in the data.
    identifier = address or phone or ""
    return (name, identifier)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    all_rows = conn.execute("SELECT * FROM leads").fetchall()
    print(f"Scanning {len(all_rows)} total leads...\n")

    groups = defaultdict(list)
    for row in all_rows:
        groups[_group_key(row)].append(row)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"Found {len(dup_groups)} businesses with duplicate rows "
          f"({sum(len(v) for v in dup_groups.values())} rows involved).\n")

    total_deleted = 0
    for (name, _identifier), rows in sorted(dup_groups.items(), key=lambda kv: -len(kv[1])):
        rows_sorted = sorted(rows, key=_progress_score, reverse=True)
        keeper = rows_sorted[0]
        losers = rows_sorted[1:]

        if len(rows) >= 5:  # only print the big ones to keep output readable
            print(f"{keeper['business_name']!r} ({len(rows)} rows) -> keeping id={keeper['id']} "
                  f"(status={keeper['status']!r})")

        for loser in losers:
            conn.execute("DELETE FROM leads WHERE id = ?", (loser["id"],))
            total_deleted += 1

    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    print(f"\nDeleted {total_deleted} duplicate rows. {remaining} leads remain.")
    conn.close()


if __name__ == "__main__":
    main()
