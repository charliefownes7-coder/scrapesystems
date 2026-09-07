"""
lead_review.py — Lovable-facing lead review feature for ScrapeSystems.

This is a FastAPI router, not a standalone app — it plugs into your
existing main.py (or agent.py) alongside /run-scrape, so the whole
thing ships as ONE download/ONE server, not a separate process.

WHAT THIS DOES (same behavior as your Streamlit swipe checker /
swipe_tool.py, just served to Lovable instead of a local browser
window):
- Streams a lead's live Facebook page into the Lovable dashboard using
  a real headless Chromium browser + DevTools Protocol screencasting
  (the same low-level API paid tools like Browserbase use for live
  browser views — pushes frames as the page changes, instead of us
  manually taking repeated screenshots).
- Exposes a "current lead to review" + "submit good/bad decision"
  API that pulls from and writes to your existing leads.db, using the
  EXACT same query and UPDATE logic as swipe_tool.py — a lead
  reviewed from Lovable and one reviewed from the local swipe tool
  end up in identical states, no divergence.

KNOWN TRADEOFF: this uses Chromium (not Firefox). Chromium's
automation fingerprint is more detectable by Facebook than Firefox's
— that's why the rest of ScrapeSystems (bulk/unattended scraping)
uses Firefox. This feature is a different usage pattern though — one
human, one page, real-time review — which is lower detection risk
than bulk scraping, but not zero. Worth watching for CAPTCHAs/blocks
if this gets heavy use.

--- INTEGRATION STEPS ---

1. Save this file as lead_review.py in the SAME folder as your
   main.py (wherever leads.db and /run-scrape already live).

2. Install what this needs (skip anything already installed):
       pip install --break-system-packages fastapi uvicorn playwright websockets
       playwright install chromium

3. In main.py, add these two lines — import goes near your other
   imports, include_router goes anywhere after `app = FastAPI(...)`:

       from lead_review import router as lead_review_router
       app.include_router(lead_review_router)

   That's it — no other changes to main.py. This ADDS new endpoints
   (/review/current, /review/decide, /review/ws) alongside whatever
   you already have; it doesn't touch or replace /run-scrape or
   anything else.

4. If Lovable can't already reach main.py's other endpoints (i.e. if
   /run-scrape didn't need extra CORS setup), you're already good —
   this router uses main.py's existing FastAPI app and its existing
   CORS config. If /run-scrape DOES already work from Lovable, these
   new endpoints will too, automatically.

5. Restart your agent (however you normally start main.py) and test
   directly first, before wiring up Lovable:
       curl http://127.0.0.1:8765/review/current
   (adjust the port to whatever main.py actually runs on)

6. Once that returns real lead data from leads.db, paste the Lovable
   prompt (given separately) to build the dashboard page.

--- LOVABLE PROMPT (paste this once step 5 works) ---

Add a "Review Leads" feature to the Dashboard. In the top-right
corner, show a small floating box with the current lead's business
name, a "Bad Lead" button, and a "Good Lead" button — styled like a
compact overlay, not a full panel. Below/behind it, show a
live-streamed view of the lead's Facebook page: connect to
ws://127.0.0.1:8765/review/ws (adjust port to match main.py) and
render incoming binary frames as a Blob URL on an <img>, replacing
the previous frame each time and revoking the old blob URL. Clicking
on the streamed image sends a click action via POST to
http://127.0.0.1:8765/session/action with body
{"type": "click", "x": <scaled x>, "y": <scaled y>} — scale from
displayed size to 1000x800. Scrolling batches deltas and sends them
every ~50ms via the same endpoint with
{"type": "scroll", "delta_y": <accumulated delta>}.

On page load, call GET http://127.0.0.1:8765/review/current to get
the first lead and display its business name in the overlay.
Clicking "Good Lead" or "Bad Lead" POSTs to
http://127.0.0.1:8765/review/decide with body {"result": "good"} or
{"result": "bad"} — the response contains the NEXT lead already, so
update the overlay and stream target from that response directly. If
the response shows "done": true, replace the streamed view with a
"No leads left to review" message. Match the existing dark theme
(#272726 backgrounds, #0057FD blue accent).

--- CHANGE LOG ---

- Fixed: the auto-dismiss loop previously only knew how to click a
  "Close" button on Facebook's dialog-style login wall. Facebook also
  shows a DIFFERENT login wall that replaces the ENTIRE page (a real
  navigation to a login form, not an overlay) — that version has no
  close button at all, so the old logic silently did nothing when it
  showed up. The loop now also detects that full-page version (by URL
  and by the presence of the login form's email field) and recovers
  by re-navigating back to the lead's actual Facebook URL, capped at
  3 attempts per 30 seconds so it can't get stuck in an endless
  flash-back-and-forth loop.

- Fixed (root cause): the login wall was showing up far more
  aggressively here than in swipe_tool.py, even though both use
  Chromium and neither is logged into a real Facebook account. The
  actual difference: swipe_tool.py launches Chromium with
  headless=False (a real, visible browser window), while this file
  was launching with headless=True. Headless Chromium has its own
  detectable fingerprint, separate from the CDP-automation-protocol
  issue that pushed the rest of ScrapeSystems to Firefox — Facebook
  appears to treat headless sessions as more suspicious and wall them
  off harder, which lines up with swipe_tool.py never hitting this
  wall.

  WHY OFF-SCREEN, NOT A VIRTUAL DISPLAY: the first attempt at this
  wrapped headless=False in a virtual display (Xvfb via
  pyvirtualdisplay) so nothing would be visible on screen. That fought
  with Crostini's own display setup — Xvfb either failed to start or
  Chromium preferred the real display anyway, so a real, visible
  browser window opened on screen instead of staying hidden. Simpler
  fix: launch headed for real, but position the window 3000px off the
  left edge of the screen (--window-position) so it's never visible or
  in your way, with no virtual display involved at all.
"""

import asyncio
import base64
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from db_store import DB_PATH

router = APIRouter()

VIEWPORT = {"width": 1000, "height": 800}


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_screened_column(conn):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
    if "screened" not in existing:
        conn.execute("ALTER TABLE leads ADD COLUMN screened TEXT")
        conn.commit()


def _load_review_queue():
    """Identical query to swipe_tool.py's _load_queue()."""
    with _conn() as conn:
        _ensure_screened_column(conn)
        rows = conn.execute(
            """
            SELECT id, business_name, niche, city, facebook_url FROM leads
            WHERE has_facebook = 1
              AND (move_to_cold_call IS NULL OR move_to_cold_call = 0)
              AND (screened IS NULL OR screened = '')
              AND (status IS NULL OR status = '' OR status = 'Not Contacted')
              AND facebook_url IS NOT NULL AND facebook_url != ''
            ORDER BY id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _mark_lead(lead_id, result):
    """Same write logic as swipe_tool.py's _mark(), plus: marking a
    lead 'good' clears a stale status='Bad Lead' if it's sitting there
    from an earlier (possibly incorrect) bad verdict, so a row can
    never end up in the contradictory state of screened='good' AND
    status='Bad Lead' at the same time."""
    with _conn() as conn:
        _ensure_screened_column(conn)
        if result == "bad":
            conn.execute(
                "UPDATE leads SET screened = ?, status = 'Bad Lead' WHERE id = ?",
                (result, lead_id),
            )
        else:
            conn.execute(
                """
                UPDATE leads SET screened = ?,
                    status = CASE WHEN status = 'Bad Lead' THEN 'Not Contacted' ELSE status END
                WHERE id = ?
                """,
                (result, lead_id),
            )


# --- Browser/streaming state (single active review session) ---
_playwright = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_cdp_session = None
_last_action_time = 0.0
_latest_frame_bytes: Optional[bytes] = None
_websocket_clients: set = set()
_dismiss_task: Optional[asyncio.Task] = None
_dismiss_running = False

# Timestamps of recent re-navigations away from the full-page login
# wall (see _auto_dismiss_loop) — used to detect when we're stuck in
# a re-navigate -> wall reappears -> re-navigate loop, since Facebook
# will keep re-showing that wall past a certain scroll point no
# matter how many times we navigate back.
_dismiss_attempts_window: list = []

# The lead's real Facebook URL for whichever review session is
# currently active — the auto-dismiss loop needs this so it knows
# where to re-navigate back to if the page gets replaced by the
# full-page login wall (see _auto_dismiss_loop below).
_current_review_url: Optional[str] = None

# Consecutive FAILURES TO LOAD a lead's page (technical failures only
# — timeouts, browser crashes, network errors — never a human
# decision). This mirrors swipe_tool.py's circuit breaker: a load
# failure must never be silently written to the database as a human
# "Bad Lead" verdict. Instead it's surfaced to the reviewer, who can
# explicitly decide good/bad or call /review/skip to leave it
# unreviewed. After too many failures in a row, the session stops
# entirely instead of cascading through the queue.
_consecutive_load_failures = 0
MAX_CONSECUTIVE_LOAD_FAILURES = 5

# Facebook shows a "log in to see more" wall two different ways:
#   1. A dismissible dialog/overlay ON TOP of the page — has a real
#      close button, one of the selectors below.
#   2. A FULL-PAGE login form that Facebook navigates to directly —
#      this is NOT a dialog, there's no close button, the page itself
#      just became the login form. This shows up both on initial load
#      and again after scrolling far enough on a page you're not
#      logged into.
# We handle both: try the click-to-close selectors for #1, and detect
# + recover from #2 by re-navigating back to the lead's URL.
_POPUP_CLOSE_SELECTORS = [
    '[aria-label="Close"]',
    'div[role="dialog"] [aria-label="Close"]',
    'div[aria-label="Close"][role="button"]',
    # Post lightbox/viewer popups (e.g. clicking into a single post)
    # sometimes render their close control slightly differently —
    # these extra patterns catch that version too.
    'div[role="dialog"] div[role="button"][aria-label="Close"]',
    'div[aria-label="Close"]',
]

# --- Review-queue state (leads.db-backed, mirrors swipe_tool.py) ---
_review_queue: list = []
_review_index: int = 0
_review_loaded: bool = False


class ActionRequest(BaseModel):
    type: str  # "click" | "scroll" | "dismiss_login_popup"
    x: Optional[float] = None
    y: Optional[float] = None
    delta_y: Optional[float] = None


class DecideRequest(BaseModel):
    result: str  # "good" | "bad"


async def _broadcast_frame(frame_bytes: bytes):
    dead = set()
    for ws in _websocket_clients:
        try:
            await ws.send_bytes(frame_bytes)
        except Exception:
            dead.add(ws)
    _websocket_clients.difference_update(dead)


def _on_screencast_frame(params):
    asyncio.create_task(_handle_frame(params))


async def _handle_frame(params):
    global _latest_frame_bytes
    frame_bytes = base64.b64decode(params["data"])
    _latest_frame_bytes = frame_bytes
    await _broadcast_frame(frame_bytes)
    if _cdp_session:
        try:
            await _cdp_session.send(
                "Page.screencastFrameAck",
                {"sessionId": params["sessionId"]},
            )
        except Exception as e:
            print(f"[lead_review] ack failed: {e}")


async def _is_full_page_login_wall() -> bool:
    """
    Detects Facebook's full-page login wall — the version that
    replaces the entire page (a real navigation) rather than showing
    a closeable dialog on top of it. Checked two ways: the URL itself
    (Facebook's login form lives at a /login-adjacent URL), and the
    presence of the login form's email input as a fallback in case
    the URL pattern changes.
    """
    if _page is None:
        return False
    try:
        current_url = _page.url or ""
        if "login" in current_url.lower():
            return True
        email_field = _page.locator('input[name="email"]')
        return await email_field.count() > 0
    except Exception:
        return False


async def _auto_dismiss_loop():
    """Runs continuously in the background for the life of a review
    session, checking every second or so for Facebook's login wall
    and clearing it automatically — including the one that reappears
    after scrolling partway down a page, not just the initial-load
    one. No user action required; the reviewer never has to click
    anything to get past it.

    Handles BOTH versions of the wall:
      - Full-page login form (a real navigation, no close button):
        detected via _is_full_page_login_wall(), recovered by
        re-navigating back to the lead's actual Facebook URL — but
        only up to 3 times within any 30-second window. Facebook
        re-shows this wall past a certain point in a page no matter
        how many times we navigate back to it, so retrying forever
        just produces an endless flash-back-and-forth loop. Past that
        limit we back off and leave the wall up — the reviewer can
        still see it and move on to the next lead instead of main.py
        being stuck fighting it indefinitely.
      - Dialog/overlay version (post lightboxes, "log in to see more"
        overlays, etc — anything with a real close button): handled
        by the existing click-to-close selectors, tried whenever the
        full-page login wall isn't what's currently showing.
    """
    global _dismiss_attempts_window

    while _dismiss_running:
        if _page is not None:
            try:
                if await _is_full_page_login_wall():
                    now = time.time()
                    _dismiss_attempts_window = [
                        t for t in _dismiss_attempts_window if now - t < 30
                    ]

                    if len(_dismiss_attempts_window) >= 3:
                        # Hit Facebook's real anonymous-viewing limit on
                        # this page — re-navigating won't get past it,
                        # it'll just keep re-triggering. Stop fighting it.
                        await asyncio.sleep(5.0)
                        continue

                    if _current_review_url:
                        try:
                            await _page.goto(
                                _current_review_url,
                                wait_until="domcontentloaded",
                                timeout=15000,
                            )
                            _dismiss_attempts_window.append(now)
                        except Exception as e:
                            print(f"[lead_review] re-navigation past login wall failed: {e}")
                else:
                    _dismiss_attempts_window.clear()
                    for selector in _POPUP_CLOSE_SELECTORS:
                        try:
                            btn = _page.locator(selector).first
                            if await btn.count() > 0:
                                await btn.click(timeout=500)
                                break  # one real popup at a time is normal
                        except Exception:
                            pass  # selector didn't match or wasn't clickable right now — fine, try again next loop
            except Exception as e:
                print(f"[lead_review] auto-dismiss loop error: {e}")
        await asyncio.sleep(1.0)


async def _start_streaming_session(url: str):
    global _playwright, _browser, _context, _page, _cdp_session, _dismiss_task, _dismiss_running, _current_review_url

    _current_review_url = url

    if _playwright is None:
        _playwright = await async_playwright().start()

    if _browser is None:
        # headless=False deliberately — see the change log at the top
        # of this file. Positioned far off-screen so no window is
        # ever actually visible to you, without relying on a virtual
        # display (which fought with Crostini's own display setup and
        # ended up opening a real, visible window instead).
        _browser = await _playwright.chromium.launch(
            headless=False,
            args=["--window-position=-3000,0", "--window-size=1000,800"],
        )

    if _page:
        await _page.close()
    if _context:
        await _context.close()

    _context = await _browser.new_context(viewport=VIEWPORT)
    _page = await _context.new_page()

    try:
        await _page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        return {"status": "error", "message": f"Page failed to load: {e}"}

    _cdp_session = await _context.new_cdp_session(_page)
    _cdp_session.on("Page.screencastFrame", _on_screencast_frame)

    await _cdp_session.send(
        "Page.startScreencast",
        {
            "format": "jpeg",
            "quality": 85,
            "maxWidth": VIEWPORT["width"],
            "maxHeight": VIEWPORT["height"],
            "everyNthFrame": 1,
        },
    )

    if not _dismiss_running:
        _dismiss_running = True
        _dismiss_task = asyncio.create_task(_auto_dismiss_loop())

    return {"status": "ok", "url": url}


@router.get("/review/current")
async def review_current():
    global _review_queue, _review_index, _review_loaded, _consecutive_load_failures

    if not _review_loaded:
        _review_queue = _load_review_queue()
        _review_index = 0
        _review_loaded = True
        _consecutive_load_failures = 0

    if _review_index >= len(_review_queue):
        return {"done": True, "remaining": 0}

    lead = _review_queue[_review_index]
    result = await _start_streaming_session(lead["facebook_url"])

    if result.get("status") == "error":
        # IMPORTANT: a failure to LOAD a page is a technical problem,
        # not a human judgment — this must never write screened='bad'
        # on its own (that was the original bug). Surface it to the
        # reviewer instead; they can explicitly decide good/bad, or
        # POST /review/skip to leave it unreviewed and move on.
        _consecutive_load_failures += 1
        if _consecutive_load_failures >= MAX_CONSECUTIVE_LOAD_FAILURES:
            return {
                "done": True,
                "stopped_on_error": True,
                "remaining": len(_review_queue) - _review_index,
                "message": (
                    f"Stopped after {_consecutive_load_failures} consecutive "
                    "page load failures — nothing was auto-marked Bad. "
                    "Check your network/browser, then POST /review/reset "
                    "when ready to try again."
                ),
            }
        return {
            "done": False,
            "load_error": True,
            "message": result.get("message", "Page failed to load."),
            "lead": {
                "id": lead["id"],
                "business_name": lead["business_name"],
                "niche": lead["niche"],
                "city": lead["city"],
                "facebook_url": lead["facebook_url"],
            },
            "remaining": len(_review_queue) - _review_index,
        }

    _consecutive_load_failures = 0
    return {
        "done": False,
        "lead": {
            "id": lead["id"],
            "business_name": lead["business_name"],
            "niche": lead["niche"],
            "city": lead["city"],
            "facebook_url": lead["facebook_url"],
        },
        "remaining": len(_review_queue) - _review_index,
    }


@router.post("/review/decide")
async def review_decide(req: DecideRequest):
    global _review_index, _consecutive_load_failures

    if not _review_loaded or _review_index >= len(_review_queue):
        raise HTTPException(400, "No lead currently under review")

    if req.result not in ("good", "bad"):
        raise HTTPException(422, "result must be 'good' or 'bad'")

    lead = _review_queue[_review_index]
    _mark_lead(lead["id"], req.result)
    _review_index += 1
    _consecutive_load_failures = 0

    return await review_current()


@router.post("/review/skip")
async def review_skip():
    """
    Leaves the current lead unreviewed (writes nothing to screened/
    status) and advances to the next one. This is distinct from
    /review/decide, which always writes a human good/bad verdict —
    use this when a lead's page won't load and the reviewer wants to
    move on without rendering a judgment on it.
    """
    global _review_index, _consecutive_load_failures

    if not _review_loaded or _review_index >= len(_review_queue):
        raise HTTPException(400, "No lead currently under review")

    _review_index += 1
    _consecutive_load_failures = 0
    return await review_current()


@router.post("/review/reset")
async def review_reset():
    global _review_loaded
    _review_loaded = False
    return await review_current()


@router.websocket("/review/ws")
async def review_websocket(websocket: WebSocket):
    await websocket.accept()
    _websocket_clients.add(websocket)

    if _latest_frame_bytes is not None:
        try:
            await websocket.send_bytes(_latest_frame_bytes)
        except Exception:
            pass

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _websocket_clients.discard(websocket)


@router.post("/session/action")
async def send_action(action: ActionRequest):
    global _last_action_time

    if _page is None:
        raise HTTPException(400, "No active review session")

    if action.type == "click":
        if action.x is None or action.y is None:
            raise HTTPException(422, "click requires x and y")
        await _page.mouse.click(action.x, action.y)

    elif action.type == "scroll":
        delta = action.delta_y if action.delta_y is not None else 300
        await _page.mouse.wheel(0, delta)

    elif action.type == "dismiss_login_popup":
        try:
            close_btn = _page.locator('[aria-label="Close"]').first
            await close_btn.click(timeout=2000)
        except Exception:
            pass

    else:
        raise HTTPException(422, f"Unknown action type: {action.type}")

    _last_action_time = time.time()
    return {"status": "ok"}
