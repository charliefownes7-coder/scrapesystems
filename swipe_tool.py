"""
swipe_tool.py — Tinder-style Facebook lead screening tool.

Run standalone:
    python3 swipe_tool.py

Or launched automatically by main.py when "Start Swipe Session" is
clicked in the Lovable dashboard (see swipe_launcher.py) — either way
it runs as its own separate program, a real browser window.

A small overlay appears in the top-left corner of the browser window
with two clickable buttons — "Bad Lead" and "Good Lead" — plus a
confirmation that the tool is active. Click whichever fits, and it
loads the next lead automatically. Arrow keys still work too if you
ever want to use those instead:
    Right Arrow   Mark the current lead GOOD, load the next one
    Left Arrow    Mark the current lead BAD, load the next one
    Esc           Quit the tool entirely

WHY THIS VERSION DOESN'T USE GLOBAL HOTKEYS:
An earlier version used pynput to listen for keys system-wide. On a
Chromebook's Linux (Crostini) environment, that doesn't reliably work
— the virtualized display layer between the Linux container and
ChromeOS doesn't pass background keypresses through to a Python
script the way a normal Linux desktop would, which is why the keys
did nothing.

This version sidesteps that entirely: instead of listening at the OS
level, it listens for the arrow keys INSIDE the browser window itself
(the one showing the Facebook page you're already looking at), using
Playwright to inject a small script into the page. Since your focus
is already on that window while you're reviewing a lead, this works
reliably regardless of Crostini's display limitations.

WHAT "GOOD" / "BAD" ACTUALLY DO TO leads.db:
    GOOD -> leaves the lead as a normal DM-ready lead (no change to any
            column your Streamlit app already uses), and stamps a new
            'screened' column with 'good' so it won't show up in the
            swipe queue again.
    BAD  -> stamps 'screened' = 'bad' and sets the status to "Bad Lead"
            (an option your Streamlit app already has) so it's visibly
            flagged in the list. It stays right where it is in your
            Facebook DM Leads tab rather than disappearing, so you can
            still see your position while swiping - it just also shows
            up in the new Bad Leads tab.

The 'screened' column is new — this script adds it automatically the
first time it runs (an additive ALTER TABLE, never touches or removes
any existing data or column).

REQUIREMENTS:
    Just playwright — same one your scraper already uses. pynput is
    NOT needed anymore; you can `pip uninstall pynput` if you want.

--- WINDOW FRAMING (new) ---

The browser window now launches in Chromium's "app mode"
(--app=<url>) instead of a normal browser window — this hides the
address bar, tabs, and bookmarks bar, so it reads as part of
ScrapeSystems rather than a raw Chrome window. The window title bar
is also set dynamically to show which lead you're currently
reviewing (e.g. "ScrapeSystems — Bow Wow Grooming").

This required switching from launch() + new_page() to
launch_persistent_context() — app mode ties itself to the window a
browser is FIRST launched with, so the same window needs to be reused
for the whole session (which also matches how swipe_tool.py already
worked — one continuous session, not a fresh window per lead) rather
than opening a separate page afterward.

NOTE: app mode is a real, well-known Chromium flag, but it's not a
setup Playwright is specifically designed around — if anything about
the window behaves unexpectedly (sizing, focus, closing), that's the
first place to look. Worth confirming everything still works
end-to-end after this change before relying on it daily.

--- WINDOW ICON + LAUNCH DETECTION (new) ---

The window's icon (shown in the title bar, taskbar, and alt-tab
switcher) is now overridden to the ScrapeSystems logo instead of
whatever favicon Facebook's page itself declares — done by rewriting
the page's <link rel="icon"> tags after each load rather than trying
to intercept network requests, since Facebook's actual favicon URLs
aren't predictable enough to reliably block.

This also now writes a small status file
(~/.scrapesystems/swipe_session_status.json) the moment the FIRST
lead's window is actually up and ready to use — not just when the
process starts, which can be a second or two earlier than the window
actually being usable. swipe_launcher.py reads this file so Lovable
can show a loading state that clears at the right moment instead of
the instant the button is clicked.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

DB_PATH = "leads.db"

# Persistent Chrome profile directory for the app-mode window — using
# launch_persistent_context (rather than launch() + new_page())
# requires a user_data_dir. This also means cookies persist between
# runs, as a side benefit, though this tool still runs anonymously
# (no Facebook login) unless that's set up separately.
PROFILE_DIR = Path.home() / ".scrapesystems" / "swipe_tool_profile"

# Written the moment the first lead's window is actually ready (see
# _finish_loading_current), and removed when the session ends —
# swipe_launcher.py's /swipe/status reads this so Lovable knows the
# real moment to stop showing a loading state, not just "the process
# exists" (which can be a second or two before the window is usable).
STATUS_FILE = Path.home() / ".scrapesystems" / "swipe_session_status.json"

WINDOW_SIZE = (1300, 900)
WINDOW_POSITION = (100, 100)

# The ScrapeSystems logo, embedded as base64 so this file has no
# external image dependency — used to override the window's icon
# (title bar / taskbar / alt-tab) after each page load, since
# Facebook's own favicon would otherwise show there instead.
_FAVICON_BASE64 = (
    "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"
)

_SET_ICON_JS = """
(iconDataUri) => {
    try {
        document.querySelectorAll(
            'link[rel="icon"], link[rel="shortcut icon"], link[rel="apple-touch-icon"]'
        ).forEach((el) => el.remove());

        const link = document.createElement('link');
        link.rel = 'icon';
        link.type = 'image/png';
        link.href = iconDataUri;
        document.head.appendChild(link);
        return 'ok';
    } catch (err) {
        return 'error: ' + err.message;
    }
}
"""

# Registered as an init script so it's in place before ANY of a page's
# own JS runs, including on Facebook's very first load - this is what
# makes the arrow keys work even before you can see the overlay.
_LISTENER_JS = """
() => {
    window.addEventListener('keydown', (e) => {
        // e.isTrusted is false for any keydown event a PAGE'S OWN
        // JavaScript fires programmatically (e.g. a photo carousel
        // simulating arrow-key navigation internally) - only true for
        // an event that genuinely came from your keyboard. Without
        // this check, some pages' own internal key-simulation was
        // getting misread as real swipe decisions and auto-marking
        // leads bad with no one touching a key.
        if (!e.isTrusted) return;
        if (e.key === 'ArrowRight') { window.reportSwipeKey('good'); }
        else if (e.key === 'ArrowLeft') { window.reportSwipeKey('bad'); }
        else if (e.key === 'Escape') { window.reportSwipeKey('quit'); }
    }, true);
}
"""

# Builds the overlay. Registered as an init script too (so it appears
# as early as possible), AND re-run manually after each page finishes
# loading (see _load_current below) - some sites' own JS (Facebook
# included) can rebuild large parts of the page after the initial
# load, which can wipe out anything added too early. Running it again
# post-load is what makes it actually stick. Removes any old copy
# first so re-running it never creates duplicates.
_BUILD_OVERLAY_JS = """
() => {
    try {
        const old = document.getElementById('__swipe_overlay');
        if (old) old.remove();

        // Non-visual wrapper — its two children below are each
        // independently `position: fixed`, so they place themselves
        // relative to the viewport regardless of this wrapper.
        const wrapper = document.createElement('div');
        wrapper.id = '__swipe_overlay';

        // Top-left: Close button — ends the whole session, same as
        // pressing Esc.
        const closeBtn = document.createElement('button');
        closeBtn.textContent = 'Close';
        closeBtn.style.cssText = `
            position: fixed; top: 12px; left: 12px; z-index: 2147483647;
            padding: 8px 16px; border: none; border-radius: 20px;
            font: bold 13px/1 -apple-system, sans-serif; cursor: pointer;
            background: #272726; color: #fff;
            box-shadow: 0 2px 10px rgba(0,0,0,0.4);
        `;
        closeBtn.addEventListener('click', () => window.reportSwipeKey('quit'));
        wrapper.appendChild(closeBtn);

        // Top-right: Bad Lead / Good Lead buttons.
        const box = document.createElement('div');
        box.style.cssText = `
            position: fixed; top: 12px; right: 12px; z-index: 2147483647;
            display: flex; gap: 8px; align-items: center;
        `;

        const makeButton = (label, color, key) => {
            const btn = document.createElement('button');
            btn.textContent = label;
            btn.style.cssText = `
                padding: 8px 18px; border: none; border-radius: 20px;
                font: bold 13px/1 -apple-system, sans-serif; cursor: pointer;
                background: ${color}; color: #fff;
                box-shadow: 0 2px 10px rgba(0,0,0,0.4);
            `;
            btn.addEventListener('click', () => window.reportSwipeKey(key));
            return btn;
        };

        box.appendChild(makeButton('Bad Lead', '#272726', 'bad'));
        box.appendChild(makeButton('Good Lead', '#0057FD', 'good'));
        wrapper.appendChild(box);

        (document.body || document.documentElement).appendChild(wrapper);
        return 'ok';
    } catch (err) {
        return 'error: ' + err.message;
    }
}
"""


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


def _load_queue():
    """
    Pulls every lead that: has a Facebook page, hasn't already been
    moved to Cold Call, hasn't been screened by this tool before, and
    is still "Not Contacted" (a lead you've already DMed or otherwise
    updated the status on won't show up in the swipe queue again).
    Same lead set your Streamlit app's "Facebook DM Leads" tab shows,
    minus anything you've already swiped on.
    """
    with _conn() as conn:
        _ensure_screened_column(conn)
        rows = conn.execute(
            """
            SELECT id, business_name, facebook_url FROM leads
            WHERE has_facebook = 1
              AND (move_to_cold_call IS NULL OR move_to_cold_call = 0)
              AND (screened IS NULL OR screened = '')
              AND (status IS NULL OR status = '' OR status = 'Not Contacted')
              AND facebook_url IS NOT NULL AND facebook_url != ''
            ORDER BY id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _mark(lead_id, result):
    with _conn() as conn:
        _ensure_screened_column(conn)
        if result == "bad":
            # Deliberately NOT setting move_to_cold_call here anymore.
            # That used to pull bad-marked leads out of the Facebook DM
            # Leads tab entirely, which made it impossible to see where
            # you were in the list while swiping. Now a bad lead just
            # gets stamped 'screened' (so it's skipped in future swipe
            # sessions and shows up in the Bad Leads tab) and its
            # status set to "Bad Lead" (an option the app already has)
            # so it's visually flagged rather than looking identical to
            # an unreviewed lead - but it stays put in DM Leads.
            conn.execute(
                "UPDATE leads SET screened = ?, status = 'Bad Lead' WHERE id = ?",
                (result, lead_id),
            )
        else:
            conn.execute(
                "UPDATE leads SET screened = ? WHERE id = ?",
                (result, lead_id),
            )


class SwipeSession:
    def __init__(self):
        self.queue = _load_queue()
        self.index = 0
        self.page = None
        self.context = None
        self.playwright = None
        self.done = False
        self._pending = None
        self._last_decision_time = 0.0
        # Circuit breaker: consecutive load failures (NOT decisions).
        # If loading pages keeps failing (network blip, Facebook
        # blocking, dead browser context, etc.), stop the whole
        # session after this many in a row rather than silently
        # skipping through the entire remaining queue.
        self._consecutive_load_failures = 0
        self.MAX_CONSECUTIVE_LOAD_FAILURES = 5

    def start(self):
        if not self.queue:
            print("No unscreened leads with a Facebook page right now. Nothing to swipe.")
            self.done = True
            return
        print(f"Starting swipe session — {len(self.queue)} lead(s) queued.")

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        first_url = self.queue[0]["facebook_url"]

        self.playwright = sync_playwright().start()

        # launch_persistent_context (rather than launch() + new_page())
        # so the --app window we open here is the SAME window reused
        # for the whole session — app mode ties itself to the window
        # a browser is first launched with, so this has to be one
        # continuous context, not a fresh page/window per lead.
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": WINDOW_SIZE[0], "height": WINDOW_SIZE[1]},
            args=[
                f"--app={first_url}",
                f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
                f"--window-position={WINDOW_POSITION[0]},{WINDOW_POSITION[1]}",
            ],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

        # Surfaces JS errors and console.error() calls from inside the
        # page in THIS terminal - if the overlay ever fails to build
        # again, this is what tells us why instead of failing silently.
        self.page.on(
            "console",
            lambda msg: print(f"  [browser console] {msg.text}") if msg.type == "error" else None,
        )
        self.page.on("pageerror", lambda exc: print(f"  [browser error] {exc}"))

        # Registers the key listener as early as possible on every
        # navigation (critical so keys work even before you can see
        # anything). The overlay-builder is also registered here so it
        # runs at the earliest possible moment too, but it's rebuilt
        # again after each load finishes (see _load_current) since
        # Facebook's own JS can wipe out things added this early.
        self.page.add_init_script(_LISTENER_JS)
        self.page.add_init_script(_BUILD_OVERLAY_JS)
        self.page.expose_function("reportSwipeKey", self._on_key)

        # If the person just closes the window themselves instead of
        # pressing Esc, exit cleanly instead of hanging.
        self.page.on("close", lambda: setattr(self, "done", True))

        # The --app launch already navigated the window to the first
        # lead's URL directly (that's what --app=<url> does), so
        # _load_current for lead 0 just needs to finish setting it up
        # (title, overlay) rather than navigating again.
        self._finish_loading_current(already_navigated=True)

    def _on_key(self, result):
        # IMPORTANT: this callback fires from INSIDE Playwright's
        # internal dispatch, itself triggered while run()'s loop below
        # is mid-wait. Calling page.goto() directly from here is
        # unreliable - it can silently misbehave (the DB update lands,
        # but the visible browser window doesn't actually navigate,
        # which is exactly what you saw happen). So this callback does
        # nothing but record the click; run()'s loop is what actually
        # acts on it, safely outside that nested context.
        self._pending = result

    def _set_window_title(self, business_name: str):
        """Branded title bar text — visible even in app mode, since
        app mode hides the address bar/tabs but still shows the
        page's document.title in the window's title bar."""
        try:
            self.page.evaluate(
                "(name) => { document.title = 'ScrapeSystems — ' + name; }",
                business_name,
            )
        except Exception as e:
            print(f"  [title] couldn't set window title: {e}")

    def _set_window_icon(self):
        """Overrides Facebook's own favicon with the ScrapeSystems
        logo, so the icon shown in the title bar/taskbar/alt-tab
        switcher matches the product instead of whatever page is
        currently loaded."""
        try:
            icon_data_uri = f"data:image/png;base64,{_FAVICON_BASE64}"
            result = self.page.evaluate(_SET_ICON_JS, icon_data_uri)
            if result != "ok":
                print(f"  [icon] {result}")
        except Exception as e:
            print(f"  [icon] couldn't set window icon: {e}")

    def _mark_window_ready(self):
        """Writes STATUS_FILE the moment the first lead's window is
        actually up and usable — this is what swipe_launcher.py's
        /swipe/status checks so Lovable can clear its loading state
        at the right moment, not just when the process starts."""
        try:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATUS_FILE.write_text(json.dumps({"window_ready": True}))
        except Exception as e:
            print(f"  [status] couldn't write ready status: {e}")

    def _clear_window_ready(self):
        try:
            if STATUS_FILE.exists():
                STATUS_FILE.unlink()
        except Exception:
            pass

    def _finish_loading_current(self, already_navigated: bool = False):
        """Shared tail end of loading a lead: set the branded title
        and rebuild the overlay. Split out from _load_current so the
        first lead (already navigated to by --app on launch) and every
        lead after it (navigated via goto()) can both reach this
        without navigating twice.

        Runs as a loop (not recursion) so a long run of consecutive
        skips can never hit Python's recursion limit — it just keeps
        advancing the index in place until a lead loads, the queue
        ends, or the failure circuit breaker trips."""
        while True:
            if self.index >= len(self.queue):
                print("Queue finished — no more leads to review right now.")
                self.done = True
                return

            advanced = self._load_one(already_navigated)
            already_navigated = False  # only applies to the very first call
            if self.done:
                return
            if not advanced:
                return  # loaded successfully, or waiting is done
            # advanced == True means: this lead failed to load and we
            # should try the next one, in this same loop (not a new
            # recursive call).

    def _load_one(self, already_navigated: bool) -> bool:
        """Attempts to load self.queue[self.index]. Returns True if it
        failed and the caller should move on to the next lead; False
        if it succeeded (or the session ended)."""
        lead = self.queue[self.index]
        print(f"[{self.index + 1}/{len(self.queue)}] {lead['business_name']}")
        try:
            if not already_navigated:
                self.page.goto(lead["facebook_url"], timeout=20000)
            self._set_window_title(lead["business_name"])
            self._set_window_icon()
            # The init-script copy of the overlay may have already been
            # wiped out by Facebook's own React rendering by this point
            # - rebuilding it now, after things have settled, is what
            # actually makes it stick. Logs the result so it's obvious
            # in the terminal whether this succeeded or not.
            result = self.page.evaluate(_BUILD_OVERLAY_JS)
            if result != "ok":
                print(f"  [overlay] {result}")
            # The window is genuinely up and usable at this point (this
            # is the first lead's load finishing) — mark it ready so
            # swipe_launcher.py's /swipe/status can tell Lovable to
            # clear its loading state.
            if self.index == 0:
                self._mark_window_ready()
            # A successful load resets the failure streak.
            self._consecutive_load_failures = 0
            return False
        except Exception as e:
            # IMPORTANT: a failure to LOAD a page is a technical
            # problem, not a human judgment that the lead is bad. It
            # must never write screened='bad' / status='Bad Lead' on
            # its own — only an explicit human decide("bad") call may
            # do that. This previously auto-marked the lead bad here,
            # which meant a run of unrelated load failures (network
            # blip, Facebook blocking, a stale/corrupt browser
            # profile, etc.) could silently mark every remaining lead
            # in the queue bad with no human involved.
            self._consecutive_load_failures += 1
            print(
                f"  Couldn't load page ({e}) — leaving unreviewed and "
                f"skipping (failure {self._consecutive_load_failures}/"
                f"{self.MAX_CONSECUTIVE_LOAD_FAILURES} in a row)."
            )

            if self._consecutive_load_failures >= self.MAX_CONSECUTIVE_LOAD_FAILURES:
                print(
                    "  Too many consecutive load failures — stopping the "
                    "session instead of continuing to skip leads. "
                    "Nothing further was marked bad. Check your network/"
                    "browser and restart the swipe tool when ready."
                )
                self.done = True
                return False

            self.index += 1
            return True

    def _load_current(self):
        self._finish_loading_current(already_navigated=False)

    def decide(self, result):
        if self.index >= len(self.queue):
            return
        lead = self.queue[self.index]
        _mark(lead["id"], result)
        print(f"  -> {result.upper()}")
        self.index += 1
        self._load_current()

    def run(self):
        self.start()
        # Playwright's sync API needs something pumping its event loop
        # so expose_function callbacks actually fire - a short polling
        # wait does that job. Each time through, check whether a click
        # or key landed in self._pending while we were waiting, and
        # act on it HERE (outside the callback's nested context) -
        # that's what makes goto() actually navigate the visible window
        # reliably every time.
        try:
            while not self.done:
                try:
                    self.page.wait_for_timeout(200)
                except Exception:
                    break
                if self._pending is not None:
                    result, self._pending = self._pending, None
                    if result == "quit":
                        print("Quitting.")
                        self.done = True
                    else:
                        # Second safety net, on top of the isTrusted
                        # filter in the JS listener: refuse to act on
                        # more than one decision within a fifth of a
                        # second. A real click or keypress is never
                        # this fast; anything faster is almost
                        # certainly a runaway/synthetic event rather
                        # than a genuine intentional decision.
                        now = time.monotonic()
                        if now - self._last_decision_time < 0.2:
                            print("  (ignored — too fast to be a real click/keypress)")
                        else:
                            self._last_decision_time = now
                            self.decide(result)
        finally:
            self._clear_window_ready()
            try:
                self.context.close()
                self.playwright.stop()
            except Exception:
                pass


def main():
    print("Swipe tool ready — a browser window will open shortly.")
    print("Look for the small overlay in its top-left corner as confirmation.")
    session = SwipeSession()
    session.run()


if __name__ == "__main__":
    main()
