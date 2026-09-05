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
    "Move to Cold Call", "Screened",
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
        "screened": "TEXT",
    }
    for col, coltype in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {coltype}")

    conn.execute(
        "UPDATE leads SET status = 'Not Contacted' WHERE status = 'new' OR status IS NULL OR status = ''"
    )

    # One-time catch-up for leads that existed BEFORE the swipe tool
    # did: they never got a 'screened' value, but their status already
    # tells us how they should be categorized now that Good Leads /
    # Bad Leads tabs exist. Only touches rows where screened is still
    # blank, so this can't overwrite anything the swipe tool (or a
    # later run of this same backfill) already set - safe to run every
    # time _ensure_schema runs, effectively a no-op after the first.
    #
    # A DM lead (has a Facebook page, not moved to Cold Call) that's
    # still sitting at "Not Contacted" hasn't been looked at yet -
    # leave it alone, it belongs in the regular swipe queue.
    # Anything else already got SOME kind of human decision:
    #   - status = 'Bad Lead'  -> screened = 'bad'  (matches what the
    #     swipe tool's Bad button does, minus forcing a move to Cold
    #     Call - the original "Bad Lead" flag was always meant to mark
    #     without moving, so this backfill keeps that intact)
    #   - any other real status (DM Sent, Replied - Interested,
    #     No Response, Not Interested, Bot Answered, Call Booked,
    #     Has website) -> screened = 'good', since it was actively
    #     contacted/reviewed already, obviously not a dead page
    conn.execute(
        """
        UPDATE leads SET screened = 'bad'
        WHERE (screened IS NULL OR screened = '')
          AND has_facebook = 1
          AND (move_to_cold_call IS NULL OR move_to_cold_call = 0)
          AND status = 'Bad Lead'
        """
    )
    conn.execute(
        """
        UPDATE leads SET screened = 'good'
        WHERE (screened IS NULL OR screened = '')
          AND has_facebook = 1
          AND (move_to_cold_call IS NULL OR move_to_cold_call = 0)
          AND status IS NOT NULL AND status != '' AND status != 'Not Contacted'
        """
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
        "Screened": row["screened"] or "",
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


# Refuses a save outright if it would change the status of more than
# this many EXISTING leads in one call — see save_leads()'s docstring.
# A real person reviewing leads one at a time never legitimately hits
# this; a bulk accidental grid interaction easily does.
MASS_STATUS_CHANGE_THRESHOLD = 20


def save_leads(df: pd.DataFrame) -> None:
    """
    Upserts every row in df into leads.db, matched by (Name, Adress).
    Existing rows get their editable fields updated; a Name+Adress
    combo not already in the db gets inserted fresh. Rows already in
    the db but NOT present in df (e.g. added by the overnight agent
    after this session's df was loaded) are left completely alone.

    SAFETY GUARD (added after the mass "Bad Lead" incident): before
    writing anything, this does a read-only pass comparing each row's
    "Call status" in df against what's currently stored for that same
    lead. If more than MASS_STATUS_CHANGE_THRESHOLD existing leads
    would have their status changed by this one call, the ENTIRE save
    is refused and nothing is written — not even the rows that were
    genuinely intended. Raises ValueError, which callers (app.py's
    save try/except) already catch and surface as an error message
    instead of crashing. This is deliberately blunt: a legitimate
    editing session changes a status one (or a handful) at a time, so
    a jump this large in a single save is far more likely to be an
    accidental multi-row grid interaction than real work — better to
    stop and let the person look than to silently apply it.
    """
    df = df.fillna("")
    now = datetime.now(timezone.utc).isoformat()

    with _conn() as conn:
        _ensure_schema(conn)

        # --- Pass 1: read-only. Look up what already exists for every
        # row and count how many real status changes this save would
        # make, WITHOUT writing anything yet. ---
        planned = []
        status_changes = 0
        for _, row in df.iterrows():
            name = str(row.get("Name", "")).strip()
            address = str(row.get("Adress", "")).strip()
            if not name:
                continue

            existing = None
            if address:
                existing = conn.execute(
                    "SELECT id, status FROM leads WHERE business_name = ? COLLATE NOCASE AND address = ? COLLATE NOCASE",
                    (name, address),
                ).fetchone()
            phone_val = str(row.get("Phone", "")).strip()
            if not existing and phone_val:
                existing = conn.execute(
                    "SELECT id, status FROM leads WHERE business_name = ? COLLATE NOCASE AND phone = ? COLLATE NOCASE",
                    (name, phone_val),
                ).fetchone()
            if not existing and not address and not phone_val:
                existing = conn.execute(
                    "SELECT id, status FROM leads WHERE business_name = ? COLLATE NOCASE "
                    "AND (address IS NULL OR TRIM(address) = '') "
                    "AND (phone IS NULL OR TRIM(phone) = '')",
                    (name,),
                ).fetchone()

            if existing:
                new_status = str(row.get("Call status", "")) or "Not Contacted"
                old_status = existing["status"] or "Not Contacted"
                if new_status != old_status:
                    status_changes += 1

            planned.append((name, address, phone_val, existing, row))

        if status_changes > MASS_STATUS_CHANGE_THRESHOLD:
            raise ValueError(
                f"Refusing to save: this would change the status of "
                f"{status_changes} existing leads at once (limit is "
                f"{MASS_STATUS_CHANGE_THRESHOLD}). This is almost always an "
                "accidental multi-row edit (paste, fill, drag, or a "
                f"select-all-style gesture) rather than {status_changes} "
                "intentional one-by-one reviews. Nothing was saved — "
                "reload the page to discard the change, or make the "
                "edits again a few at a time."
            )

        # --- Pass 2: now that it's passed the safety check, actually
        # write. Reuses the lookups from pass 1 instead of re-querying.
        #
        # addr_cache/phone_cache/name_cache track rows INSERTED earlier
        # in this same pass, mirroring the same address -> phone ->
        # name-only fallback order as the db lookup above. Without
        # this, two rows in the SAME save() call for the same new lead
        # (e.g. a duplicate in the incoming dataframe) would both come
        # back existing=None from pass 1 (neither is in the db yet)
        # and both get INSERTed — a real duplicate. The original
        # single-pass version avoided this by accident, since each
        # row's lookup ran right before its own write and so could see
        # the previous row's just-committed insert; splitting into two
        # passes for the safety check above lost that side effect, so
        # it's restored explicitly here instead. ---
        addr_cache: dict = {}
        phone_cache: dict = {}
        name_cache: dict = {}

        for name, address, phone_val, existing, row in planned:
            name_l = name.lower()
            if existing is None:
                if address:
                    cached_id = addr_cache.get((name_l, address.strip().lower()))
                    if cached_id:
                        existing = {"id": cached_id}
                if existing is None and phone_val:
                    cached_id = phone_cache.get((name_l, phone_val.strip().lower()))
                    if cached_id:
                        existing = {"id": cached_id}
                if existing is None and not address and not phone_val:
                    cached_id = name_cache.get(name_l)
                    if cached_id:
                        existing = {"id": cached_id}

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
                        screened = ?, maps_url = ?, search_query = ?, updated_at = ?
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
                        str(row.get("Screened", "")) or None,
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
                        move_to_cold_call, screened, maps_url, search_query,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        str(row.get("Screened", "")) or None,
                        str(row.get("Maps URL", "")) or None,
                        str(row.get("Search query", "")) or None,
                        now,
                        now,
                    ),
                )
                new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                if address:
                    addr_cache[(name_l, address.strip().lower())] = new_id
                if phone_val:
                    phone_cache[(name_l, phone_val.strip().lower())] = new_id
                if not address and not phone_val:
                    name_cache[name_l] = new_id
