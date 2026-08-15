"""
db_store.py — Same interface as sheets.py (load_leads / save_leads /
REQUIRED_COLUMNS), backed by leads.db instead of Google Sheets.

To switch app.py over from the Google Sheet to the local database,
the only change needed is the import line:

    from sheets import load_leads, save_leads, REQUIRED_COLUMNS
        becomes
    from db_store import load_leads, save_leads, REQUIRED_COLUMNS

Nothing else in app.py needs to change — it already works entirely
against a DataFrame shaped like the sheet's real columns, and this
module returns/accepts that exact same shape.

WHY save_leads() UPDATES ROWS INSTEAD OF WIPING THE TABLE:
The old sheets.save_leads() cleared the whole sheet and rewrote every
row every time, which was safe there because nothing else ever wrote
to the sheet in between. That's not true anymore — the overnight
agent (agent.py) can be adding new leads to leads.db in the background
while you're using the app. So save_leads() here updates/inserts only
the rows it's given (matched by business name + address) and never
deletes or wipes rows it wasn't given — anything the agent added in
the background stays untouched and shows up next time you reload.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

DB_PATH = "leads.db"

# Matches your real sheet's column headers/order exactly, so app.py
# doesn't need to change anything about how it builds/reads the
# DataFrame.
REQUIRED_COLUMNS = [
    "Name", "Category", "Adress", "Phone",
    "Call status", "Call notes", "Text Phone Number", "Email",
    "Rating", "Reveiw count", "Has website", "Has facebook", "Facebook URL",
    "Move to Cold Call",
    "Maps URL", "Search query",
]


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_SCHEMA_ENSURED = False


def _ensure_schema(conn):
    """
    Makes sure leads.db has every column this module needs, adding any
    that are missing (safe to run repeatedly, never touches existing
    data). This runs independently of agent.py's own migration, since
    app.py only imports this module, not agent.py directly.
    """
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "leads" not in tables:
        conn.execute(
            """
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_name TEXT NOT NULL,
                niche TEXT, city TEXT, province TEXT, phone TEXT, website TEXT,
                address TEXT, has_facebook INTEGER DEFAULT 0, facebook_url TEXT,
                status TEXT DEFAULT 'Not Contacted', outreach_channel TEXT,
                source_job_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(business_name, city) ON CONFLICT IGNORE
            )
            """
        )

    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
    new_columns = {
        "call_notes": "TEXT",
        "text_phone_number": "TEXT",
        "email": "TEXT",
        "rating": "TEXT",
        "review_count": "TEXT",
        "maps_url": "TEXT",
        "search_query": "TEXT",
        "move_to_cold_call": "INTEGER DEFAULT 0",
    }
    for col, coltype in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {coltype}")

    conn.execute(
        "UPDATE leads SET status = 'Not Contacted' WHERE status = 'new' OR status IS NULL OR status = ''"
    )
    conn.commit()
    _SCHEMA_ENSURED = True


def _row_to_sheet_dict(row: sqlite3.Row) -> dict:
    return {
        "Name": row["business_name"] or "",
        "Category": row["niche"] or "",
        "Adress": row["address"] or "",
        "Phone": row["phone"] or "",
        "Call status": row["status"] or "Not Contacted",
        "Call notes": row["call_notes"] or "",
        "Text Phone Number": row["text_phone_number"] or "",
        "Email": row["email"] or "",
        "Rating": row["rating"] or "",
        "Reveiw count": row["review_count"] or "",
        "Has website": str(bool(row["website"])),
        "Has facebook": str(bool(row["has_facebook"])),
        "Facebook URL": row["facebook_url"] or "",
        "Move to Cold Call": str(bool(row["move_to_cold_call"])),
        "Maps URL": row["maps_url"] or "",
        "Search query": row["search_query"] or "",
    }


def load_leads() -> pd.DataFrame:
    with _conn() as conn:
        _ensure_schema(conn)
        rows = conn.execute("SELECT * FROM leads ORDER BY id").fetchall()

    records = [_row_to_sheet_dict(r) for r in rows]
    df = pd.DataFrame(records)

    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    return df[REQUIRED_COLUMNS] if len(df) else df


def save_leads(df: pd.DataFrame) -> None:
    """
    Upserts every row in df into leads.db, matched by (Name, Adress).
    Existing rows get their editable fields updated; a Name+Adress
    combo not already in the db gets inserted fresh. Rows already in
    the db but NOT present in df (e.g. added by the overnight agent
    after this session's df was loaded) are left completely alone.
    """
    df = df.fillna("")
    now = datetime.now(timezone.utc).isoformat()

    with _conn() as conn:
        _ensure_schema(conn)
        for _, row in df.iterrows():
            name = str(row.get("Name", "")).strip()
            address = str(row.get("Adress", "")).strip()
            if not name:
                continue

            existing = None
            if address:
                existing = conn.execute(
                    "SELECT id FROM leads WHERE business_name = ? COLLATE NOCASE AND address = ? COLLATE NOCASE",
                    (name, address),
                ).fetchone()
            phone_val = str(row.get("Phone", "")).strip()
            if not existing and phone_val:
                existing = conn.execute(
                    "SELECT id FROM leads WHERE business_name = ? COLLATE NOCASE AND phone = ? COLLATE NOCASE",
                    (name, phone_val),
                ).fetchone()
            if not existing and not address and not phone_val:
                existing = conn.execute(
                    "SELECT id FROM leads WHERE business_name = ? COLLATE NOCASE "
                    "AND (address IS NULL OR TRIM(address) = '') "
                    "AND (phone IS NULL OR TRIM(phone) = '')",
                    (name,),
                ).fetchone()

            has_website = str(row.get("Has website", "")).strip().lower() == "true"
            has_facebook = str(row.get("Has facebook", "")).strip().lower() == "true"
            move_to_cold_call = str(row.get("Move to Cold Call", "")).strip().lower() == "true"

            if existing:
                conn.execute(
                    """
                    UPDATE leads SET
                        niche = ?, phone = ?, status = ?, call_notes = ?,
                        text_phone_number = ?, email = ?, rating = ?, review_count = ?,
                        has_facebook = ?, facebook_url = ?, move_to_cold_call = ?,
                        maps_url = ?, search_query = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(row.get("Category", "")) or None,
                        str(row.get("Phone", "")) or None,
                        str(row.get("Call status", "")) or "Not Contacted",
                        str(row.get("Call notes", "")) or None,
                        str(row.get("Text Phone Number", "")) or None,
                        str(row.get("Email", "")) or None,
                        str(row.get("Rating", "")) or None,
                        str(row.get("Reveiw count", "")) or None,
                        1 if has_facebook else 0,
                        str(row.get("Facebook URL", "")) or None,
                        1 if move_to_cold_call else 0,
                        str(row.get("Maps URL", "")) or None,
                        str(row.get("Search query", "")) or None,
                        now,
                        existing["id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO leads (
                        business_name, niche, address, phone, website,
                        has_facebook, facebook_url, status, call_notes,
                        text_phone_number, email, rating, review_count,
                        move_to_cold_call, maps_url, search_query,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        str(row.get("Category", "")) or None,
                        address or None,
                        str(row.get("Phone", "")) or None,
                        "yes" if has_website else None,  # non-null = "has a website"
                        1 if has_facebook else 0,
                        str(row.get("Facebook URL", "")) or None,
                        str(row.get("Call status", "")) or "Not Contacted",
                        str(row.get("Call notes", "")) or None,
                        str(row.get("Text Phone Number", "")) or None,
                        str(row.get("Email", "")) or None,
                        str(row.get("Rating", "")) or None,
                        str(row.get("Reveiw count", "")) or None,
                        1 if move_to_cold_call else 0,
                        str(row.get("Maps URL", "")) or None,
                        str(row.get("Search query", "")) or None,
                        now,
                        now,
                    ),
                )
