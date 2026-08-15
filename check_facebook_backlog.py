"""
Re-checks existing leads in your "Sterling Digital Leads" sheet for a
Facebook page, using the Firefox-based method confirmed working today.
Only touches the "Has facebook" and "Facebook URL" columns - every
other column (Call status, Call notes, Email, etc.) is read in and
written back completely untouched, since save_leads() rewrites the
whole sheet.

HOW TO USE:
  1. First run: leave BATCH_SIZE at 50, run this, check the sheet
     looks right.
  2. Once you're happy with it, change BATCH_SIZE to something like
     1000 (bigger than your total lead count) and run again - it will
     automatically skip everything already successfully checked and
     pick up where it left off, so you don't need to track progress
     yourself.

NOTE on re-checking: a lead only counts as "already done" once it has
a real Facebook URL saved. Leads that come back with no Facebook page
found will get re-checked again on the next run too (since there's no
way to tell "confirmed no page" apart from "not checked yet" without
adding a new tracking column) - that's a minor inefficiency, not a
bug, and fine given how fast/reliable checks are now.

NOTE on location: your sheet doesn't store a per-lead location
separately, so this uses "Nova Scotia" for every lead. If you're
scraping leads from outside Nova Scotia later, this will need
updating.
"""

import random
import time

from playwright.sync_api import sync_playwright

from scraper import find_facebook_page, new_facebook_lookup_browser
from sheets import load_leads, save_leads

BATCH_SIZE = 1000
LOCATION = "Nova Scotia"

# Set this True to re-check the first BATCH_SIZE no-website leads
# regardless of whether they already have a Facebook URL saved -
# useful right after a matching-logic fix, to re-verify a batch you
# already ran. Set back to False for normal "pick up where I left
# off" runs.
FORCE_RECHECK = False


def main():
    print("Loading leads from the sheet...")
    df = load_leads()

    has_no_website = df["Has website"].astype(str).str.strip().str.lower() == "false"

    if FORCE_RECHECK:
        candidates = df[has_no_website]
    else:
        not_yet_found = df["Facebook URL"].astype(str).str.strip() == ""
        candidates = df[has_no_website & not_yet_found]

    if candidates.empty:
        print("No leads left to check - every no-website lead already has a Facebook URL saved (or none exist).")
        return

    batch = candidates.head(BATCH_SIZE)
    print(f"{len(candidates)} leads still need checking. Checking {len(batch)} this run.\n")

    found_count = 0
    not_found_count = 0

    with sync_playwright() as p:
        browser = new_facebook_lookup_browser(p)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        for count, (idx, row) in enumerate(batch.iterrows(), start=1):
            name = row["Name"]
            print(f"[{count}/{len(batch)}] Checking: {name}")

            fb_url = find_facebook_page(page, name, LOCATION)

            df.at[idx, "Has facebook"] = str(bool(fb_url))
            df.at[idx, "Facebook URL"] = fb_url or ""

            if fb_url:
                found_count += 1
                print(f"  -> FOUND: {fb_url}\n")
            else:
                not_found_count += 1
                print(f"  -> not found / unknown\n")

            # Slower, more human-looking pacing - same reasoning as the
            # main scraper: a random gap after every lookup, plus a
            # longer pause every 15 businesses to break up the rhythm.
            if count % 15 == 0:
                time.sleep(random.uniform(45, 90))
            else:
                time.sleep(random.uniform(5.0, 12.0))

        browser.close()

    print(f"Saving results back to the sheet ({found_count} found, {not_found_count} not found/unknown this run)...")
    save_leads(df)
    print("Done.")


if __name__ == "__main__":
    main()
