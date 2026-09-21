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

import itertools
import json
import queue
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from db_store import (
    DB_PATH, REQUIRED_COLUMNS, load_leads, save_leads, _ensure_schema,
    log_scrape_history, get_scrape_history, _next_contacted_at,
)
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


# ---------------------------------------------------------------------------
# Plans & usage
# ---------------------------------------------------------------------------
# IMPORTANT: replace with your real Supabase project values (Project
# Settings -> API). The anon key is safe to ship in the app — it's the
# same public key the website already uses, and Row Level Security on
# the user_plans table (see supabase-schema.sql) is what actually keeps
# one user from reading or changing another user's plan/usage.
SUPABASE_URL = "https://inxymjalyniafhkbvggq.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_EBN5BldgVsK_mf7hDMZnnA_bAGpUT6j"

# Keep these two maps in sync with the same values used on the website
# (pricing page, upgrade prompts) and in supabase-schema.sql's plan
# check constraint — there's no single shared source of truth across
# Python/SQL/the frontend, so a pricing change means updating all three.
PLAN_CAPS = {"free": 500, "pro": 5000, "premium": 50000}
PLAN_FEATURES = {
    "free": {"facebook_check": False, "region_scrape": False},
    "pro": {"facebook_check": True, "region_scrape": True},
    "premium": {"facebook_check": True, "region_scrape": True},
}

# Short-lived cache so a fast-moving scrape (checking cap on every
# single lead) doesn't hit Supabase once per lead just to read plan
# status — only report_lead_saved()'s write happens that often.
# Refreshed at the start of every job regardless, so a plan change
# mid-scrape is picked up at the very next job even if this hasn't
# expired yet.
_plan_cache = {"status": None, "fetched_at": 0.0}
PLAN_CACHE_SECONDS = 30


def get_plan_status(force_refresh: bool = False) -> dict:
    """
    Fetches the logged-in user's plan + this period's usage from
    Supabase, reusing the same JWT /auth-callback already saved — no
    separate login needed for this. Falls back to the last known-good
    value on a transient network error, and to the most restrictive
    (free-tier) numbers if there's truly nothing to go on yet, so a
    Supabase hiccup can never let a scrape run past someone's real cap,
    and never crashes an in-progress job either.
    """
    default = {"plan": "free", "leads_used_this_period": 0, "cap": PLAN_CAPS["free"]}
    saved = load_token()
    if saved is None or not saved.get("token"):
        return default

    now = time.time()
    if not force_refresh and _plan_cache["status"] and (now - _plan_cache["fetched_at"] < PLAN_CACHE_SECONDS):
        return _plan_cache["status"]

    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_plans",
            params={"select": "plan,leads_used_this_period"},
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {saved['token']}",
            },
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return default
        plan = rows[0].get("plan", "free")
        result = {
            "plan": plan,
            "leads_used_this_period": rows[0].get("leads_used_this_period", 0),
            "cap": PLAN_CAPS.get(plan, PLAN_CAPS["free"]),
        }
        _plan_cache.update(status=result, fetched_at=now)
        return result
    except Exception as e:
        print(f"  Warning: couldn't fetch plan status from Supabase ({e})")
        return _plan_cache["status"] or default


def report_lead_saved():
    """
    Tells Supabase one more lead was saved for the logged-in user, via
    the increment_lead_usage() RPC (see supabase-schema.sql) — this is
    what keeps the website's usage banner accurate in real time, since
    it reads the same Supabase row. Best-effort: a failed call here
    never breaks the scrape itself — worst case this one lead's usage
    is undercounted until the next successful call catches back up, or
    the next fresh get_plan_status() call reconciles it.
    """
    saved = load_token()
    if saved is None or not saved.get("token"):
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/increment_lead_usage",
            json={"p_count": 1},
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {saved['token']}",
                "Content-Type": "application/json",
            },
            timeout=8,
        )
        if _plan_cache["status"]:
            _plan_cache["status"]["leads_used_this_period"] += 1
    except Exception as e:
        print(f"  Warning: couldn't report lead usage to Supabase ({e})")


# IMPORTANT: replace with your real Lovable domain(s). Keep localhost
# origins for local testing.
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://preview--leads-finder-dash.lovable.app",
    "https://leads-finder-dash.lovable.app",
    "https://scrape.systems",
    "https://www.scrape.systems",
]

# Any *.lovable.app or *.lovableproject.com preview/staging URL is also
# allowed, so a new Lovable preview link never silently gets blocked
# (which would look like "agent not connected" even though it's running
# fine). Anything outside those two patterns must be added to
# ALLOWED_ORIGINS above explicitly.
ALLOWED_ORIGIN_REGEX = r"^https://([a-zA-Z0-9-]+\.)*(lovable\.app|lovableproject\.com)$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(swipe_launcher_router)


# ---------------------------------------------------------------------------
# CSRF guard for state-changing endpoints
# ---------------------------------------------------------------------------
# The dashboard's shared agent client attaches this header to every
# request. It is NOT a secret (it ships in the dashboard's public JS
# bundle) — the real protection is that a custom header forces the
# browser to preflight, and CORS above only lets that preflight
# succeed for allow-listed origins. So this blocks a malicious website
# from silently driving the agent via a victim's browser; it does not
# (and can't) stop something on the same machine that already knows
# the header value, same as any other localhost-only service.
REQUIRED_CLIENT_HEADER_VALUE = "scrapesystems-web"


def require_dashboard_client(x_scrapesystems_client: Optional[str] = Header(None)):
    if x_scrapesystems_client != REQUIRED_CLIENT_HEADER_VALUE:
        raise HTTPException(
            status_code=403,
            detail="Missing or invalid X-ScrapeSystems-Client header.",
        )


DEVICE_FILE = Path.home() / ".scrapesystems" / "device.json"


def load_or_create_device_id() -> str:
    """
    Returns a stable per-machine ID, generating and persisting one on
    first call. Backs the "one account per device" limit — the
    frontend fetches this via GET /device-id and passes it to
    Supabase's claim_device RPC after sign-in/sign-up.
    """
    try:
        if DEVICE_FILE.exists():
            data = json.loads(DEVICE_FILE.read_text())
            existing = data.get("device_id")
            if existing:
                return existing
    except (json.JSONDecodeError, OSError):
        pass  # fall through and regenerate if the file is missing/corrupt

    new_id = str(uuid.uuid4())
    DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEVICE_FILE.write_text(json.dumps({"device_id": new_id}))
    return new_id


@app.get("/device-id")
def device_id():
    return {"status": "ok", "device_id": load_or_create_device_id()}


@app.get("/ping")
def ping():
    return {"status": "ok", "service": "scrapesystems-agent", "version": "0.2.0"}


@app.get("/status")
def status():
    saved = load_token()
    if saved is None:
        return {"status": "ok", "authenticated": False}
    return {"status": "ok", "authenticated": True, "email": saved.get("email")}


class AuthCallbackBody(BaseModel):
    token: str
    email: Optional[str] = None


@app.post("/auth-callback")
def auth_callback_post(
    body: AuthCallbackBody,
    _: None = Depends(require_dashboard_client),
):
    """
    Same as the GET route below, but for current dashboard builds:
    keeps the login token out of the URL (query strings can end up in
    browser history, server logs, and the Referer header) by sending
    it in the POST body instead, and requires the dashboard's client
    header like every other state-changing endpoint.
    """
    save_token(body.token, body.email)
    return {"status": "ok"}


@app.get("/auth-callback", response_class=HTMLResponse)
def auth_callback(token: str = Query(...), email: Optional[str] = Query(None)):
    """
    Legacy path, kept so older downloaded/packaged agents (built before
    the POST route above existed) keep working without an update.
    """
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

# Tracks progress toward ONE combined Scrape History entry per region
# run, keyed by region_run_id. Region scrapes queue one job per city
# (see start_region_scrape below) and those jobs finish one at a time
# through the same FIFO worker — this dict is how _log_scrape_completion
# knows to wait until every city in the region has finished before
# writing a single "Region scrape" row with the combined lead count,
# instead of logging one row per city.
_region_progress: Dict[str, dict] = {}


class ScrapeRequest(BaseModel):
    niche: str
    location: str


def _found_summary(found: int, duplicates: int) -> str:
    """
    Formats how many leads a finished (or stopped) scrape found:
    "X new leads found, Y duplicates found" — always both numbers, to
    avoid the earlier ambiguity where "X leads found, Y duplicates"
    left it unclear whether X included the duplicates or not.

    `found` is the TOTAL businesses found (duplicates are a subset of
    it), so X here is found - duplicates, i.e. the actual new/unique
    count, never overlapping with Y.
    """
    new_leads = found - duplicates
    return f"{new_leads} new leads found, {duplicates} duplicates found"


def _log_scrape_completion(job_id: str):
    """
    Writes one Scrape History row for a job that just finished
    (phase == "done"). Called from inside _job_lock at every place in
    _run_job (and the /run-scrape/end endpoint) where a job reaches a
    terminal state, so job/_region_progress reads below are always
    consistent.

    Regular (including queued) scrapes: logged individually, one row
    per job, as soon as that job finishes OR is hard-stopped (phase
    "done" or "ended") — including a job stopped immediately after it
    started, which logs with whatever it found (possibly 0).

    Region scrapes: NOT logged individually. Each city job in a region
    run shares a region_run_id (see start_region_scrape); this
    accumulates leads_found across all of them and only writes the
    single combined "Region scrape" row once every city job for that
    region_run_id has reached "done" or "ended" — so a region run that
    gets stopped partway still logs, with whatever cities had finished
    or been stopped contributing their results. A "stopped" (paused,
    resumable) city doesn't count yet, since it might still be resumed
    and finish normally. A city that errors out is the one case still
    not handled — that group simply never completes and never logs.
    """
    job = _jobs.get(job_id)
    if job is None:
        return

    leads_found = job.get("leads_found") or 0
    duplicates_found = job.get("duplicates_found") or 0
    region_run_id = job.get("region_run_id")

    if region_run_id:
        progress = _region_progress.get(region_run_id)
        if progress is None:
            return
        progress["completed"] += 1
        progress["leads_found"] += leads_found
        progress["duplicates_found"] += duplicates_found
        if progress["completed"] >= progress["expected"]:
            log_scrape_history(
                search_query="Region scrape",
                scrape_type="region",
                leads_found=progress["leads_found"],
                duplicates_found=progress["duplicates_found"],
                sent_at=progress["sent_at"],
                region_run_id=region_run_id,
            )
            _region_progress.pop(region_run_id, None)
    else:
        search_query = f"{job.get('niche', '')} in {job.get('location', '')}".strip()
        log_scrape_history(
            search_query=search_query,
            scrape_type="regular",
            leads_found=leads_found,
            duplicates_found=duplicates_found,
            sent_at=job.get("sent_at") or datetime.now(timezone.utc).isoformat(),
        )


def _run_job(job_id: str, niche: str, location: str, resume: Optional[dict] = None):
    stop_event = _stop_events[job_id]

    # NOTE: Facebook-page checking is NO LONGER gated by plan here.
    # Every plan runs the full Maps -> Facebook pipeline below; the
    # only Facebook-related feature still gated by plan is the
    # dedicated Facebook Checker tool's on-demand recheck endpoint
    # (see /leads/{id}/recheck-facebook further down), which still
    # correctly reads PLAN_FEATURES["facebook_check"].

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
        # "leads_found" always increments, capped or not — it's the
        # progress bar's live counter, separate from what actually gets
        # saved. Whoever is watching this scrape should see the real
        # number of businesses found even in the leads past their cap.
        with _job_lock:
            if job_id in _jobs:
                _jobs[job_id]["leads_found"] = _jobs[job_id].get("leads_found") or 0
                _jobs[job_id]["leads_found"] += 1

        usage = get_plan_status()
        if usage["leads_used_this_period"] >= usage["cap"]:
            # Over the plan's cap for this period. Deliberately doesn't
            # stop the scrape/check itself — killing a live browser
            # mid-stage is messier than it's worth — just stops this
            # lead (and anything after it) from being written to
            # leads.db or billed as usage. capped_leads_skipped lets the
            # frontend tell the user how many were found but not saved.
            with _job_lock:
                if job_id in _jobs:
                    _jobs[job_id]["cap_reached"] = True
                    _jobs[job_id]["capped_leads_skipped"] = (
                        _jobs[job_id].get("capped_leads_skipped", 0) + 1
                    )
            return

        # Saved the instant this one lead is ready, so it shows up in
        # the website's leads table and count right away — not only
        # after the whole scrape finishes.
        result = save_leads(_lead_to_df(lead))
        if result.get("duplicates"):
            with _job_lock:
                if job_id in _jobs:
                    _jobs[job_id]["duplicates_found"] = (
                        _jobs[job_id].get("duplicates_found", 0) + result["duplicates"]
                    )
        report_lead_saved()

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
                            _log_scrape_completion(job_id)
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

            # Trim to exactly what's left in this billing period before
            # running the slow, block-prone Facebook-check stage. Without
            # this, a scrape that finds e.g. 9 businesses with only 5
            # leads of quota left would still burn a Firefox lookup on
            # all 9, even though on_lead_found() below was always going
            # to discard the last 4 anyway. This is purely an efficiency
            # cut, NOT the actual cap enforcement - on_lead_found()'s own
            # per-lead check right below is still what actually decides
            # what gets saved/counted, and still applies even here (e.g.
            # if another device's scrape used up quota in the meantime).
            leads_room = get_plan_status(force_refresh=True)
            quota_remaining = max(leads_room["cap"] - leads_room["leads_used_this_period"], 0)
            if len(businesses) > quota_remaining:
                trimmed_count = len(businesses) - quota_remaining
                # These businesses are being skipped for the exact same
                # reason on_lead_found() skips leads past the cap - they
                # just never get that far since we're cutting them before
                # the (expensive) Facebook check even runs. Record them
                # the same way on_lead_found() would, so the "leads
                # found" progress count and capped_leads_skipped (which
                # the frontend's usage-limit popup watches) stay accurate
                # even though these specific businesses never get
                # Facebook-checked or saved.
                with _job_lock:
                    if job_id in _jobs:
                        _jobs[job_id]["leads_found"] = (
                            _jobs[job_id].get("leads_found") or 0
                        ) + trimmed_count
                        _jobs[job_id]["cap_reached"] = True
                        _jobs[job_id]["capped_leads_skipped"] = (
                            _jobs[job_id].get("capped_leads_skipped", 0) + trimmed_count
                        )
                businesses = businesses[:quota_remaining]

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
                        _log_scrape_completion(job_id)
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
                    found = _jobs[job_id].get("leads_found") or len(leads)
                    duplicates = _jobs[job_id].get("duplicates_found") or 0
                    _jobs[job_id].update(
                        phase="done", current=100, total=100,
                        message=f"Done — {_found_summary(found, duplicates)}",
                    )
                    _log_scrape_completion(job_id)
    except Exception as e:
        with _job_lock:
            if job_id in _jobs:
                _jobs[job_id].update(phase="error", message=str(e))


_scrape_queue: "queue.PriorityQueue" = queue.PriorityQueue()
_queue_seq = itertools.count()
_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def _enqueue_job(job_id: str, priority: int = 10):
    """
    Adds job_id to the scrape queue. Lower priority number = runs
    sooner. Regular newly-queued scrapes use the default priority
    (10) and are ordered by insertion order among themselves via
    the monotonic _queue_seq tie-breaker - same FIFO behavior as
    before for normal queueing.

    Resuming a paused ("stopped") scrape uses priority=0 so it
    always jumps ahead of anything already sitting in the queue,
    rather than going to the back of the line behind scrapes that
    were queued while it was paused - Continue should mean "pick
    back up now", not "wait your turn again".
    """
    _scrape_queue.put((priority, next(_queue_seq), job_id))


def _scrape_worker():
    """
    The ONE thread that ever actually runs a scrape. Pulls job_ids off
    _scrape_queue in priority order (see _enqueue_job) - normal queued
    scrapes run in submission order, but a resumed/continued scrape
    always jumps to the front of the line.

    IMPORTANT: the "wait while something is paused" check happens
    BEFORE pulling anything off the queue, not after. If a job is
    dequeued first and then waits, it can end up holding e.g. the
    2nd queued scrape while a paused 1st scrape sits re-queued with
    higher priority - by the time the pause clears, this thread is
    already committed to running the wrong job. Waiting up front
    means nothing is ever dequeued until it's actually safe to run
    whatever comes out next, so a resumed job's priority is honored.
    """
    while True:
        # Don't pull anything off the queue while another job is
        # paused ("stopped") - just wait. This is what lets a
        # Continue (which re-queues at priority=0) actually win the
        # next queue.get() instead of losing to something that was
        # dequeued earlier while frozen.
        while True:
            with _job_lock:
                paused = any(j.get("phase") == "stopped" for j in _jobs.values())
            if not paused:
                break
            time.sleep(0.5)

        _priority, _seq, job_id = _scrape_queue.get()
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


@app.post("/run-scrape", dependencies=[Depends(require_dashboard_client)])
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

    # Already fully capped for this billing period - refuse outright
    # rather than let a brand-new scrape run its full (possibly
    # minutes-long) Maps stage only to discard every result once
    # on_lead_found()/the quota trim above kick in. The frontend should
    # catch this 402 and show the same usage-limit popup used when an
    # in-progress scrape hits the cap, instead of silently queuing
    # something that was never going to save anything.
    usage = get_plan_status(force_refresh=True)
    if usage["leads_used_this_period"] >= usage["cap"]:
        raise HTTPException(
            status_code=402,
            detail=f"You've used all {usage['cap']} leads for this billing period. Upgrade to keep scraping.",
        )

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
            # When this scrape was actually submitted — used by Scrape
            # History (see _log_scrape_completion) instead of whenever
            # it happens to finish, since queue wait + run time can
            # push completion well past when the user actually sent it.
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }

    _ensure_worker()
    _enqueue_job(job_id)
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


@app.post("/run-scrape/stop", dependencies=[Depends(require_dashboard_client)])
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


@app.post("/run-scrape/continue", dependencies=[Depends(require_dashboard_client)])
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
            message="Resuming — will start as soon as the current scrape finishes",
        )

    _ensure_worker()
    _enqueue_job(job_id, priority=0)
    return {"job_id": job_id, "status": "resumed"}


@app.post("/run-scrape/end", dependencies=[Depends(require_dashboard_client)])
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
            _log_scrape_completion(job_id)
        else:
            _end_requested[job_id] = True
            _stop_events[job_id].set()

    return {"status": "stopping", "job_id": job_id}


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_dashboard_client)])
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


@app.post("/run-region-scrape", dependencies=[Depends(require_dashboard_client)])
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

    plan_status = get_plan_status(force_refresh=True)
    if not PLAN_FEATURES.get(plan_status["plan"], PLAN_FEATURES["free"])["region_scrape"]:
        raise HTTPException(
            status_code=403,
            detail="Region scrape is a Pro feature — upgrade to unlock it.",
        )

    try:
        cities = get_cities_in_region(req.region)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not look up cities for {req.region!r}: {e}")

    if not cities:
        raise HTTPException(status_code=404, detail=f"No cities found for {req.region!r}.")

    region_run_id = str(uuid.uuid4())
    job_ids = []

    with _job_lock:
        _region_progress[region_run_id] = {
            "expected": len(cities),
            "completed": 0,
            "leads_found": 0,
            "duplicates_found": 0,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
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
        _enqueue_job(job_id)

    return {
        "region_run_id": region_run_id, "region": req.region,
        "cities_found": len(cities), "job_ids": job_ids,
    }


@app.post("/run-region-scrape/cancel", dependencies=[Depends(require_dashboard_client)])
def cancel_region_scrape(region_run_id: Optional[str] = Query(None)):
    """
    Cancels an entire region run in one call ("Stop All").

    Queued cities are removed from memory outright (the worker skips
    any job id it can no longer find), and the city actually running
    is hard-stopped exactly like /run-scrape/end — its already-saved
    leads stay saved, and it finalizes as "ended" a moment later.
    Paused and already-finished cities are removed too, so a page
    reload can never redraw the cancelled queue.

    If region_run_id isn't given, every region job currently tracked
    is cancelled.
    """
    cancelled, stopping = [], []
    with _job_lock:
        targets = [
            jid for jid, j in list(_jobs.items())
            if j.get("region_run_id")
            and (region_run_id is None or j.get("region_run_id") == region_run_id)
        ]
        touched_runs = set()
        for jid in targets:
            job = _jobs.get(jid)
            if job is None:
                continue
            touched_runs.add(job.get("region_run_id"))
            phase = job.get("phase")
            if phase not in ("queued", "stopped", "done", "error", "ended", "idle"):
                # A live thread owns this one — flag it so it finalizes
                # as "ended" (no resume checkpoint) at its next safe
                # stopping point, keeping every lead already saved.
                _end_requested[jid] = True
                ev = _stop_events.get(jid)
                if ev is not None:
                    ev.set()
                stopping.append(jid)
            else:
                # Queued, paused or finished: drop it entirely, but let
                # a paused city's already-found leads still count toward
                # the region's combined totals before it's removed.
                progress = _region_progress.get(job.get("region_run_id"))
                if progress is not None and phase == "stopped":
                    progress["completed"] += 1
                    progress["leads_found"] += job.get("leads_found") or 0
                    progress["duplicates_found"] += job.get("duplicates_found") or 0
                _jobs.pop(jid, None)
                _stop_events.pop(jid, None)
                _end_requested.pop(jid, None)
                cancelled.append(jid)

        # Re-baseline each touched region run to what actually ran (plus
        # anything still finalizing), so the combined-row threshold in
        # _log_scrape_completion can be reached even though the queued
        # cities above were dropped instead of ever running.
        for rid in touched_runs:
            if not rid:
                continue
            progress = _region_progress.get(rid)
            if progress is None:
                continue
            still_stopping = sum(
                1 for jid in stopping
                if (_jobs.get(jid) or {}).get("region_run_id") == rid
            )
            progress["expected"] = progress["completed"] + still_stopping
            if still_stopping == 0:
                # Nothing left to finish for this run — write the
                # partial Scrape History row now.
                if progress["completed"] > 0:
                    log_scrape_history(
                        search_query="Region scrape",
                        scrape_type="region",
                        leads_found=progress["leads_found"],
                        duplicates_found=progress["duplicates_found"],
                        sent_at=progress["sent_at"],
                        region_run_id=rid,
                    )
                _region_progress.pop(rid, None)

    return {"status": "cancelled", "removed": cancelled, "stopping": stopping}


@app.get("/scrape-history")
def scrape_history():
    """
    Returns past scrape runs for the Scrape History sidebar section,
    most recent first. Flat, read-only, non-clickable list per the
    agreed scope — nothing here links back to individual leads, and
    nothing is backfilled for scrapes that ran before this shipped.
    """
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")
    return {"history": get_scrape_history()}


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
            # ISO 8601 UTC timestamp set once, when the lead row is
            # first inserted (see db_store.py's save_leads() INSERT).
            # Used by the dashboard's leads-over-time chart to bucket
            # leads by day/hour.
            "createdAt": row["created_at"] if "created_at" in row.keys() else None,
            # "screened" is added by lead_review.py the first time a
            # swipe session runs (ALTER TABLE) — guard against it not
            # existing yet on a fresh install that hasn't used that
            # feature. "good" | "bad" | None.
            "screened": row["screened"] if "screened" in row.keys() and row["screened"] else None,
            # ISO 8601 UTC timestamp, set once when a lead's status first
            # becomes anything other than "Not Contacted" (see db_store's
            # _next_contacted_at). None = never contacted, or a lead from
            # before this column existed. Source of truth for the
            # dashboard's contacted stats/chart.
            "contactedAt": row["contacted_at"] if "contacted_at" in row.keys() and row["contacted_at"] else None,
        }
        for row in rows
    ]
    return {"leads": leads}


@app.post("/leads/{lead_id}/recheck-facebook", dependencies=[Depends(require_dashboard_client)])
def recheck_facebook(lead_id: int):
    """
    Manually re-runs the Facebook-page lookup for ONE existing lead —
    e.g. a lead saved back when this account was on Free (no Facebook
    data at all), or one whose Facebook status just looks stale.
    Deliberately exempt from this period's cap: refreshing a field on
    a row that's already saved and already counted isn't new usage,
    so this never calls report_lead_saved() either. Still gated on
    plan, though — Free never gets Facebook data, recheck or not.

    Runs the real Firefox-based lookup synchronously (typically ~10-20
    seconds — browser launch plus one deliberate pacing delay, see
    check_facebook_pages' docstring), so the frontend should show a
    spinner rather than expect an instant response.
    """
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    plan_status = get_plan_status(force_refresh=True)
    if not PLAN_FEATURES.get(plan_status["plan"], PLAN_FEATURES["free"])["facebook_check"]:
        raise HTTPException(
            status_code=403,
            detail="Facebook page checking is a Pro feature — upgrade to unlock it.",
        )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT business_name, niche, address, phone, rating, review_count, website, maps_url, search_query "
        "FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}")

    # Reshape back into the lowercase "business" dict check_facebook_pages
    # expects (the same shape scrape_maps() produces) — the inverse of
    # db_store.py's _row_to_sheet_dict mapping.
    business = {
        "name": row["business_name"] or "",
        "category": row["niche"] or "",
        "address": row["address"] or "",
        "phone": row["phone"] or "",
        "rating": row["rating"] or "",
        "review_count": row["review_count"] or "",
        "has_website": bool(row["website"]),
        "maps_url": row["maps_url"] or "",
        "search_query": row["search_query"] or "",
    }

    # A single business is enough context for find_facebook_page — its
    # own address stands in for "location" here, since a one-off
    # recheck doesn't carry the original search's location separately.
    leads, _remaining = check_facebook_pages([business], business["address"])
    if not leads:
        raise HTTPException(status_code=500, detail="Facebook check didn't return a result — try again.")

    updated = leads[0]
    has_facebook = updated["Has facebook"] == "True"
    facebook_url = updated["Facebook URL"]

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE leads SET has_facebook = ?, facebook_url = ?, updated_at = datetime('now') WHERE id = ?",
        (int(has_facebook), facebook_url, lead_id),
    )
    conn.commit()
    conn.close()

    return {"id": lead_id, "hasFacebook": has_facebook, "facebookUrl": facebook_url}


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


@app.patch("/leads/{lead_id}/status", dependencies=[Depends(require_dashboard_client)])
def update_lead_status(lead_id: int, body: StatusUpdate):
    """
    Updates a single lead's status by its real row id — the only
    field the website is allowed to edit directly. Everything else
    about a lead only ever comes from a scrape.
    """
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT contacted_at FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}")

        now = datetime.now(timezone.utc).isoformat()
        new_contacted_at = _next_contacted_at(existing["contacted_at"], body.status, now)

        conn.execute(
            "UPDATE leads SET status = ?, contacted_at = ?, updated_at = datetime('now') WHERE id = ?",
            (body.status, new_contacted_at, lead_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"id": lead_id, "callStatus": body.status, "contactedAt": new_contacted_at}


class ScreenedUpdate(BaseModel):
    # "good" | "bad" | None (None clears the mark).
    screened: Optional[str] = None


@app.patch("/leads/{lead_id}/screened", dependencies=[Depends(require_dashboard_client)])
def update_lead_screened(lead_id: int, body: ScreenedUpdate):
    """
    Marks a single lead Good or Bad straight from the dashboard table,
    for any lead (not only ones that went through the Facebook
    checker / swipe tool). Mirrors the swipe tool's write logic in
    lead_review.py's _mark_lead():

    - "bad"  -> screened = 'bad' and status = 'Bad Lead'.
    - "good" -> screened = 'good'; a leftover status of 'Bad Lead' is
                reset to 'Not Contacted' so a row can never be both.
    - None / "" -> clears the mark; a status of 'Bad Lead' is reset
                to 'Not Contacted' the same way.

    contacted_at is never stamped by this endpoint: marking a lead
    good/bad is a screening decision, not outreach. It is only cleared
    when the status goes back to 'Not Contacted', same rule as
    everywhere else (see db_store._next_contacted_at).
    """
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    value = (body.screened or "").strip().lower() or None
    if value not in (None, "good", "bad"):
        raise HTTPException(status_code=400, detail='screened must be "good", "bad", or null.')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT status, contacted_at FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}")

        status = existing["status"] or "Not Contacted"
        contacted_at = existing["contacted_at"]

        if value == "bad":
            status = "Bad Lead"
        elif status == "Bad Lead":
            status = "Not Contacted"
            contacted_at = _next_contacted_at(contacted_at, status, None)

        conn.execute(
            "UPDATE leads SET screened = ?, status = ?, contacted_at = ?, updated_at = datetime('now') WHERE id = ?",
            (value, status, contacted_at, lead_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"id": lead_id, "screened": value, "callStatus": status, "contactedAt": contacted_at}


class LeadFieldsUpdate(BaseModel):
    call_notes: Optional[str] = None
    text_phone_number: Optional[str] = None
    email: Optional[str] = None
@app.patch("/leads/{lead_id}", dependencies=[Depends(require_dashboard_client)])
def update_lead_fields(lead_id: int, body: LeadFieldsUpdate):
    if load_token() is None:
        raise HTTPException(status_code=401, detail="Not connected — log in on the website first.")

    updates = body.dict(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    set_clause = ", ".join(f"{field} = ?" for field in updates)
    values = list(updates.values())

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        f"UPDATE leads SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
        (*values, lead_id),
    )
    conn.commit()
    updated = cur.rowcount
    conn.close()

    if updated == 0:
        raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}")

    return {"id": lead_id, **updates}
if __name__ == "__main__":
    import uvicorn

    free_port(8765)
    uvicorn.run(app, host="127.0.0.1", port=8765)
