"""
ScrapeSystems local agent — Phase 1 + 3 + 4

- /ping tells the website "I'm installed and running"
- /auth-callback receives a login token from the website and saves it
- /status tells the website "I'm installed AND logged in as this user"
- /run-scrape starts a real scrape as a background job (a scrape takes
  minutes, not seconds — see scraper.py's deliberate pacing — so this
  returns immediately with a job id rather than blocking the request)
- /run-scrape/status reports live progress on the current/last job,
  reusing the exact same progress shape the Streamlit app already uses
"""

import json
import sqlite3
import threading
import uuid
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from db_store import DB_PATH, REQUIRED_COLUMNS, load_leads, save_leads
from scraper import run_scrape

app = FastAPI(title="ScrapeSystems Local Agent")

TOKEN_FILE = Path.home() / ".scrapesystems" / "token.json"

# A realistic "typical" scroll count for turning scroll progress into a
# percentage — same number app.py already uses, kept identical so the
# website's progress bar behaves the same way the Streamlit one does.
TYPICAL_SCROLL_ESTIMATE = 40


def save_token(token: str, email: str | None = None):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"token": token, "email": email}))


def load_token():
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# IMPORTANT: replace with your real Lovable domain(s). Keep localhost
# origins for local testing.
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://preview--leads-finder-dash.lovable.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/ping")
def ping():
    return {"status": "ok", "service": "scrapesystems-agent", "version": "0.2.0"}


@app.get("/status")
def status():
    saved = load_token()
    if saved is None:
        return {"status": "ok", "authenticated": False}
    return {"status": "ok", "authenticated": True, "email": saved.get("email")}


@app.get("/auth-callback", response_class=HTMLResponse)
def auth_callback(token: str = Query(...), email: str | None = Query(None)):
    save_token(token, email)
    display_email = email or "your account"
    return f"""
    <html>
      <body style="font-family: sans-serif; text-align: center; padding-top: 80px;">
        <h2>✅ Connected as {display_email}</h2>
        <p>You can close this tab and go back to ScrapeSystems.</p>
      </body>
    </html>
    """


# ---------------------------------------------------------------------------
# Phase 4 — real scrape jobs
# ---------------------------------------------------------------------------

# One job at a time, in memory. Good enough for a single-user local
# agent — if the process restarts, job history resets, which is fine
# since finished results are already saved into leads.db by then.
_job_lock = threading.Lock()
_current_job: dict | None = None


class ScrapeRequest(BaseModel):
    niche: str
    location: str


def _run_job(job_id: str, niche: str, location: str):
    global _current_job

    def update(phase, current, total, message):
        with _job_lock:
            if _current_job and _current_job["job_id"] == job_id:
                _current_job.update(
                    phase=phase, current=current, total=total, message=message
                )

    def on_scroll_progress(current, total, cards_found):
        pct = min(int(current / TYPICAL_SCROLL_ESTIMATE * 100), 95)
        update("scraping_maps", pct, 100, f"Scraping Google Maps... ({cards_found} cards found so far)")

    def on_progress(current, total, name):
        pct = int(current / total * 100) if total else 0
        lead_number = min(int(current) + 1, total) if total else 0
        update("checking_facebook", pct, 100, f"Checking Facebook pages... ({lead_number}/{total}) — {name}")

    try:
        leads = run_scrape(
            niche, location,
            on_progress=on_progress,
            on_scroll_progress=on_scroll_progress,
        )

        # Save into leads.db the same way the Streamlit app does, so
        # this shares the exact same local database — nothing about
        # how results are stored changes because the trigger came from
        # the website instead of the Streamlit UI.
        df = pd.DataFrame(leads)
        df["Call status"] = "Not Contacted"
        df["Call notes"] = ""
        for col in REQUIRED_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[REQUIRED_COLUMNS]
        save_leads(df)

        with _job_lock:
            if _current_job and _current_job["job_id"] == job_id:
                _current_job.update(
                    phase="done", current=100, total=100,
                    message=f"Done — {len(leads)} leads found",
                    leads_found=len(leads),
                )
    except Exception as e:
        with _job_lock:
            if _current_job and _current_job["job_id"] == job_id:
                _current_job.update(phase="error", message=str(e))


@app.post("/run-scrape")
def start_scrape(req: ScrapeRequest):
    global _current_job

    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    with _job_lock:
        if _current_job and _current_job["phase"] not in ("done", "error"):
            raise HTTPException(status_code=409, detail="A scrape is already running.")

        job_id = str(uuid.uuid4())
        _current_job = {
            "job_id": job_id, "niche": req.niche, "location": req.location,
            "phase": "starting", "current": 0, "total": 100,
            "message": "Starting...", "leads_found": None,
        }

    thread = threading.Thread(target=_run_job, args=(job_id, req.niche, req.location), daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "started"}


@app.get("/run-scrape/status")
def scrape_status():
    with _job_lock:
        if _current_job is None:
            return {"phase": "idle"}
        return dict(_current_job)


@app.get("/leads")
def get_leads():
    """
    Returns every lead currently in leads.db, same data the Streamlit
    app shows, PLUS each row's real database id — needed so the
    website can tell the PATCH endpoint below exactly which row to
    update when someone changes a status in the table.
    """
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM leads ORDER BY id").fetchall()
    conn.close()

    leads = [
        {
            "id": row["id"],
            "name": row["business_name"] or "",
            "category": row["niche"] or "",
            "address": row["address"] or "",
            "phone": row["phone"] or "",
            "callStatus": row["status"] or "Not Contacted",
            "callNotes": row["call_notes"] or "",
            "textPhoneNumber": row["text_phone_number"] or "",
            "email": row["email"] or "",
            "rating": row["rating"] or "",
            "reviewCount": row["review_count"] or "",
            "hasWebsite": bool(row["website"]),
            "hasFacebook": bool(row["has_facebook"]),
            "facebookUrl": row["facebook_url"] or "",
            "moveToColdCall": bool(row["move_to_cold_call"]),
            "mapsUrl": row["maps_url"] or "",
            "searchQuery": row["search_query"] or "",
        }
        for row in rows
    ]
    return {"leads": leads}


CALL_STATUS_OPTIONS = [
    "Not Contacted", "Did not answer", "Not interested",
    "Answered", "Bot answered", "Could not contact", "Has website",
]

DM_STATUS_OPTIONS = [
    "Not Contacted", "DM Sent", "No Response", "Replied - Interested",
    "Not Interested", "Bad Lead", "Bot Answered", "Call Booked", "Has website",
]


@app.get("/status-options")
def status_options():
    """
    The exact dropdown choices for each lead type, straight from the
    real Streamlit app — so the website's dropdown always matches,
    even if these lists change later (add an option there, it shows
    up here automatically, no separate website edit needed).
    """
    return {"callStatusOptions": CALL_STATUS_OPTIONS, "dmStatusOptions": DM_STATUS_OPTIONS}


class StatusUpdate(BaseModel):
    status: str


@app.patch("/leads/{lead_id}/status")
def update_lead_status(lead_id: int, body: StatusUpdate):
    """
    Updates a single lead's status by its real row id — the only
    field the website is allowed to edit directly. Everything else
    about a lead only ever comes from a scrape.
    """
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "UPDATE leads SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (body.status, lead_id),
    )
    conn.commit()
    updated = cur.rowcount
    conn.close()

    if updated == 0:
        raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}")
    return {"id": lead_id, "callStatus": body.status}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
