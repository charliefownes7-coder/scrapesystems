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
import queue
import sqlite3
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from db_store import DB_PATH, REQUIRED_COLUMNS, load_leads, save_leads, _ensure_schema
from scraper import scrape_maps, check_facebook_pages
from swipe_launcher import router as swipe_launcher_router
from agent import get_cities_in_region


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Makes sure leads.db has the leads table (and every expected column)
    before any request comes in. Without this, a brand-new install with
    no leads.db yet — i.e. every first-time user, before their first
    scrape ever saves anything — gets a 500 error the moment the
    dashboard tries to load the (empty) leads list, since the table
    itself doesn't exist until db_store.py's own load_leads()/save_leads()
    happen to run it first.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()
    yield


app = FastAPI(title="ScrapeSystems Local Agent", lifespan=lifespan)

TOKEN_FILE = Path.home() / ".scrapesystems" / "token.json"

# A realistic "typical" scroll count for turning scroll progress into a
# percentage — same number app.py already uses, kept identical so the
# website's progress bar behaves the same way the Streamlit one does.
TYPICAL_SCROLL_ESTIMATE = 40


def free_port(port: int):
    """
    Kill any process already listening on this port before we try to
    bind — prevents the '[Errno 98] address already in use' crash that
    happens when a previous run's process didn't get cleaned up.
    """
    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True, text=True,
        )
        return
    except FileNotFoundError:
        pass

    # fuser not available — fall back to a netstat-based lookup.
    try:
        out = subprocess.check_output(["netstat", "-tulpn"], text=True)
        for line in out.splitlines():
            if f":{port} " in line and "LISTEN" in line:
                pid = line.split()[-1].split("/")[0]
                if pid.isdigit():
                    subprocess.run(["kill", "-9", pid])
    except Exception:
        # Best-effort only — if neither tool is available, just let
        # uvicorn's bind attempt fail with its normal error as before.
        pass


def save_token(token: str, email: Optional[str] = None):
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
    # Add your published (non-preview) Lovable URL here once you
    # publish the app — CORS silently blocks requests from any origin
    # not in this list, which would make a real client's browser
    # look like "agent not connected" even when it's running fine.
    # Example: "https://leads-finder-dash.lovable.app"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(swipe_launcher_router)


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
def auth_callback(token: str = Query(...), email: Optional[str] = Query(None)):
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

# Multiple jobs can now run at once, in memory, each tracked by its
# own job_id. Good enough for a single-user local agent — if the
# process restarts, job history resets, which is fine since finished
# results are already saved into leads.db by then.
_job_lock = threading.Lock()
_jobs: Dict[str, dict] = {}
_stop_events: Dict[str, threading.Event] = {}

# Set for a job_id between a hard "Stop" request and that job actually
# reaching its own safe-stop point. Read (and cleared) exactly where
# _run_job decides what stopping means for that job: present -> throw
# the resume checkpoint away and finalize as "ended" (see /run-scrape/end);
# absent -> normal pause behavior, same as always (finalize as "stopped",
# keep the checkpoint so /run-scrape/continue can pick it back up).
_end_requested: Dict[str, bool] = {}


class ScrapeRequest(BaseModel):
    niche: str
    location: str


def _run_job(job_id: str, niche: str, location: str, resume: Optional[dict] = None):
    stop_event = _stop_events[job_id]

    # If this run is resuming a Maps-stage pause, we know roughly how
    # many cards had already loaded last time (cards_found_at_pause)
    # and exactly what the progress bar looked like right before the
    # pause (progress_snapshot). Re-scraping always has to restart the
    # Maps scroll from 0 (scroll position can't survive a pause) — but
    # rather than show that as a visible reset, we freeze the display
    # at the pre-pause snapshot ("starting") until the new scroll
    # catches back up to (or gives up trying to reach) the old count,
    # so it just looks like the scrape picked back up instead of
    # restarting.
    _maps_resume = resume if (resume and resume.get("stage") == "maps") else None
    _catchup_target = _maps_resume.get("cards_found_at_pause") if _maps_resume else None
    _catchup_snapshot = _maps_resume.get("progress_snapshot") if _maps_resume else None
    _catchup_state = {"stalled_count": 0, "last_cards_found": -1}

    # A run that isn't resuming, or has no target/snapshot to work
    # with, never enters "catching up" mode at all.
    if not _catchup_target or not _catchup_snapshot:
        _catchup_target = None

    def update(phase, current, total, message):
        with _job_lock:
            if job_id in _jobs:
                _jobs[job_id].update(
                    phase=phase, current=current, total=total, message=message
                )

    def on_scroll_progress(current, total, cards_found):
        nonlocal _catchup_target

        with _job_lock:
            if job_id in _jobs:
                _jobs[job_id]["cards_found"] = cards_found

        if _catchup_target is not None:
            if cards_found >= _catchup_target:
                # Caught back up to (or past) where the last attempt
                # left off — switch over to normal live reporting from
                # here on, same as a fresh run would show.
                _catchup_target = None
            else:
                # Still behind — but if the card count has genuinely
                # stopped growing for a few scroll steps in a row (e.g.
                # this search now returns fewer live results than it
                # did before the pause), waiting for it to reach the
                # old count would freeze the display for nothing. Give
                # up the freeze early in that case and fall through to
                # normal reporting so the bar reflects reality instead
                # of sitting stuck at "starting" indefinitely.
                if cards_found == _catchup_state["last_cards_found"]:
                    _catchup_state["stalled_count"] += 1
                else:
                    _catchup_state["stalled_count"] = 0
                    _catchup_state["last_cards_found"] = cards_found

                if _catchup_state["stalled_count"] >= 4:
                    _catchup_target = None
                else:
                    with _job_lock:
                        if job_id in _jobs:
                            _jobs[job_id].update(
                                phase="starting",
                                current=_catchup_snapshot["current"],
                                total=_catchup_snapshot["total"],
                                message=_catchup_snapshot["message"],
                            )
                    return

        pct = min(int(current / TYPICAL_SCROLL_ESTIMATE * 100), 95)
        update("scraping_maps", pct, 100, f"Scraping Google Maps... ({cards_found} cards found so far)")

    def on_progress(current, total, name):
        pct = int(current / total * 100) if total else 0
        business_number = min(int(current) + 1, total) if total else 0
        with _job_lock:
            if job_id in _jobs:
                _jobs[job_id]["businesses_checked"] = business_number
                _jobs[job_id]["businesses_total"] = total
        update("checking_facebook", pct, 100, f"Checking Facebook pages... ({business_number}/{total}) — {name}")

    def _lead_to_df(lead: dict) -> pd.DataFrame:
        # Same shaping main.py always did before saving — just now
        # applied to one lead at a time instead of the whole batch.
        df = pd.DataFrame([lead])
        df["Call status"] = "Not Contacted"
        df["Call notes"] = ""
        for col in REQUIRED_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[REQUIRED_COLUMNS]

    def on_lead_found(lead: dict):
        # Saved the instant this one lead is ready, so it shows up in
        # the website's leads table and count right away — not only
        # after the whole scrape finishes.
        save_leads(_lead_to_df(lead))
        with _job_lock:
            if job_id in _jobs:
                _jobs[job_id]["leads_found"] = _jobs[job_id].get("leads_found") or 0
                _jobs[job_id]["leads_found"] += 1

    try:
        if resume and resume.get("stage") == "facebook":
            # Picking back up exactly where a previous stop left off -
            # skip the Maps stage entirely, re-check nothing already
            # checked.
            leads, remaining = check_facebook_pages(
                resume["businesses"], location,
                on_progress=on_progress, on_lead_found=on_lead_found,
                should_stop=stop_event.is_set,
                start_index=resume["start_index"], total_count=resume["total_count"],
            )
        else:
            # Fresh run, OR resuming after a stop that happened during
            # the Maps stage - either way the Maps stage has to run (or
            # re-run) since scroll position can't survive a stop.
            businesses = scrape_maps(
                niche, location, on_scroll_progress=on_scroll_progress, should_stop=stop_event.is_set,
            )
            if stop_event.is_set():
                with _job_lock:
                    if job_id in _jobs:
                        cards = _jobs[job_id].get("cards_found", 0)
                        if _end_requested.pop(job_id, False):
                            found = _jobs[job_id].get("leads_found") or 0
                            _jobs[job_id].pop("_resume", None)
                            _jobs[job_id].update(
                                phase="ended",
                                message=f"Stopped — {found} lead(s) saved. This run won't be resumed.",
                            )
                        else:
                            # Snapshot exactly what the progress bar showed
                            # right before pausing (current/total/message) so
                            # a later resume can freeze the display at this
                            # same spot instead of visibly dropping back to
                            # 0% while the Maps stage re-scrolls from
                            # scratch — see the "catching up" logic in
                            # on_scroll_progress below.
                            progress_snapshot = {
                                "current": _jobs[job_id].get("current", 0),
                                "total": _jobs[job_id].get("total", 100),
                                "message": _jobs[job_id].get("message", "Starting..."),
                            }
                            _jobs[job_id].update(
                                phase="stopped",
                                message=f"Stopped while scraping Google Maps — {cards} businesses found so far, none checked for Facebook yet.",
                            )
                            _jobs[job_id]["_resume"] = {
                                "stage": "maps", "niche": niche, "location": location,
                                "cards_found_at_pause": cards,
                                "progress_snapshot": progress_snapshot,
                            }
                return
            leads, remaining = check_facebook_pages(
                businesses, location,
                on_progress=on_progress, on_lead_found=on_lead_found, should_stop=stop_event.is_set,
            )

        with _job_lock:
            if job_id in _jobs:
                if remaining:
                    found = _jobs[job_id].get("leads_found") or 0
                    checked = _jobs[job_id].get("businesses_checked") or 0
                    total_b = _jobs[job_id].get("businesses_total") or (checked + len(remaining))
                    if _end_requested.pop(job_id, False):
                        _jobs[job_id].pop("_resume", None)
                        _jobs[job_id].update(
                            phase="ended",
                            message=f"Stopped — {found} lead(s) saved. This run won't be resumed.",
                        )
                    else:
                        _jobs[job_id].update(
                            phase="stopped",
                            message=f"Stopped after checking {checked}/{total_b} businesses — {found} leads found so far",
                        )
                        _jobs[job_id]["_resume"] = {
                            "stage": "facebook",
                            "businesses": remaining,
                            "location": location,
                            "start_index": checked + 1,
                            "total_count": total_b,
                        }
                else:
                    _jobs[job_id].pop("_resume", None)
                    _end_requested.pop(job_id, None)
                    _jobs[job_id].update(
                        phase="done", current=100, total=100,
                        message=f"Done — {len(leads)} leads found",
                    )
    except Exception as e:
        with _job_lock:
            if job_id in _jobs:
                _jobs[job_id].update(phase="error", message=str(e))


_scrape_queue: "queue.Queue[str]" = queue.Queue()
_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def _scrape_worker():
    """
    The ONE thread that ever actually runs a scrape. Pulls job_ids off
    _scrape_queue strictly in the order they were put there - queue.Queue
    is genuinely FIFO, so unlike the old semaphore-race approach, there's
    no thread-scheduling luck involved in deciding which job goes next.
    Submission order == execution order, guaranteed.
    """
    while True:
        job_id = _scrape_queue.get()
        try:
            with _job_lock:
                job = _jobs.get(job_id)
                if job is None:
                    continue
                niche = job["niche"]
                location = job["location"]
                resume = job.pop("_pending_resume", None)
                job.update(phase="starting", message="Starting...")
            _run_job(job_id, niche, location, resume=resume)
        except Exception as e:
            with _job_lock:
                if job_id in _jobs:
                    _jobs[job_id].update(phase="error", message=str(e))
        finally:
            _scrape_queue.task_done()


def _ensure_worker():
    """Starts the single background worker the first time it's needed.
    Safe to call on every request - only actually spawns a thread once
    (or again if a previous one somehow died)."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_scrape_worker, daemon=True)
            _worker_thread.start()


@app.post("/run-scrape")
def start_scrape(req: ScrapeRequest):
    """
    Adds a new scrape job to the FIFO queue and returns immediately.
    Jobs run one at a time, strictly in the order they were submitted -
    see _scrape_worker(), the single background thread that actually
    executes them, so calling this repeatedly (e.g. once per queued
    niche+location pair from the multiscrape UI) queues them up in
    submission order instead of racing to start concurrently.

    Idempotent for the same (niche, location) pair: if a job for that
    exact combo is already queued or actively running, this returns
    that existing job instead of creating a second one behind it. This
    guards against a duplicate submission from the frontend (e.g. a
    double click, or a page load that fires the same start-scrape call
    a navigation already fired) silently queueing the same scrape
    twice — which otherwise shows up as the dashboard tracking the
    wrong (phantom, still-queued) job while the real one runs
    invisibly underneath it. Deliberately does NOT match against a
    paused ("stopped") job with the same niche+location — that's a
    legitimate case where someone might want to start a fresh run
    rather than be silently redirected back to the old paused one.
    """
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    ACTIVE_PHASES = ("queued", "starting", "scraping_maps", "checking_facebook")

    with _job_lock:
        for job in _jobs.values():
            if (
                job.get("niche") == req.niche
                and job.get("location") == req.location
                and job.get("phase") in ACTIVE_PHASES
            ):
                return {"job_id": job["job_id"], "status": "already_running"}

        job_id = str(uuid.uuid4())
        _stop_events[job_id] = threading.Event()
        _jobs[job_id] = {
            "job_id": job_id, "niche": req.niche, "location": req.location,
            "phase": "queued", "current": 0, "total": 100,
            "message": "Queued — waiting for the current scrape to finish",
            "leads_found": 0,
        }

    _ensure_worker()
    _scrape_queue.put(job_id)
    return {"job_id": job_id, "status": "started"}


def _job_view(job: dict) -> dict:
    view = dict(job)
    view["canContinue"] = "_resume" in view
    view.pop("_resume", None)
    view.pop("_pending_resume", None)
    return view


@app.get("/jobs")
def list_jobs():
    """
    Returns every job currently tracked (running, stopped, done, or
    errored), newest first. Used by the multiscrape UI to show live
    progress for several concurrent scrapes at once.
    """
    with _job_lock:
        jobs = [_job_view(job) for job in _jobs.values()]
    return {"jobs": jobs}


@app.get("/run-scrape/status")
def scrape_status(
    job_id: Optional[str] = Query(None),
    niche: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
):
    """
    Single-job status lookup, in order of specificity:

    1. job_id given -> that exact job (unchanged from before).
    2. No job_id, but niche+location given -> the most recently
       created job matching that exact (niche, location) pair. This is
       what lets a specific dashboard/tab track the specific scrape it
       cares about, instead of the old "whichever job was started most
       recently" guess -- that guess breaks the moment more than one
       niche+location is being tracked at once (multiple tabs, a
       region scrape running alongside a manual one, or a paused job
       from an earlier scrape still lingering in memory), since it has
       no way to tell those apart and can end up reporting on a
       completely different job than the one this page is showing.
    3. Neither given -> the single most-recently-started job overall,
       same fallback as always, kept for backward compatibility with
       any caller that doesn't yet pass niche+location.
    """
    with _job_lock:
        if job_id is None:
            if niche is not None or location is not None:
                matches = [
                    j for j in _jobs.values()
                    if j.get("niche") == niche and j.get("location") == location
                ]
                if not matches:
                    return {"phase": "idle"}
                return _job_view(matches[-1])  # most recently created match
            if not _jobs:
                return {"phase": "idle"}
            job_id = next(reversed(_jobs))
        job = _jobs.get(job_id)
        if job is None:
            return {"phase": "idle"}
        return _job_view(job)


@app.post("/run-scrape/stop")
def stop_scrape(job_id: Optional[str] = Query(None)):
    """
    Requests a running scrape to stop as soon as it safely can —
    between scroll steps or between Facebook lookups, not instantly,
    since a scrape is mid-way through Playwright/network calls that
    can't just be killed cleanly. Every lead found before the stop
    lands is already saved (see on_lead_found in _run_job), so nothing
    found so far is lost. If job_id isn't given, stops the
    most-recently-started job.
    """
    with _job_lock:
        if job_id is None:
            if not _jobs:
                raise HTTPException(status_code=409, detail="No scrape is currently running.")
            job_id = next(reversed(_jobs))
        job = _jobs.get(job_id)
        if job is None or job["phase"] in ("done", "error", "stopped", "ended", "idle"):
            raise HTTPException(status_code=409, detail="No scrape is currently running.")
        _stop_events[job_id].set()
    return {"status": "stopping", "job_id": job_id}


@app.post("/run-scrape/continue")
def continue_scrape(job_id: Optional[str] = Query(None)):
    """
    Resumes a stopped scrape from its saved checkpoint. If it stopped
    mid-Facebook-check, picks back up at the exact next unchecked
    business (nothing re-checked). If it stopped during the Maps
    stage, re-runs that stage from scratch — scroll position doesn't
    survive a stop — but this is harmless: dedup means re-finding the
    same businesses never creates duplicate leads. If job_id isn't
    given, resumes the most-recently-started job.
    """
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    with _job_lock:
        if job_id is None:
            if not _jobs:
                raise HTTPException(status_code=409, detail="No stopped scrape to continue.")
            job_id = next(reversed(_jobs))
        job = _jobs.get(job_id)
        if job is None or job["phase"] != "stopped":
            raise HTTPException(status_code=409, detail="No stopped scrape to continue.")
        resume = job.get("_resume")
        if not resume:
            raise HTTPException(status_code=409, detail="Nothing to continue — start a new scrape instead.")

        _stop_events[job_id].clear()
        # location can differ from job["location"] when resuming
        # mid-facebook-check (see resume dict shape above) - fold it
        # back into the job now so the worker picks up the right one.
        job["location"] = resume.get("location", job["location"])
        job["_pending_resume"] = resume
        job.pop("_resume", None)
        job.update(
            phase="queued",
            message="Queued to resume — waiting for the current scrape to finish",
        )

    _ensure_worker()
    _scrape_queue.put(job_id)
    return {"job_id": job_id, "status": "resumed"}


@app.post("/run-scrape/end")
def end_scrape(job_id: Optional[str] = Query(None)):
    """
    Hard-stops a job for good — unlike /run-scrape/stop (Pause), this
    throws away its resume checkpoint so /run-scrape/continue can never
    pick it back up. Every lead found before this point is already
    saved (see on_lead_found in _run_job), so nothing is lost — this
    only decides whether the run itself can be resumed later.

    Works whether the job is currently RUNNING or QUEUED (flags it via
    _end_requested and sets the stop event, same trigger as a pause —
    _run_job checks that flag exactly where it'd normally set up a
    resume checkpoint, and skips doing so) or already PAUSED/stopped
    (finalizes it immediately, since there's no running thread left to
    ever reach that check). If job_id isn't given, ends the
    most-recently-started job.
    """
    with _job_lock:
        if job_id is None:
            if not _jobs:
                raise HTTPException(status_code=409, detail="No scrape to stop.")
            job_id = next(reversed(_jobs))
        job = _jobs.get(job_id)
        if job is None or job["phase"] in ("done", "error", "ended", "idle"):
            raise HTTPException(status_code=409, detail="No active or paused scrape to stop.")

        if job["phase"] == "stopped":
            # Already paused - no running thread left to catch the
            # flag, so finalize it right here instead.
            found = job.get("leads_found") or 0
            job.pop("_resume", None)
            _end_requested.pop(job_id, None)
            job.update(
                phase="ended",
                message=f"Stopped — {found} lead(s) saved. This run won't be resumed.",
            )
        else:
            _end_requested[job_id] = True
            _stop_events[job_id].set()

    return {"status": "stopping", "job_id": job_id}


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """
    Removes a finished job from memory entirely — called by the
    frontend right after a Stop is confirmed and the job reaches
    "ended", so it disappears from the list instead of lingering.
    Only allowed once a job has actually reached a final state (done,
    error, or ended) — refuses to remove anything still running or
    paused, since that would orphan its stop_event/end_requested
    bookkeeping while a background thread might still reference it.
    """
    with _job_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
        if job["phase"] not in ("done", "error", "ended"):
            raise HTTPException(status_code=409, detail="Can't remove a job that's still running or paused.")
        _jobs.pop(job_id, None)
        _stop_events.pop(job_id, None)
        _end_requested.pop(job_id, None)
    return {"status": "deleted", "job_id": job_id}


# ---------------------------------------------------------------------------
# Region scrape — one region -> many cities (via agent.py's free OSM
# geocoding). Cities are fed into the SAME single FIFO queue that
# normal /run-scrape jobs use (see _scrape_queue / _scrape_worker
# above) — there is only ever one scrape running at a time, region or
# not, so this no longer needs its own separate concurrency
# mechanism. This also closes the old gap where pausing then
# continuing a region city could re-enter through the FIFO worker
# while the region's own semaphore-driven pool kept running a
# different city in parallel — now both paths are the same path.
# ---------------------------------------------------------------------------


class RegionScrapeRequest(BaseModel):
    niche: str
    region: str


@app.post("/run-region-scrape")
def start_region_scrape(req: RegionScrapeRequest):
    """
    Looks up every city/town inside the given region (free OSM
    geocoding — see agent.py), then queues one scrape job per city
    into the same FIFO queue every other scrape uses, so cities run
    strictly one at a time, in order, alongside (not competing with)
    any other scrapes already queued. Returns immediately with every
    job_id created so the frontend can poll GET /jobs and filter by
    these ids for live per-city progress.
    """
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    try:
        cities = get_cities_in_region(req.region)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not look up cities for {req.region!r}: {e}")

    if not cities:
        raise HTTPException(status_code=404, detail=f"No cities found for {req.region!r}.")

    region_run_id = str(uuid.uuid4())
    job_ids = []

    with _job_lock:
        for city in cities:
            job_id = str(uuid.uuid4())
            _stop_events[job_id] = threading.Event()
            _jobs[job_id] = {
                "job_id": job_id, "niche": req.niche, "location": city["name"],
                "phase": "queued", "current": 0, "total": 100,
                "message": "Queued — waiting for the current scrape to finish",
                "leads_found": 0, "region_run_id": region_run_id,
            }
            job_ids.append(job_id)

    _ensure_worker()
    for job_id in job_ids:
        _scrape_queue.put(job_id)

    return {
        "region_run_id": region_run_id, "region": req.region,
        "cities_found": len(cities), "job_ids": job_ids,
    }


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
            # "screened" is added by lead_review.py the first time a
            # swipe session runs (ALTER TABLE) — guard against it not
            # existing yet on a fresh install that hasn't used that
            # feature. "good" | "bad" | None.
            "screened": row["screened"] if "screened" in row.keys() and row["screened"] else None,
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

    free_port(8765)
    uvicorn.run(app, host="127.0.0.1", port=8765)
