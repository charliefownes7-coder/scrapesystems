"""
Standalone live-browser streaming prototype for ScrapeSystems — Chromium/CDP version.

What changed from the Firefox version:
- Uses Chromium instead of Firefox, specifically to use DevTools
  Protocol's native screencasting (Page.startScreencast). This is
  a purpose-built streaming API — the browser itself pushes frames
  as the page changes, instead of us manually taking a full
  screenshot + encoding it on a loop. This is the same underlying
  mechanism paid tools like Browserbase use for live browser views.
- No more background capture loop — frames arrive as CDP events and
  get pushed straight to connected WebSocket clients.

KNOWN TRADEOFF (carried over from earlier discussion, still true):
Chromium's automation fingerprint is more detectable by Facebook
than Firefox's — that's the exact reason the project moved to
Firefox for bulk/unattended scraping earlier on. This feature is a
different usage pattern (one human, one page, real-time review) —
lower risk than bulk scraping, but not zero risk. Worth watching
for blocks/captchas if this gets used a lot.

Run it with:
    pip install --break-system-packages fastapi uvicorn playwright websockets
    playwright install chromium
    uvicorn stream_server:app --port 8766 --reload

Then open test_stream.html in a browser and click "Start Session".
"""

import asyncio
import base64
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, Page, BrowserContext

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Global browser state (single session, for prototyping only) ---
_playwright = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_cdp_session = None
_last_action_time = 0.0
_latest_frame_bytes: Optional[bytes] = None
_websocket_clients: set = set()
_frame_count = 0
_frame_count_reset_time = 0.0

VIEWPORT = {"width": 1000, "height": 800}


class StartSessionRequest(BaseModel):
    url: str


class ActionRequest(BaseModel):
    type: str  # "click" | "scroll" | "dismiss_login_popup"
    x: Optional[float] = None
    y: Optional[float] = None
    delta_y: Optional[float] = None


async def _broadcast_frame(frame_bytes: bytes):
    dead = set()
    for ws in _websocket_clients:
        try:
            await ws.send_bytes(frame_bytes)
        except Exception:
            dead.add(ws)
    _websocket_clients.difference_update(dead)


def _on_screencast_frame(params):
    """CDP fires this every time Chromium has a new frame to offer.
    Must ack each frame (sessionId) or the browser stops sending
    more -- that's the CDP contract, not optional."""
    asyncio.create_task(_handle_frame(params))


async def _handle_frame(params):
    global _latest_frame_bytes, _frame_count
    frame_bytes = base64.b64decode(params["data"])
    _latest_frame_bytes = frame_bytes
    _frame_count += 1
    await _broadcast_frame(frame_bytes)

    if _cdp_session:
        try:
            await _cdp_session.send(
                "Page.screencastFrameAck",
                {"sessionId": params["sessionId"]},
            )
        except Exception as e:
            print(f"[cdp] ack failed: {e}")


@app.on_event("shutdown")
async def shutdown():
    global _browser, _playwright
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()


@app.post("/session/start")
async def start_session(req: StartSessionRequest):
    global _playwright, _browser, _context, _page, _cdp_session, _frame_count, _frame_count_reset_time

    if _playwright is None:
        _playwright = await async_playwright().start()

    if _browser is None:
        _browser = await _playwright.chromium.launch(headless=True)

    if _page:
        await _page.close()
    if _context:
        await _context.close()

    _context = await _browser.new_context(viewport=VIEWPORT)
    _page = await _context.new_page()

    try:
        await _page.goto(req.url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        return {"status": "error", "message": f"Page failed to load: {e}"}

    _cdp_session = await _context.new_cdp_session(_page)
    _cdp_session.on("Page.screencastFrame", _on_screencast_frame)

    await _cdp_session.send(
        "Page.startScreencast",
        {
            "format": "jpeg",
            "quality": 85,  # CDP's native pipeline has real headroom — pushed up from 60
            "maxWidth": VIEWPORT["width"],
            "maxHeight": VIEWPORT["height"],
            "everyNthFrame": 1,
        },
    )

    _frame_count = 0
    _frame_count_reset_time = time.time()

    return {"status": "ok", "url": req.url}


@app.websocket("/session/ws")
async def frame_websocket(websocket: WebSocket):
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


@app.get("/session/frame")
async def get_frame():
    if _latest_frame_bytes is None:
        raise HTTPException(400, "No frame yet — session may still be starting")
    b64 = base64.b64encode(_latest_frame_bytes).decode("ascii")
    return {"frame": b64, "ts": time.time()}


@app.post("/session/action")
async def send_action(action: ActionRequest):
    global _last_action_time

    if _page is None:
        raise HTTPException(400, "No active session — call /session/start first")

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


@app.post("/session/stop")
async def stop_session():
    global _page, _context, _cdp_session, _latest_frame_bytes

    if _cdp_session:
        try:
            await _cdp_session.send("Page.stopScreencast")
        except Exception:
            pass
        _cdp_session = None

    if _page:
        await _page.close()
        _page = None
    if _context:
        await _context.close()
        _context = None

    _latest_frame_bytes = None
    return {"status": "ok"}
