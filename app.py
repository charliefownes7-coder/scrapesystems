"""
ScrapeSystems - internal cold-call lead tracker (v1)

Run with: streamlit run app.py
"""

import hashlib
import random
import time

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from playwright.sync_api import sync_playwright

from scraper import find_facebook_page, new_facebook_lookup_browser, run_scrape
from db_store import load_leads, save_leads, REQUIRED_COLUMNS

# Leads don't have a per-row location stored, so this is used as a
# consistent hint for the Facebook search - matches what
# check_facebook_backlog.py uses for the same reason.
FACEBOOK_LOOKUP_LOCATION = "Nova Scotia"

# A realistic "typical" scroll count for the Maps progress bar's fill
# percentage - not the same as the 400-scroll safety cap in scraper.py
# (that stays untouched). Most searches finish well under this, which
# is fine - the bar just fills faster than it "should" for a small
# search and slower for a big one, capped at 95% either way so it
# never claims to be further along than it actually is.
TYPICAL_SCROLL_ESTIMATE = 40

st.set_page_config(
    page_title="ScrapeSystems",
    page_icon="favicon.png",
    layout="wide",
)

# Custom progress bar (blue, animated diagonal stripes, black track)
# built from scratch as plain HTML/CSS. st.progress() doesn't expose
# what's needed to reliably reskin its internals - repeatedly guessing
# at its DOM structure wasn't working, so this sidesteps that
# entirely: full control over every pixel, nothing to guess at.
def _progress_bar_html(pct):
    pct = max(0, min(100, pct))
    return f"""
    <div style="background:#000000; border-radius:6px; overflow:hidden;
                height:14px; width:100%;">
      <div style="width:{pct}%; height:100%; background-color:#0057FD;
                  background-image:linear-gradient(135deg,
                      rgba(255,255,255,0.28) 25%, transparent 25%,
                      transparent 50%, rgba(255,255,255,0.28) 50%,
                      rgba(255,255,255,0.28) 75%, transparent 75%,
                      transparent);
                  background-size:36px 36px;
                  animation:ss-stripes 0.9s linear infinite;
                  transition:width 0.25s ease;">
      </div>
    </div>
    <style>
    @keyframes ss-stripes {{
        from {{ background-position: 36px 0; }}
        to   {{ background-position: 0 0; }}
    }}
    </style>
    """

st.title("ScrapeSystems")
st.caption("Internal lead tracker")

# Restores scroll position after any rerun (editing a cell, switching
# tabs, etc. all cause Streamlit to redraw the whole page, which is
# most of what was actually snapping you back to the top - not just
# literal browser refreshes) plus genuine page reloads. components.html
# renders inside a sandboxed iframe, so this reaches out to
# window.parent to read/restore the actual page's scroll position, not
# the iframe's own (which is always empty/invisible here).
components.html(
    """
    <script>
    (function() {
        const KEY = "scrapesystems_scroll_pos";
        try {
            const win = window.parent;
            const storage = win.sessionStorage;

            const saved = storage.getItem(KEY);
            if (saved !== null) {
                setTimeout(function() {
                    win.scrollTo(0, parseInt(saved, 10));
                }, 100);
            }

            if (!win.__ssScrollListenerAttached) {
                win.__ssScrollListenerAttached = true;
                let ticking = false;
                win.addEventListener('scroll', function() {
                    if (!ticking) {
                        win.requestAnimationFrame(function() {
                            storage.setItem(KEY, win.scrollY);
                            ticking = false;
                        });
                        ticking = true;
                    }
                });
            }
        } catch (e) {
            console.warn('Scroll restore failed:', e);
        }
    })();
    </script>
    """,
    height=0,
)

# Cold-call-specific statuses (phone outreach) - kept exactly as
# before, values unchanged, so nothing already saved in the sheet
# stops matching a dropdown option.
CALL_STATUS_OPTIONS = [
    "Not Contacted", "Did not answer", "Not interested",
    "Answered", "Bot answered", "Could not contact", "Has website",
]

# DM-specific statuses (Facebook outreach) - separate from cold call
# since "Did not answer" / "Could not contact" don't really describe
# a DM. These match the actual DM flow: sent -> no reply yet, or they
# reply interested -> gets a call booked from there.
DM_STATUS_OPTIONS = [
    "Not Contacted", "DM Sent", "No Response", "Replied - Interested",
    "Not Interested", "Bad Lead", "Bot Answered", "Call Booked", "Has website",
]

# "All" tab (and the top status filter) mixes both segments, so its
# dropdown needs to accept values from either list without showing
# blank for leads that already have a status set from one side or
# the other. dict.fromkeys() dedupes while keeping order.
ALL_STATUS_OPTIONS = list(dict.fromkeys(CALL_STATUS_OPTIONS + DM_STATUS_OPTIONS))

if "leads" not in st.session_state:
    st.session_state.leads = load_leads()
    st.session_state["_last_synced"] = st.session_state.leads.copy()

# Bumped whenever leads are changed by something OTHER than an
# in-place edit inside the table itself (a new scrape, a Facebook
# check, "Move to Cold Call") - i.e. whenever the table's underlying
# ROW SET or unedited cell values genuinely changed and the grid needs
# to rebuild from scratch. A normal cell edit does NOT bump this,
# which is what keeps the grid's scroll position intact on ordinary
# status changes - see _render_editor's base-view caching below.
if "_leads_generation" not in st.session_state:
    st.session_state["_leads_generation"] = 0

# A widget's session_state value can't be changed after that widget
# has already been created in the same run - so clearing the form
# after a scrape (below) sets this flag and reruns, and the actual
# clearing happens here, before the niche/location widgets exist yet
# for this run.
if st.session_state.get("_clear_scrape_form"):
    st.session_state["niche_input"] = ""
    st.session_state["location_input"] = ""
    st.session_state["_clear_scrape_form"] = False


def _categorize(df):
    """Split leads into (cold_call, dm) segments.

    A lead is a Cold Call lead if it has no Facebook page, OR it's
    been manually flagged "Move to Cold Call" (used for Facebook
    pages that look inactive/abandoned and aren't worth DMing).
    Everything else with a Facebook page is a DM lead. Every lead
    lands in exactly one segment, never both.
    """
    if "Has facebook" in df.columns:
        has_fb = df["Has facebook"].astype(str) == "True"
    else:
        has_fb = pd.Series(False, index=df.index)

    if "Move to Cold Call" in df.columns:
        overridden = df["Move to Cold Call"].astype(str) == "True"
    else:
        overridden = pd.Series(False, index=df.index)

    dm_mask = has_fb & ~overridden
    return df[~dm_mask], df[dm_mask]


def _segment_stats(segment_df, exclude_from_contacted=None, exclude_entirely=None):
    """Total/Not Contacted/Contacted counts for a lead segment - always
    computed from the FULL segment (unaffected by the status filter
    below), so these numbers stay a stable overview regardless of what
    the table happens to be filtered to.

    exclude_entirely: statuses dropped from ALL THREE counts (Total,
    Not Contacted, Contacted) - e.g. "Bad Lead" shouldn't count toward
    Total DM Leads at all, even though the lead still shows in the
    table so you can still see/revert it.

    exclude_from_contacted: statuses that count toward Total but not
    toward "Contacted"/"Dials"/"DMs Sent" specifically."""
    total_df = segment_df
    if exclude_entirely:
        total_df = total_df[~total_df["Call status"].isin(exclude_entirely)]

    total = len(total_df)
    if not total:
        return 0, 0, 0
    not_contacted = (total_df["Call status"] == "Not Contacted").sum()
    exclude = set(exclude_from_contacted or []) | {"Not Contacted"}
    contacted = (~total_df["Call status"].isin(exclude)).sum()
    return total, not_contacted, contacted


# --- Run a new scrape ---
with st.expander("Run a new scrape", expanded=True):
    col1, col2, col3 = st.columns([2, 2, 1])
    niche = col1.text_input("Niche", placeholder="e.g. roofing", key="niche_input")
    location = col2.text_input("Location", placeholder="e.g. Sydney, NS", key="location_input")
    run_clicked = col3.button("Run Scrape", width="stretch")

    if run_clicked:
        # Plain, always-fully-visible text + bar - deliberately not
        # st.status(), which is a collapsible widget by nature and
        # tends to remember its last collapsed state across reruns
        # even when expanded=True is passed again.
        status_placeholder = st.empty()
        progress_placeholder = st.empty()
        status_placeholder.markdown("Scraping Google Maps for leads...")
        progress_placeholder.markdown(_progress_bar_html(0), unsafe_allow_html=True)

        def _update_progress(current, total, name):
            pct = int(current / total * 100) if total else 0
            progress_placeholder.markdown(_progress_bar_html(pct), unsafe_allow_html=True)
            lead_number = min(int(current) + 1, total)
            status_placeholder.markdown(f"Checking Facebook pages... ({lead_number}/{total}) — {name}")

        def _update_scroll_progress(current, total, cards_found):
            # "total" here is the 400-scroll safety cap, not a
            # realistic estimate - most searches finish in ~20-40
            # scrolls, so using it directly made the bar crawl to
            # single digits and never look like it was going
            # anywhere. TYPICAL_SCROLL_ESTIMATE gives a far more
            # representative fill, capped below 100 since we can't
            # know the true finish point in advance - it hands off
            # to the (exact) Facebook-checking percentage right
            # after anyway.
            pct = min(int(current / TYPICAL_SCROLL_ESTIMATE * 100), 95)
            progress_placeholder.markdown(_progress_bar_html(pct), unsafe_allow_html=True)
            status_placeholder.markdown(f"Scraping Google Maps for leads... ({cards_found} cards found so far)")

        new_leads = run_scrape(
            niche, location,
            on_progress=_update_progress,
            on_scroll_progress=_update_scroll_progress,
        )
        status_placeholder.empty()
        progress_placeholder.empty()

        new_df = pd.DataFrame(new_leads)
        new_df["Call status"] = "Not Contacted"
        new_df["Call notes"] = ""
        for col in REQUIRED_COLUMNS:
            if col not in new_df.columns:
                new_df[col] = ""
        new_df = new_df[REQUIRED_COLUMNS]

        # Break down the new batch before it gets folded into the
        # existing leads - same has-facebook / moved-to-cold-call
        # logic as the tabs below, just computed on new_df alone so
        # the summary reflects only what THIS scrape just added.
        new_cold_call, new_dm = _categorize(new_df)

        before = len(st.session_state.leads)
        st.session_state.leads = pd.concat(
            [st.session_state.leads, new_df], ignore_index=True
        ).drop_duplicates(subset=["Name", "Adress"], keep="first")
        st.session_state["_leads_generation"] += 1
        after = len(st.session_state.leads)
        duplicates_skipped = len(new_df) - (after - before)

        summary = (
            f"Added {after - before} leads — "
            f"{len(new_cold_call)} to Cold Call, {len(new_dm)} to Facebook DM"
        )
        if duplicates_skipped:
            summary += f" ({duplicates_skipped} duplicate(s) skipped)"
        st.toast(summary)

        # st.toast() survives one rerun, so this clears the form
        # immediately without losing the toast notification.
        st.session_state["_clear_scrape_form"] = True
        st.rerun()

st.divider()

# --- Filters (apply to whichever tab you're looking at) ---
status_filter = st.multiselect("Filter by status", ALL_STATUS_OPTIONS)


def _apply_common_filters(df):
    view = df.copy()
    if status_filter:
        view = view[view["Call status"].isin(status_filter)]
    return view


def _make_commit_callback(editor_key, original_index):
    """
    Fired the instant an edit is actually committed by the browser -
    this is Streamlit's real edit-commit event, not the editor's return
    value (which was lagging a full interaction behind, causing the
    "have to do it twice" bug). Runs BEFORE the script reruns, so by
    the time the save-check at the bottom of the script runs, the edit
    is already sitting in st.session_state.leads and gets saved on
    this same interaction - no second click needed.
    """
    def _on_commit():
        state = st.session_state.get(editor_key)
        if not state:
            return
        for row_pos_str, changes in state.get("edited_rows", {}).items():
            row_pos = int(row_pos_str)
            if row_pos >= len(original_index):
                continue  # a freshly-added row via dynamic rows, not a real lead yet
            orig_idx = original_index[row_pos]
            for col, new_val in changes.items():
                if col in ("Select", "#"):
                    continue
                st.session_state.leads.loc[orig_idx, col] = new_val
    return _on_commit


def _render_editor(df, key, status_label="Call status", status_options=None, column_priority=None):
    # Streamlit silently ignores hide_index=True whenever the table's
    # real index has gaps (which it does here, since this is a
    # filtered view of the full lead list) combined with
    # num_rows="dynamic" - that's what was causing the extra raw-index
    # column to show up alongside our clean "#" column. Giving it a
    # clean 0..N-1 index makes hide_index actually work, but we need
    # to remember the real original index so edits still save back to
    # the correct rows afterward.
    view = df.copy()
    original_index = view.index
    view = view.reset_index(drop=True)
    view.insert(0, "#", range(1, len(view) + 1))
    view.insert(0, "Select", False)

    # Column display order: "Select" and "#" always lead (unchanged),
    # then any columns named in column_priority in that exact order,
    # then whatever's left over in its original sheet order. "Move to
    # Cold Call" always stays hidden either way - it's set via the
    # button below, not edited in-line.
    fixed = ["Select", "#"]
    remaining = [c for c in view.columns if c not in fixed and c != "Move to Cold Call"]
    if column_priority:
        ordered_rest = [c for c in column_priority if c in remaining]
        ordered_rest += [c for c in remaining if c not in ordered_rest]
    else:
        ordered_rest = remaining
    visible_columns = fixed + ordered_rest

    column_config = {
        "Select": st.column_config.CheckboxColumn(width="small"),
        "#": st.column_config.NumberColumn(disabled=True, width="small"),
        "Call status": st.column_config.SelectboxColumn(
            status_label, options=status_options or CALL_STATUS_OPTIONS
        ),
        "Facebook URL": st.column_config.LinkColumn(display_text="Open ↗"),
        "Maps URL": st.column_config.LinkColumn(display_text="Open ↗"),
    }

    # st.data_editor has no native on_select (that's st.dataframe-only),
    # so row selection is done the standard workaround way: a plain
    # checkbox column read back out of the edited frame.
    #
    # The widget key is a fingerprint of which ROWS are in this view
    # (their original indices, in order) - deliberately NOT their cell
    # values. Keying on full content caused the table to fully rebuild
    # itself the instant you edited a cell (since editing a value
    # changes that content's fingerprint too), which is what was
    # wiping out your change as it landed. Keying on row identity
    # instead only forces a fresh widget when the actual SET of leads
    # in this tab changes - a lead moving between Cold Call and DM,
    # or being added/removed - which is the specific case that risked
    # a stale widget misapplying an old edit to the wrong row. A
    # normal value edit like a status change no longer touches the
    # key at all, so the widget stays mounted and the edit sticks.
    row_identity = ",".join(str(i) for i in original_index)
    identity_hash = hashlib.md5(row_identity.encode()).hexdigest()[:10]
    editor_key = f"{key}_{identity_hash}"

    # The actual scroll-reset fix: only feed the grid a FRESH copy of
    # the data when something outside this edit genuinely changed the
    # row set or its values (tracked via _leads_generation). On an
    # ordinary edit-triggered rerun, generation hasn't moved, so we
    # keep reusing the exact same cached table object instead of
    # handing the grid what looks like "new" data on every keystroke -
    # that's what was resetting its scroll position back to the top.
    base_cache_key = f"_editor_base_{key}_{identity_hash}"
    generation = st.session_state["_leads_generation"]
    cached = st.session_state.get(base_cache_key)
    if cached is None or cached["generation"] != generation:
        st.session_state[base_cache_key] = {"generation": generation, "view": view}
    display_view = st.session_state[base_cache_key]["view"]

    edited = st.data_editor(
        display_view,
        column_order=visible_columns,
        column_config=column_config,
        hide_index=True,
        width="stretch",
        num_rows="dynamic",
        key=editor_key,
        on_change=_make_commit_callback(editor_key, original_index),
    )

    # Rows the user ticked "Select" on, restricted to real (not
    # freshly-added-via-dynamic-rows) rows so we can map them back to
    # their real original index.
    selected = edited[edited["Select"]]
    selected = selected[selected.index < len(original_index)]

    to_save = edited.drop(columns=["#", "Select"], errors="ignore")
    n = min(len(to_save), len(original_index))
    to_save = to_save.iloc[:n].copy()
    to_save.index = original_index[:n]
    st.session_state.leads.update(to_save)

    _, btn_col = st.columns([4, 1])
    with btn_col:
        move_clicked = st.button(
            "Move to Cold Call", key=f"move_{key}", width="stretch"
        )

    if move_clicked:
        if len(selected):
            sel_original_idx = original_index[selected.index]
            st.session_state.leads.loc[sel_original_idx, "Move to Cold Call"] = "True"
            st.session_state["_leads_generation"] += 1
            st.success(f"Moved {len(sel_original_idx)} lead(s) to Cold Call.")
            st.rerun()
        else:
            st.warning("Select at least one row first.")


tab_all, tab_call, tab_dm = st.tabs(["All", "Cold Call Leads", "Facebook DM Leads"])

with tab_all:
    metric_placeholder = st.empty()

    _render_editor(
        _apply_common_filters(st.session_state.leads),
        key="editor_all",
        status_options=ALL_STATUS_OPTIONS,
    )

    # Computed AFTER the editor call (which already applied any edit to
    # st.session_state.leads), so these numbers reflect this run's edit
    # immediately instead of lagging one rerun behind.
    total = len(st.session_state.leads)
    not_contacted = (
        (st.session_state.leads["Call status"] == "Not Contacted").sum() if total else 0
    )
    with metric_placeholder.container():
        g1, g2 = st.columns(2)
        g1.metric("Total Leads", total)
        g2.metric("Not Contacted", not_contacted)

with tab_call:
    metric_placeholder = st.empty()

    full_cold_call, _ = _categorize(st.session_state.leads)
    call_view = _apply_common_filters(full_cold_call)
    st.caption("No Facebook page found, or manually moved from DM — call these.")

    to_check = call_view[call_view["Has facebook"].astype(str) == ""]
    if len(to_check):
        if st.button(f"Check Facebook for {len(to_check)} unchecked lead(s)", key="check_fb_button"):
            check_status_placeholder = st.empty()
            check_progress_placeholder = st.empty()
            check_status_placeholder.markdown("Checking leads for a Facebook page...")
            check_progress_placeholder.markdown(_progress_bar_html(0), unsafe_allow_html=True)
            total = len(to_check)
            with sync_playwright() as p:
                browser = new_facebook_lookup_browser(p)
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                for i, (idx, row) in enumerate(to_check.iterrows(), start=1):
                    check_status_placeholder.markdown(f"Checking leads... ({i}/{total}) — {row['Name']}")
                    fb_url = find_facebook_page(page, row["Name"], FACEBOOK_LOOKUP_LOCATION)
                    st.session_state.leads.loc[idx, "Has facebook"] = str(bool(fb_url))
                    st.session_state.leads.loc[idx, "Facebook URL"] = fb_url or ""

                    # Same pacing as the batch script - a random gap
                    # after every lookup, plus a longer pause every
                    # 15 to avoid looking like an automated sweep.
                    # Broken into small steps so the bar ticks
                    # smoothly through the wait instead of sitting
                    # frozen for up to a minute and a half at a time.
                    if i % 15 == 0:
                        pause = random.uniform(45, 90)
                    else:
                        pause = random.uniform(5.0, 12.0)
                    elapsed = 0.0
                    step = 1.0
                    while elapsed < pause:
                        wait = min(step, pause - elapsed)
                        time.sleep(wait)
                        elapsed += wait
                        frac = elapsed / pause if pause else 1
                        pct = min(int((i - 1 + frac) / total * 100), 100)
                        check_progress_placeholder.markdown(_progress_bar_html(pct), unsafe_allow_html=True)
                browser.close()
            check_status_placeholder.empty()
            check_progress_placeholder.empty()
            st.session_state["_leads_generation"] += 1
            st.toast("Done — matches will show up under Facebook DM Leads")
            st.rerun()

    _render_editor(
        call_view,
        key="editor_call",
        status_options=CALL_STATUS_OPTIONS,
        column_priority=["Name", "Category", "Adress", "Phone", "Call status", "Maps URL"],
    )

    # Recomputed AFTER the editor call so an edit this run shows up in
    # the metrics immediately, not one rerun late.
    full_cold_call_after, _ = _categorize(st.session_state.leads)
    cc_total, cc_not_contacted, cc_dials = _segment_stats(full_cold_call_after)
    with metric_placeholder.container():
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Total Cold Call Leads", cc_total)
        cc2.metric("Not Contacted", cc_not_contacted)
        cc3.metric("Dials", cc_dials)

with tab_dm:
    metric_placeholder = st.empty()

    _, full_dm = _categorize(st.session_state.leads)
    dm_view = _apply_common_filters(full_dm)
    st.caption(
        "Has an active Facebook page — DM these. Select a row and hit "
        '"Move to Cold Call" for pages that look inactive, haven\'t '
        'posted recently, or are unlikely to respond. Mark a lead '
        '"Bad Lead" to flag it without moving it to Cold Call right away.'
    )
    _render_editor(
        dm_view,
        key="editor_dm",
        status_label="DM Status",
        status_options=DM_STATUS_OPTIONS,
        column_priority=["Name", "Category", "Facebook URL", "Call status"],
    )

    # Recomputed AFTER the editor call so DMs Sent updates the instant
    # you change a status, same run, no lag. "Bad Lead" is excluded so
    # flagging a lead doesn't inflate this count.
    _, full_dm_after = _categorize(st.session_state.leads)
    dm_total, dm_not_contacted, dm_sent = _segment_stats(
        full_dm_after, exclude_entirely=["Bad Lead"]
    )
    with metric_placeholder.container():
        dm1, dm2, dm3 = st.columns(3)
        dm1.metric("Total DM Leads", dm_total)
        dm2.metric("Not Contacted", dm_not_contacted)
        dm3.metric("DMs Sent", dm_sent)

# Compared as strings rather than with .equals() directly - .equals()
# also checks column dtypes, not just values, and a dtype could
# plausibly shift slightly during st.data_editor's browser round-trip
# even when the actual value didn't change in any way that matters.
# Comparing as strings sidesteps that entirely.
leads_now = st.session_state.leads.astype(str)
last_synced_now = st.session_state["_last_synced"].astype(str)

if not leads_now.equals(last_synced_now):
    try:
        print(f"Saving {len(st.session_state.leads)} leads to leads.db...")
        save_leads(st.session_state.leads)
        print("Save complete.")
        # _last_synced only updates on a CONFIRMED successful save - if
        # save_leads() raises, this line is skipped, so the mismatch
        # is still there on the next rerun and it retries automatically
        # instead of quietly marking a failed save as done.
        st.session_state["_last_synced"] = st.session_state.leads.copy()
        st.toast("Saved")
    except Exception as e:
        print(f"Save FAILED: {e}")
        st.error(f"Couldn't save - will retry on your next edit. ({e})")

st.caption("Changes save automatically as you edit.")
