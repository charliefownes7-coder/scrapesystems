"""
diagnose_categorization.py — Read-only checks on leads.db to figure
out why Cold Call / DM counts look wrong. Doesn't change anything.

Run: python diagnose_categorization.py
"""

import sqlite3

DB_PATH = "leads.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    print(f"Total leads: {total}\n")

    print("-- has_facebook breakdown --")
    for row in conn.execute("SELECT has_facebook, COUNT(*) c FROM leads GROUP BY has_facebook"):
        print(f"  has_facebook={row['has_facebook']}: {row['c']} leads")

    print("\n-- status value breakdown (top 20) --")
    for row in conn.execute(
        "SELECT status, COUNT(*) c FROM leads GROUP BY status ORDER BY c DESC LIMIT 20"
    ):
        print(f"  {row['status']!r}: {row['c']} leads")

    print("\n-- move_to_cold_call breakdown --")
    for row in conn.execute("SELECT move_to_cold_call, COUNT(*) c FROM leads GROUP BY move_to_cold_call"):
        print(f"  move_to_cold_call={row['move_to_cold_call']}: {row['c']} leads")

    print("\n-- duplicate business names (same name, possibly different address, that may have collided during migration) --")
    dupes = conn.execute(
        """
        SELECT business_name, COUNT(*) c
        FROM leads
        GROUP BY business_name COLLATE NOCASE
        HAVING c > 1
        ORDER BY c DESC
        LIMIT 20
        """
    ).fetchall()
    if dupes:
        for row in dupes:
            print(f"  {row['business_name']!r}: {row['c']} rows")
    else:
        print("  none found")

    print(f"\n-- sample of 5 leads with has_facebook=1 --")
    for row in conn.execute("SELECT business_name, status, has_facebook, move_to_cold_call FROM leads WHERE has_facebook=1 LIMIT 5"):
        print(f"  {dict(row)}")

    print(f"\n-- sample of 5 leads with has_facebook=0 --")
    for row in conn.execute("SELECT business_name, status, has_facebook, move_to_cold_call FROM leads WHERE has_facebook=0 LIMIT 5"):
        print(f"  {dict(row)}")

    conn.close()


if __name__ == "__main__":
    main()
