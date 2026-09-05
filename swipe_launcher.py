"""
swipe_launcher.py — Lovable-facing trigger for swipe_tool.py.

This is a FastAPI router, not a standalone app — it plugs into your
existing main.py alongside /run-scrape, same pattern as
lead_review.py did. Unlike lead_review.py, this doesn't try to stream
Facebook content into the Lovable dashboard at all — it just launches
the real, proven-working swipe_tool.py as its own browser window
(same as it already runs today), and gives Lovable a way to know it's
running and see live progress while it does.

WHY THIS EXISTS INSTEAD OF lead_review.py's STREAMING APPROACH:
Every attempt at getting a live-streamed Facebook view working inside
Lovable (CDP screencast, periodic screenshots, headless/headed
toggling, fresh-context-per-lead) kept hitting Facebook's login wall
unpredictably. swipe_tool.py never has this problem. Rather than keep
guessing at why, this takes the simpler, guaranteed-reliable path:
launch the exact thing that already works, and let Lovable show a
"session running" state with live progress instead of trying to
embed the browser view itself.

--- INTEGRATION STEPS ---

1. Save this file as swipe_launcher.py in the SAME folder as your
   main.py and swipe_tool.py (wherever leads.db already lives).

2. In main.py, add these two lines — import goes near your other
   imports, include_router goes anywhere after `app = FastAPI(...)`:

       from swipe_launcher import router as swipe_launcher_router
       app.include_router(swipe_launcher_router)

   If lead_review.py is still included from earlier, you can leave it
   in (it doesn't conflict — different endpoints) or remove it now
   that this replaces it. Removing it is cleaner if you're not using
   the streaming version anymore.

3. Restart your agent and test directly first:
       curl -X POST http://127.0.0.1:8765/swipe/start
       curl http://127.0.0.1:8765/swipe/status
   (adjust the port to whatever main.py actually runs on)

4. Once that launches a real swipe_tool.py window and /swipe/status
   reports real numbers, paste the Lovable prompt (given separately)
   to wire up the dashboard.

--- LOVABLE PROMPT (paste this once step 3 works) ---

Replace the current "Review Leads" streaming feature with a simpler
"Swipe Session" flow. The "Start Swipe Session" button should POST to
http://127.0.0.1:8765/swipe/start (adjust port to match main.py) —
immediately show a loading animation (the ScrapeSystems logo pulse
animation already built for the app) in place of where the streamed
view used to be. Poll GET http://127.0.0.1:8765/swipe/status every 1
second while a session is starting or active: it returns
{"running": true/false, "window_ready": true/false, "remaining":
<number>, "total": <number>}. Keep showing the loading animation
until "window_ready" becomes true — only then switch to a status
card saying something like "Swipe session running in a separate
window — check your screen" along with a live "<remaining> of
<total> leads left to review" count that updates as the numbers
change. When "running" becomes false (the window was closed or the
queue finished), hide the status card and return to the normal
dashboard view. Remove any code that connects to the old /review/ws
websocket or renders streamed video frames — none of that is used
anymore. Match the existing dark theme (#272726 backgrounds, #0057FD
blue accent).
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from db_store import DB_PATH
import sqlite3
from contextlib import contextmanager

router = APIRouter()

# swipe_tool.py is expected to live right next to this file / main.py.
SWIPE_TOOL_PATH = Path(__file__).parent / "swipe_tool.py"

# Written by swipe_tool.py the moment the first lead's window is
# actually up and ready — read here so /swipe/status can report a
# real "window_ready" flag instead of just "the process exists"
# (which can be a second or two before the window is actually usable).
STATUS_FILE = Path.home() / ".scrapesystems" / "swipe_session_status.json"

# Tracks the currently running swipe_tool.py process, if any. Only one
# session at a time — same as the streaming version only ever
# supported one active review session.
_process: Optional[subprocess.Popen] = None


def _is_window_ready() -> bool:
    try:
        if not STATUS_FILE.exists():
            return False
        data = json.loads(STATUS_FILE.read_text())
        return bool(data.get("window_ready", False))
    except Exception:
        return False


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _queue_counts() -> dict:
    """
    Same query as swipe_tool.py's _load_queue(), just counting instead
    of fetching rows — this is how /swipe/status reports live progress
    without needing any communication with the subprocess itself. As
    leads get marked good/bad inside swipe_tool.py's own window, this
    count naturally drops on its own since it's reading the same
    leads.db.
    """
    with _conn() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
        if "screened" not in existing:
            # No leads have ever been screened yet — nothing to
            # subtract, so total == remaining.
            total = conn.execute(
                """
                SELECT COUNT(*) FROM leads
                WHERE has_facebook = 1
                  AND (move_to_cold_call IS NULL OR move_to_cold_call = 0)
                  AND (status IS NULL OR status = '' OR status = 'Not Contacted')
                  AND facebook_url IS NOT NULL AND facebook_url != ''
                """
            ).fetchone()[0]
            return {"remaining": total, "total": total}

        remaining = conn.execute(
            """
            SELECT COUNT(*) FROM leads
            WHERE has_facebook = 1
              AND (move_to_cold_call IS NULL OR move_to_cold_call = 0)
              AND (screened IS NULL OR screened = '')
              AND (status IS NULL OR status = '' OR status = 'Not Contacted')
              AND facebook_url IS NOT NULL AND facebook_url != ''
            """
        ).fetchone()[0]

        # "total" here means "however many were queued at the start of
        # this run" — approximated as remaining + however many got
        # screened since the process started. Simpler and good enough:
        # just report remaining as both, letting the frontend show a
        # count that only goes down. If you want a true fixed starting
        # total later, that'd need swipe_tool.py to write its starting
        # count somewhere main.py can read it.
        return {"remaining": remaining, "total": remaining}


@router.post("/swipe/start")
def start_swipe_session():
    global _process

    if _process is not None and _process.poll() is None:
        return {"status": "already_running"}

    if not SWIPE_TOOL_PATH.exists():
        return {"status": "error", "message": f"swipe_tool.py not found at {SWIPE_TOOL_PATH}"}

    # Clear any leftover status file from a previous session before
    # starting a new one — otherwise a stale "window_ready": true from
    # last time could make /swipe/status report ready immediately,
    # before the new window actually exists.
    try:
        if STATUS_FILE.exists():
            STATUS_FILE.unlink()
    except Exception:
        pass

    _process = subprocess.Popen(
        [sys.executable, str(SWIPE_TOOL_PATH)],
        cwd=str(SWIPE_TOOL_PATH.parent),
    )
    return {"status": "started"}


@router.get("/swipe/status")
def swipe_status():
    global _process

    running = _process is not None and _process.poll() is None
    window_ready = _is_window_ready() if running else False
    counts = _queue_counts()

    return {
        "running": running,
        "window_ready": window_ready,
        "remaining": counts["remaining"],
        "total": counts["total"],
    }


@router.post("/swipe/stop")
def stop_swipe_session():
    """
    Not exposed in the Lovable prompt above (closing the actual
    browser window is the normal way to end a session, same as
    today), but available in case you want a "force stop" button
    later — e.g. if the window gets lost behind other windows.
    """
    global _process

    if _process is not None and _process.poll() is None:
        _process.terminate()
        return {"status": "stopped"}
    return {"status": "not_running"}
