"""
agent.py — ScrapeSystems overnight lead-gen agent, all in one file.

Combines what used to be 5 separate files (db, geocode, lead_mapper,
command_parser, job_runner) into one, since there was no real reason
to keep them apart for a single-person project.

SETUP (one time):
    pip install requests

RUN IT:
    python agent.py "New Brunswick, dentists + plumbers + HVAC"

What happens:
  1. Parses your command into a region + list of niches
  2. Looks up every real city/town in that region (free OSM geocoding
     API — no hardcoded city list to maintain)
  3. Builds a job for every city x niche combo, queues them in leads.db
  4. Runs your existing scraper.run_scrape(niche, city) on each job,
     one at a time (NOT in parallel — your scraper already paces
     itself with random delays specifically to avoid getting blocked;
     running jobs in parallel would defeat that)
  5. Writes each result straight into leads.db as it's found, deduped
     against everything already in there (including your migrated
     Sheet history) — so a crash mid-run doesn't lose finished work
  6. Marks each job done/failed, logs progress to agent_run.log

If interrupted (crash, laptop sleep, Ctrl+C) — just run the SAME
command again. Already-completed jobs are skipped automatically.

To move your existing Google Sheet leads into leads.db first, run
migrate_sheet_to_db.py once (separate file, since it's a one-time
utility you'll only ever run once).
"""

import argparse
import re
import sqlite3
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests

from scraper import run_scrape

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = "leads.db"
MIN_CITY_POPULATION = 1000
MAX_RETRIES_PER_JOB = 2

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]
USER_AGENT = "ScrapeSystems-LeadGen/1.0 (contact: charlie@sterlingdigital.example)"


# ---------------------------------------------------------------------------
# Database — SQLite, replaces the Google Sheet as the source of truth
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_name   TEXT NOT NULL,
    niche           TEXT,
    city            TEXT,
    province        TEXT,
    phone           TEXT,
    website         TEXT,
    address         TEXT,
    has_facebook    INTEGER DEFAULT 0,
    facebook_url    TEXT,
    status          TEXT DEFAULT 'new',
    outreach_channel TEXT,
    source_job_id   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(business_name, address) ON CONFLICT IGNORE
);
CREATE INDEX IF NOT EXISTS idx_leads_city ON leads(city);
CREATE INDEX IF NOT EXISTS idx_leads_niche ON leads(niche);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_has_facebook ON leads(has_facebook);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT UNIQUE NOT NULL,
    region_query TEXT,
    city        TEXT,
    niche       TEXT,
    status      TEXT DEFAULT 'pending',
    leads_found INTEGER DEFAULT 0,
    error       TEXT,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT UNIQUE NOT NULL,
    command     TEXT,
    total_jobs  INTEGER DEFAULT 0,
    jobs_done   INTEGER DEFAULT 0,
    jobs_failed INTEGER DEFAULT 0,
    started_at  TEXT,
    finished_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LeadsDB:
    def __init__(self, path: str = DB_PATH):
        self.path = Path(path)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._ensure_extra_columns(conn)

    def _ensure_extra_columns(self, conn):
        """Adds columns needed for full parity with the old Google Sheet
        shape (Call notes, Email, Rating, etc.) to an existing leads.db
        without losing any data already in it. Safe to call every run."""
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

        # Normalize old default 'new' status (from before this app
        # existed) to 'Not Contacted', which is what the Streamlit UI's
        # dropdowns actually expect.
        conn.execute("UPDATE leads SET status = 'Not Contacted' WHERE status = 'new' OR status IS NULL OR status = ''")

    def upsert_lead(self, lead: dict) -> bool:
        """Insert a lead if new. Dedup checks, in order: business_name +
        address (if there's a real address), business_name + phone (if
        there's a real phone), and business_name ALONE as a last resort
        when neither is available - which is true for the vast majority
        of leads in practice, so this last tier is what actually matters
        most. Returns True if inserted, False if it was a duplicate."""
        now = _now()
        name = lead.get("business_name")
        address = (lead.get("address") or "").strip() or None
        phone = (lead.get("phone") or "").strip() or None

        with self._conn() as conn:
            existing = None
            if address:
                existing = conn.execute(
                    "SELECT id FROM leads WHERE business_name = ? COLLATE NOCASE AND address = ? COLLATE NOCASE",
                    (name, address),
                ).fetchone()
            if not existing and phone:
                existing = conn.execute(
                    "SELECT id FROM leads WHERE business_name = ? COLLATE NOCASE AND phone = ? COLLATE NOCASE",
                    (name, phone),
                ).fetchone()
            if not existing and not address and not phone:
                existing = conn.execute(
                    "SELECT id FROM leads WHERE business_name = ? COLLATE NOCASE "
                    "AND (address IS NULL OR TRIM(address) = '') "
                    "AND (phone IS NULL OR TRIM(phone) = '')",
                    (name,),
                ).fetchone()
            if existing:
                return False

            cur = conn.execute(
                """
                INSERT INTO leads (
                    business_name, niche, city, province, phone, website,
                    address, has_facebook, facebook_url, status,
                    source_job_id, rating, review_count, maps_url, search_query,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Not Contacted', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name, lead.get("niche"), lead.get("city"),
                    lead.get("province"), phone, lead.get("website"),
                    address, 1 if lead.get("has_facebook") else 0,
                    lead.get("facebook_url"), lead.get("source_job_id"),
                    lead.get("rating"), lead.get("review_count"),
                    lead.get("maps_url"), lead.get("search_query"),
                    now, now,
                ),
            )
            return cur.rowcount > 0

    def mark_status(self, business_name: str, city: str, status: str, outreach_channel: str = None):
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE leads SET status = ?, outreach_channel = COALESCE(?, outreach_channel), updated_at = ?
                WHERE business_name = ? COLLATE NOCASE AND city = ? COLLATE NOCASE
                """,
                (status, outreach_channel, _now(), business_name, city),
            )

    def get_leads(self, city=None, niche=None, has_facebook=None, status=None):
        query = "SELECT * FROM leads WHERE 1=1"
        params = []
        if city:
            query += " AND city = ? COLLATE NOCASE"; params.append(city)
        if niche:
            query += " AND niche = ? COLLATE NOCASE"; params.append(niche)
        if has_facebook is not None:
            query += " AND has_facebook = ?"; params.append(1 if has_facebook else 0)
        if status:
            query += " AND status = ?"; params.append(status)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def export_csv(self, out_path: str, **filters):
        import csv
        rows = self.get_leads(**filters)
        if not rows:
            Path(out_path).write_text("")
            return 0
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    def queue_jobs(self, run_id: str, region_query: str, job_list: list):
        now = _now()
        with self._conn() as conn:
            for city, niche in job_list:
                job_id = f"{city}__{niche}__{run_id}".replace(" ", "-").lower()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs (job_id, region_query, city, niche, status, started_at)
                    VALUES (?, ?, ?, ?, 'pending', ?)
                    """,
                    (job_id, region_query, city, niche, now),
                )
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, command, total_jobs, started_at) VALUES (?, ?, ?, ?)",
                (run_id, region_query, len(job_list), now),
            )

    def next_pending_job(self):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()
            return dict(row) if row else None

    def mark_job(self, job_id: str, status: str, leads_found: int = 0, error: str = None):
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, leads_found = ?, error = ?, finished_at = ? WHERE job_id = ?",
                (status, leads_found, error, _now(), job_id),
            )

    def run_progress(self, run_id: str):
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                       SUM(leads_found) as total_leads
                FROM jobs WHERE job_id LIKE '%' || ? || '%'
                """,
                (run_id,),
            ).fetchone()
            return dict(row)


# ---------------------------------------------------------------------------
# Geocoding — region name -> real list of cities/towns (free OSM APIs)
# ---------------------------------------------------------------------------

def get_area_id(region_name: str) -> dict:
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": region_name, "format": "json", "limit": 1, "polygon_geojson": 0},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not find region: {region_name!r}")

    r = results[0]
    osm_id = int(r["osm_id"])
    osm_type = r["osm_type"]

    if osm_type == "relation":
        area_id = 3600000000 + osm_id
    elif osm_type == "way":
        area_id = 2400000000 + osm_id
    else:
        area_id = None

    return {"display_name": r["display_name"], "area_id": area_id, "bbox": r.get("boundingbox")}


def get_cities_in_region(region_name: str, min_population: int = MIN_CITY_POPULATION) -> list:
    area = get_area_id(region_name)

    if area["area_id"]:
        area_clause = f'area({area["area_id"]})->.searchArea;'
        scope = "(area.searchArea)"
    else:
        south, north, west, east = area["bbox"]
        area_clause = ""
        scope = f"({south},{west},{north},{east})"

    query = f"""
    [out:json][timeout:60];
    {area_clause}
    (
      node["place"~"^(city|town)$"]{scope};
    );
    out body;
    """

    time.sleep(1)  # be polite to the free Overpass endpoints
    last_error = None
    for mirror_url in OVERPASS_URLS:
        try:
            resp = requests.post(
                mirror_url,
                data={"data": query},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=90,
            )
            if resp.status_code != 200:
                last_error = f"{mirror_url} returned {resp.status_code}: {resp.text[:200]}"
                continue
            data = resp.json()
            break
        except requests.RequestException as e:
            last_error = f"{mirror_url} failed: {e}"
            continue
    else:
        raise RuntimeError(f"All Overpass mirrors failed. Last error: {last_error}")

    cities, seen = [], set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name or name.lower() in seen:
            continue
        pop_raw = tags.get("population")
        try:
            pop = int(pop_raw) if pop_raw else None
        except ValueError:
            pop = None
        if pop is not None and pop < min_population:
            continue
        cities.append({"name": name, "population": pop})
        seen.add(name.lower())

    cities.sort(key=lambda c: (c["population"] or 0), reverse=True)
    return cities


# ---------------------------------------------------------------------------
# Command parsing — "New Brunswick, dentists + plumbers" -> structured
# ---------------------------------------------------------------------------

def parse_command(command: str) -> dict:
    if not command or not command.strip():
        raise ValueError("Empty command")

    parts = re.split(r"[,:]", command.strip(), maxsplit=1)
    if len(parts) != 2:
        raise ValueError(
            f"Couldn't find a region/niche split in: {command!r}. "
            "Expected format: 'Region, niche1 + niche2'"
        )

    region = parts[0].strip()
    niche_blob = parts[1].strip()
    raw_niches = re.split(r"\s*\+\s*|\s*,\s*|\s+and\s+", niche_blob)
    niches = [n.strip() for n in raw_niches if n.strip()]

    if not region:
        raise ValueError("No region found in command")
    if not niches:
        raise ValueError("No niches found in command")

    return {"region": region, "niches": niches}


# ---------------------------------------------------------------------------
# Lead mapping — scraper.run_scrape() output -> db row
# ---------------------------------------------------------------------------

def scraper_lead_to_db_row(lead: dict, city: str, province: str, niche: str, job_id: str) -> dict:
    return {
        "business_name": lead.get("Name") or "",
        "niche": niche,
        "city": city,
        "province": province,
        "phone": lead.get("Phone") or None,
        "website": None,
        "address": lead.get("Adress") or None,
        "has_facebook": (lead.get("Has facebook") == "True"),
        "facebook_url": lead.get("Facebook URL") or None,
        "source_job_id": job_id,
        "rating": lead.get("Rating") or None,
        "review_count": lead.get("Reveiw count") or None,
        "maps_url": lead.get("Maps URL") or None,
        "search_query": lead.get("Search query") or None,
    }


# ---------------------------------------------------------------------------
# The overnight agent itself
# ---------------------------------------------------------------------------

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")
    with open("agent_run.log", "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def build_run_id(region: str) -> str:
    safe_region = region.lower().replace(" ", "-")
    return f"{safe_region}__{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def run(command: str, max_leads: int = None):
    db = LeadsDB(DB_PATH)
    db.init_schema()

    log(f"Parsing command: {command!r}")
    parsed = parse_command(command)
    region, niches = parsed["region"], parsed["niches"]
    log(f"Region: {region} | Niches: {niches}")
    if max_leads:
        log(f"Lead cap: stopping after {max_leads} new leads this run")

    log(f"Looking up cities/towns in {region} (population >= {MIN_CITY_POPULATION})...")
    try:
        cities = get_cities_in_region(region)
    except Exception as e:
        log(f"FATAL: geocoding failed for {region!r}: {e}")
        sys.exit(1)

    if not cities:
        log(f"No cities found for {region!r} above the population threshold. Nothing to do.")
        sys.exit(1)

    city_names = [c["name"] for c in cities]
    log(f"Found {len(city_names)} cities: {', '.join(city_names[:10])}"
        f"{' ...' if len(city_names) > 10 else ''}")

    job_list = [(city, niche) for city in city_names for niche in niches]
    run_id = build_run_id(region)
    db.queue_jobs(run_id, region, job_list)
    log(f"Queued {len(job_list)} jobs (run_id={run_id})")

    total_leads_this_run = 0
    job = db.next_pending_job()
    job_num = 0
    total_jobs = len(job_list)

    while job:
        job_num += 1
        city, niche, job_id = job["city"], job["niche"], job["job_id"]
        log(f"[{job_num}/{total_jobs}] Running: {niche!r} in {city!r} ...")

        attempt = 0
        leads_found_this_job = 0
        success = False
        last_error = None

        while attempt <= MAX_RETRIES_PER_JOB and not success:
            attempt += 1
            try:
                results = run_scrape(niche, city)
                for lead in results:
                    row = scraper_lead_to_db_row(lead, city=city, province=region, niche=niche, job_id=job_id)
                    if db.upsert_lead(row):
                        leads_found_this_job += 1
                success = True
            except Exception as e:
                last_error = str(e)
                log(f"  attempt {attempt} failed: {e}")
                if attempt <= MAX_RETRIES_PER_JOB:
                    time.sleep(30)

        if success:
            db.mark_job(job_id, "done", leads_found=leads_found_this_job)
            total_leads_this_run += leads_found_this_job
            log(f"  -> done: {leads_found_this_job} new leads (deduped)")
        else:
            db.mark_job(job_id, "failed", leads_found=0, error=last_error)
            log(f"  -> FAILED after {MAX_RETRIES_PER_JOB + 1} attempts: {last_error}")

        if max_leads and total_leads_this_run >= max_leads:
            log(f"Hit lead cap ({total_leads_this_run}/{max_leads}) — stopping here. "
                f"Remaining jobs stay 'pending' - rerun the same command anytime to pick up where this left off.")
            break

        job = db.next_pending_job()

    progress = db.run_progress(run_id)
    log(
        f"RUN COMPLETE — {progress['done']} done, {progress['failed']} failed "
        f"out of {progress['total']} jobs. {total_leads_this_run} new leads added this run."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overnight lead-gen agent")
    parser.add_argument("command", help='e.g. "New Brunswick, dentists + plumbers + HVAC"')
    parser.add_argument(
        "--max-leads", type=int, default=None,
        help="Stop after finding this many new leads (across all jobs this run). "
             "Remaining jobs stay queued - rerun the same command later to continue.",
    )
    args = parser.parse_args()

    try:
        run(args.command, max_leads=args.max_leads)
    except KeyboardInterrupt:
        log("Interrupted by user. Progress is saved — rerun the same command to resume.")
    except Exception:
        log(f"UNEXPECTED ERROR:\n{traceback.format_exc()}")
        sys.exit(1)
