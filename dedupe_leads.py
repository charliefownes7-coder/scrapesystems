"""
dedupe_leads.py — One-time cleanup for leads.db.

Finds businesses that ended up as multiple separate rows (same name,
same address) and merges them down to ONE row each — keeping whichever
copy has real contact progress on it (anything other than "Not
Contacted") over a blank/fresh duplicate, and keeping whichever has
Facebook data filled in if there's still a tie. The losing duplicate
row(s) are deleted.

Safe to run once now, and again later if duplicates reappear for any
reason — it only ever merges identical (name, address) matches, never
touches anything else.

Run: python dedupe_leads.py
"""

import sqlite3

DB_PATH = "leads.db"


def _progress_score(row) -> int:
    """Higher = more worth keeping."""
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


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    groups = conn.execute(
        """
        SELECT business_name, address, COUNT(*) c
        FROM leads
        GROUP BY business_name COLLATE NOCASE, address COLLATE NOCASE
        HAVING c > 1
        """
    ).fetchall()

    print(f"Found {len(groups)} businesses with duplicate rows.\n")
    if not groups:
        conn.close()
        return

    total_deleted = 0
    for g in groups:
        if g["address"] is None:
            rows = conn.execute(
                "SELECT * FROM leads WHERE business_name = ? COLLATE NOCASE AND address IS NULL",
                (g["business_name"],),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM leads WHERE business_name = ? COLLATE NOCASE AND address = ? COLLATE NOCASE",
                (g["business_name"], g["address"]),
            ).fetchall()

        if not rows:
            continue

        rows_sorted = sorted(rows, key=_progress_score, reverse=True)
        keeper = rows_sorted[0]
        losers = rows_sorted[1:]

        print(f"{g['business_name']!r} ({len(rows)} rows) -> keeping id={keeper['id']} "
              f"(status={keeper['status']!r}, has_facebook={keeper['has_facebook']})")

        for loser in losers:
            conn.execute("DELETE FROM leads WHERE id = ?", (loser["id"],))
            total_deleted += 1

    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    print(f"\nDeleted {total_deleted} duplicate rows. {remaining} leads remain.")
    conn.close()


if __name__ == "__main__":
    main()
