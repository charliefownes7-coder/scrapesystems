"""
migrate_sheet_to_db.py — Run this ONCE to copy your existing
"Sterling Digital Leads" sheet into leads.db.

Your Google Sheet is NOT modified or cleared by this script — it's
read-only here (only load_leads() is called, never save_leads()).
Safe to run any time; re-running just re-syncs (existing rows are
skipped as duplicates on business_name + city, new rows get added).

Usage:
    python migrate_sheet_to_db.py
"""

from agent import LeadsDB
from sheets import load_leads

DB_PATH = "leads.db"

# Your sheet doesn't store a per-lead city/province separately (same
# note as in check_facebook_backlog.py) - everything scraped so far
# was under one location hint. Adjust here if that's not accurate for
# your existing backlog.
DEFAULT_CITY = "Nova Scotia"
DEFAULT_PROVINCE = "NS"


def _status_from_sheet_row(row) -> str:
    """Best-effort map of your existing Call status / DM-ish state into
    the new status field, so in-progress work isn't lost/reset."""
    call_status = str(row.get("Call status") or "").strip()
    if call_status and call_status != "Not Contacted":
        return call_status
    return "new"


def main():
    print("Loading leads from Google Sheet (read-only)...")
    df = load_leads()
    print(f"  {len(df)} rows found in sheet.")

    db = LeadsDB(DB_PATH)
    db.init_schema()

    inserted = 0
    skipped = 0

    for _, row in df.iterrows():
        name = str(row.get("Name") or "").strip()
        if not name:
            continue

        has_fb_raw = str(row.get("Has facebook") or "").strip().lower()
        has_fb = has_fb_raw == "true"

        lead_row = {
            "business_name": name,
            "niche": str(row.get("Category") or "").strip() or None,
            "city": DEFAULT_CITY,
            "province": DEFAULT_PROVINCE,
            "phone": str(row.get("Phone") or "").strip() or None,
            "website": None,  # sheet only ever contains no-website leads
            "address": str(row.get("Adress") or "").strip() or None,
            "has_facebook": has_fb,
            "facebook_url": str(row.get("Facebook URL") or "").strip() or None,
            "source_job_id": "migrated_from_sheet",
        }

        was_inserted = db.upsert_lead(lead_row)
        if was_inserted:
            inserted += 1
            # Carry over call/DM status + routing override so nothing
            # already worked gets reset to "new" after migration.
            status = _status_from_sheet_row(row)
            moved_to_cold_call = str(row.get("Move to Cold Call") or "").strip().lower() == "true"
            channel = "cold_call" if (not has_fb or moved_to_cold_call) else "dm"
            db.mark_status(name, DEFAULT_CITY, status, outreach_channel=channel)
        else:
            skipped += 1

    print(f"\nMigration complete: {inserted} leads inserted, {skipped} already present (skipped).")
    print(f"Your Google Sheet was not modified. Local database: {DB_PATH}")


if __name__ == "__main__":
    main()
