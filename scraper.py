"""
Scraper module - your real Google Maps scraper, wrapped for the app.

This is your original parsing/scrolling logic from test.py, changed to:
  - run headless (no visible browser window)
  - take a single (niche, location) instead of reading searches.txt
  - return leads shaped to match your sheet's real columns
  - skip businesses that already have a website, same as before
"""

import itertools
import random
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

_DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
_BING_URL = "https://www.bing.com/search"
_STARTPAGE_URL = "https://www.startpage.com/sp/search"

# Rotated round-robin across lookups (see _next_engine) so no single
# free search engine sees enough automated volume from this IP to
# start flagging it as a bot - spreading the same total query count
# across three providers instead of hammering just one.
_SEARCH_ENGINES = [
    ("duckduckgo", _DUCKDUCKGO_URL, "q"),
    ("bing", _BING_URL, "q"),
    ("startpage", _STARTPAGE_URL, "query"),
]
_engine_counter = itertools.count()


def _next_engine():
    i = next(_engine_counter) % len(_SEARCH_ENGINES)
    return _SEARCH_ENGINES[i]

# Facebook's own generic pages that should never count as "this business
# has a page" even if they happen to rank first for a weird query.
_NON_BUSINESS_FACEBOOK_PATHS = (
    "facebook.com/policies", "facebook.com/policy", "facebook.com/help",
    "facebook.com/login", "facebook.com/business", "facebook.com/ads",
    "facebook.com/privacy", "facebook.com/legal", "facebook.com/about",
    "facebook.com/marketplace",
)


def print_current_ip():
    """Prints the public IP this machine is currently using, so you can
    tell whether two test runs happened on the same IP or a different
    one (e.g. home WiFi vs. a phone hotspot)."""
    try:
        resp = requests.get("https://api.ipify.org?format=json", timeout=10)
        print(f"  Current public IP: {resp.text.strip()}")
    except requests.RequestException as e:
        print(f"  Couldn't check current IP: {e}")


def _resolve_result_url(href: str) -> str:
    """DuckDuckGo's HTML results sometimes wrap the real destination in
    a redirect link (//duckduckgo.com/l/?uddg=...) rather than linking
    to it directly. Unwrap that so we're checking the actual target
    site, not duckduckgo.com's own domain."""
    if "uddg=" in href:
        query = parse_qs(urlparse(href).query)
        if "uddg" in query:
            return unquote(query["uddg"][0])
    return href


def _is_facebook_link(url: str) -> bool:
    """
    Checks the link's actual domain, not just whether the text
    'facebook.com' appears anywhere in the URL - a naive substring
    check can also match unrelated links that happen to contain that
    text (e.g. inside a query string), which isn't the same as
    actually pointing to facebook.com.
    """
    host = urlparse(url).netloc.lower()
    return host == "facebook.com" or host.endswith(".facebook.com")


_LEADING_ARTICLES = {"the", "a", "an"}

# Accounts confirmed (from real batch results) to repost content about
# many different unrelated businesses - a match landing on one of
# these means someone else mentioned the business, not that this is
# the business's own page. Add to this list as new ones turn up.
_KNOWN_NON_BUSINESS_ACCOUNTS = {
    "provincialonlinephonebook", "jobshopberc", "northeasttrucktrailersales",
    "ellisdoncorporation",
}


def _account_identity_segment(fb_url: str) -> str:
    """
    Pulls out the actual account/page identity from a Facebook URL,
    unwrapping the /p/ and /people/ prefixes Facebook uses for some
    profile/page URL formats (e.g. /p/Name-12345/ -> "Name-12345" is
    the real identity, "p" is just a URL-format marker).
    """
    path = urlparse(fb_url).path.lower()
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""
    if segments[0] in ("p", "people", "profile.php") and len(segments) > 1:
        return segments[1]
    return segments[0]


def _brand_token(business_name: str) -> Optional[str]:
    """
    Pulls out the business name's most distinctive word to sanity-check
    a match against - usually the actual brand/proper-noun part
    (e.g. "Fricker's" or "Darling"), skipping leading articles like
    "The" and very short/generic words (under 4 letters) that are more
    likely to coincidentally match an unrelated business - e.g. "Moo"
    from "Moo's Clues Pet Grooming" also appearing in the unrelated
    "Moo Meadows Farm Dog Grooming".
    """
    words = re.findall(r"[a-zA-Z0-9]+", business_name.lower())
    for w in words:
        if w not in _LEADING_ARTICLES and len(w) >= 4:
            return w
    return words[0] if words else None


def _looks_like_real_match(business_name: str, fb_url: str) -> bool:
    # Reject known third-party directory/aggregator accounts outright,
    # regardless of what text appears elsewhere in the URL - a match
    # here means someone else posted about the business, not that this
    # is the business's own page.
    identity = _account_identity_segment(fb_url)
    if any(known in identity for known in _KNOWN_NON_BUSINESS_ACCOUNTS):
        return False

    token = _brand_token(business_name)
    if not token:
        return True  # nothing distinctive to check against - don't block on it
    # Check the whole path, not just the identity segment - some
    # businesses' own pages post about themselves using their full name
    # in the post's auto-generated URL text even when their page's own
    # handle drops part of the name (e.g. dropping an owner's first name).
    slug = urlparse(fb_url).path.lower()
    return token in slug


def new_facebook_lookup_browser(p):
    """
    Launches a real, headless Firefox browser for Facebook lookups.

    This is the method that actually held up under testing: plain
    requests (even with a genuine Chrome TLS fingerprint faked via
    curl_cffi) kept getting blocked, while real headless Firefox
    succeeded on the first try in a clean side-by-side comparison.
    Chromium-based automation (Playwright's default) goes through CDP
    (Chrome DevTools Protocol), which has its own well-known detection
    signature - Firefox automation doesn't use CDP at all, which
    likely explains the difference. It's not about TLS or headers, it
    was specifically the Chromium automation protocol.

    Pass the `p` object from an open `with sync_playwright() as p:`
    block. Returns the browser - open ONE per run and reuse the same
    page across every lookup so cookies persist, instead of a fresh
    session each time.
    """
    return p.firefox.launch(
        headless=True,
        firefox_user_prefs={
            "dom.webdriver.enabled": False,
            "useAutomationExtension": False,
        },
    )


def find_facebook_page(page, business_name: str, location: str, retries: int = 3) -> Optional[str]:
    """
    Looks for a public Facebook page for a business, using a real
    headless Firefox page (see new_facebook_lookup_browser) navigating
    a search engine's HTML results - confirmed via side-by-side
    testing to get past blocking that both plain requests and a
    TLS-fingerprint-faking HTTP client both hit.

    Rotates across multiple free search engines (see _SEARCH_ENGINES)
    round-robin, one per lookup - spreads the same total query volume
    thin across providers instead of sending it all to one, which is
    what actually attracts a block (a burst of identical-looking
    automated queries), not the total count on its own.

    `page` should be a Playwright Page from an already-open Firefox
    browser context - reuse the same one across the whole run so
    cookies persist across lookups, don't open a fresh browser per
    business.

    Checks the top facebook.com links found anywhere on the results
    page for a real business/profile match, validated two ways: it
    must actually be on facebook.com's domain (not just contain that
    text somewhere in the URL), and the business's own distinctive
    name must appear in the page's URL slug (catches an unrelated
    similarly-named business slipping into the results, which happened
    during testing). Scanning every link on the page rather than one
    engine's specific result-list markup means this keeps working
    the same way regardless of which engine answered this lookup.

    A search engine sometimes responds to automated requests with a
    block/bot-check page instead of real results. That's NOT the same
    as "this business has no Facebook page" - if we treated it that
    way we'd wrongly mark real leads as False. So when that happens,
    we back off, try the NEXT engine in rotation, and retry - instead
    of hammering the same already-suspicious engine again.
    """
    if not business_name:
        return None

    # site:facebook.com restricts results to Facebook's own domain, so we
    # don't need an exact-phrase match on the full Google Maps business
    # name - businesses often brand their FB page differently (dropping
    # a category suffix like "Dog Grooming", different capitalization,
    # etc). Loosening the name match while narrowing the domain finds
    # more real pages, not fewer.
    query = f"site:facebook.com {business_name} {location}"

    for attempt in range(retries + 1):
        engine_name, engine_url, param = _next_engine()
        url = f"{engine_url}?{urlencode({param: query})}"

        try:
            response = page.goto(url, timeout=20000)
            page.wait_for_timeout(random.uniform(1000, 1800))
        except Exception as e:
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"  Facebook lookup failed for {business_name}: {e}")
            return None

        status = response.status if response else None
        soup = BeautifulSoup(page.content(), "html.parser")
        # Engine-agnostic: scan every link on the page rather than one
        # engine's specific result-list CSS class, so adding/rotating
        # engines never needs new markup-specific parsing.
        candidate_links = soup.find_all("a", href=True)

        # A 200 with zero facebook.com links among all of them is
        # normal (genuinely nothing found). Anything else with none
        # found is this engine blocking us, not a real answer.
        fb_candidates = [
            _resolve_result_url(a["href"]) for a in candidate_links
        ]
        fb_candidates = [u for u in fb_candidates if _is_facebook_link(u)]
        looks_blocked = status != 200 and not fb_candidates

        if looks_blocked:
            if attempt < retries:
                wait = 30 * (2 ** attempt)  # 30s, 60s, 120s
                print(f"  [{business_name}] {engine_name} looks blocked (status={status}), waiting {wait}s and trying the next engine")
                time.sleep(wait)
                continue
            print(f"  [{business_name}] STILL BLOCKED after {retries} retries across engines - result unknown, not a confirmed False")
            return None

        if not fb_candidates:
            return None

        for fb_url in fb_candidates[:5]:
            if any(path in fb_url for path in _NON_BUSINESS_FACEBOOK_PATHS):
                continue
            if not _looks_like_real_match(business_name, fb_url):
                continue
            return fb_url

        return None

    return None


def _parse_card(card):
    text = card.inner_text()
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    name = lines[0]

    rating_match = re.search(r"\b([1-5]\.\d)(?:\((\d+)\))?", text)
    rating = rating_match.group(1) if rating_match else None
    review_count = rating_match.group(2) if rating_match and rating_match.group(2) else None

    phone_match = re.search(r"\(\d{3}\)\s?\d{3}-\d{4}", text)
    phone = phone_match.group(0) if phone_match else None

    has_website = "Website" in text

    category = None
    address = None
    if rating_match:
        for i, line in enumerate(lines):
            if line == rating_match.group(0):
                if i + 1 < len(lines):
                    parts = [p.strip() for p in lines[i + 1].split("\u00b7") if p.strip()]
                    if len(parts) >= 2:
                        category, address = parts[0], parts[-1]
                    elif parts:
                        category = parts[0]
                break

    maps_url = None
    links = card.locator("a")
    for i in range(links.count()):
        href = links.nth(i).get_attribute("href")
        if href and "/maps/place/" in href:
            maps_url = href
            break

    return {
        "name": name,
        "category": category,
        "address": address,
        "phone": phone,
        "rating": rating,
        "review_count": review_count,
        "has_website": has_website,
        "maps_url": maps_url,
    }


def _dedupe(businesses):
    deduped = {}
    for b in businesses:
        key = (b["name"], b["address"])
        deduped[key] = b
    return list(deduped.values())


def _scrape_search(page, query, on_scroll_progress=None, should_stop=None):
    url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
    print(f"Searching: {url}")
    try:
        page.goto(url, timeout=30000)
    except Exception as e:
        print(f"  Couldn't load Maps search page: {e}")
        return [], True

    page.wait_for_timeout(4000)

    cards = page.locator('[role="article"]')
    feed = page.locator('div[role="feed"]')

    try:
        feed.wait_for(state="attached", timeout=5000)
    except Exception:
        print(f"  No results feed found for '{query}' (waited 5s) — likely no results, or redirected to a single listing.")
        return [], True

    previous_count = 0
    no_change_count = 0
    max_scrolls = 400
    scroll_step = 800
    interrupted = False

    for i in range(max_scrolls):
        if should_stop and should_stop():
            print("  Stop requested — ending scroll early.")
            break
        if on_scroll_progress:
            on_scroll_progress(i + 1, max_scrolls, previous_count)
        try:
            scroll_state = feed.evaluate(
                "node => ({scroll_top: node.scrollTop, visible_height: node.clientHeight, total_height: node.scrollHeight})"
            )

            at_bottom = (
                scroll_state["scroll_top"] + scroll_state["visible_height"]
                >= scroll_state["total_height"] - 50
            )

            if at_bottom:
                current_count = cards.count()
                if current_count == previous_count:
                    no_change_count += 1
                else:
                    no_change_count = 0
                    print(f"  ...{current_count} cards loaded so far (scroll {i}/{max_scrolls})")
                if no_change_count >= 4:
                    break
                previous_count = current_count
                page.wait_for_timeout(4000)
            else:
                feed.evaluate(f"node => node.scrollTop = node.scrollTop + {scroll_step}")
                page.wait_for_timeout(500)

        except Exception as e:
            print(f"  Stopped scrolling early, couldn't read the results list: {e}")
            interrupted = True
            break

    total = cards.count()
    businesses = []
    for i in range(total):
        card = cards.nth(i)
        parsed = _parse_card(card)
        parsed["search_query"] = query
        businesses.append(parsed)

    deduped = _dedupe(businesses)
    print(f"  {total} cards found, {len(deduped)} unique\n")
    return deduped, interrupted


def scrape_maps(niche: str, location: str, on_scroll_progress=None, should_stop=None) -> List[dict]:
    """
    Runs just the Google Maps stage: searches "{niche} in {location}",
    scrolls to load every result, and returns deduped businesses
    WITHOUT an existing website (pitch-ready leads) - no Facebook
    checking yet, see check_facebook_pages() for that.

    should_stop() is checked between scroll steps, same as before -
    but note that stopping here can't be "resumed" the way
    check_facebook_pages() can, since scroll position lives inside a
    closed browser tab. A caller that stops mid-Maps-stage should plan
    to just re-run this stage from scratch (harmless - your existing
    dedup/upsert logic means re-finding the same businesses doesn't
    create duplicate leads).
    """
    query = f"{niche} in {location}".strip()
    all_businesses = []

    with sync_playwright() as p:
        # Chromium is fine for Google Maps scraping - the blocking
        # problem was specific to DuckDuckGo detecting Chromium's CDP
        # automation protocol, not Chromium itself being unusable.
        # --disable-dev-shm-usage avoids Chromium filling up /dev/shm
        # (often tiny on constrained Linux containers) instead of
        # falling back to disk, which otherwise causes crashes rather
        # than just being slow.
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        # Google Maps result cards load a lot of business photos we
        # never use - blocking images/media/fonts cuts real RAM and
        # bandwidth per browser instance without changing what data
        # gets scraped (only visuals are skipped, not text/links).
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )

        try:
            results, interrupted = _scrape_search(page, query, on_scroll_progress, should_stop=should_stop)
            combined = list(results)

            if interrupted and not (should_stop and should_stop()):
                print(f"  '{query}' stopped early -- retrying once\n")
                page.wait_for_timeout(5000)
                retry_results, retry_interrupted = _scrape_search(page, query, on_scroll_progress, should_stop=should_stop)
                combined = _dedupe(combined + retry_results)

            all_businesses.extend(combined)
        finally:
            browser.close()

    return [b for b in _dedupe(all_businesses) if not b["has_website"]]


def check_facebook_pages(
    businesses: List[dict], location: str,
    on_progress=None, on_lead_found=None, should_stop=None,
    start_index: int = 1, total_count: Optional[int] = None,
) -> Tuple[List[dict], List[dict]]:
    """
    Runs the Facebook-checking stage over a list of businesses (as
    returned by scrape_maps). Returns (leads, remaining) - remaining
    is the sub-list of `businesses` that hadn't been checked yet when
    should_stop() fired (empty if the whole list finished normally),
    so a caller can resume later starting exactly there instead of
    re-checking everything from scratch.

    start_index/total_count only affect DISPLAYED progress numbers
    (via on_progress) - pass the original 1-based position and full
    run size when resuming a previously-stopped run, so progress reads
    e.g. "142/180" instead of restarting at "1/38" for the leftover
    slice. Both default to matching `businesses` for a fresh, unstopped
    run.

    See run_scrape()'s old docstring for what on_progress/on_lead_found/
    should_stop each receive - unchanged from before.
    """
    if total_count is None:
        total_count = len(businesses) + (start_index - 1)

    with sync_playwright() as p:
        # Separate Firefox browser for Facebook lookups - this is the
        # part that was getting blocked under Chromium, confirmed
        # fixed by switching engines. One page reused across the whole
        # run so cookies persist between lookups.
        fb_browser = new_facebook_lookup_browser(p)
        fb_page = fb_browser.new_page(viewport={"width": 1920, "height": 1080})

        leads = []
        remaining: List[dict] = []
        try:
            for local_i, b in enumerate(businesses):
                if should_stop and should_stop():
                    print("  Stop requested — ending Facebook checks early.")
                    remaining = businesses[local_i:]
                    break

                global_i = start_index + local_i
                name = b["name"] or "(unnamed)"
                print(f"  [{global_i}/{total_count}] Checking {name} for Facebook page...")
                if on_progress:
                    on_progress(global_i - 1, total_count, name)

                facebook_url = find_facebook_page(fb_page, b["name"] or "", location)

                # Slower, more human-looking pacing: a random gap after every
                # lookup, plus a longer pause every 15 businesses to break up
                # the steady rhythm that gets flagged as a bot sweep. Slower,
                # but much less likely to get blocked mid-run.
                if global_i % 15 == 0:
                    pause = random.uniform(45, 90)
                else:
                    pause = random.uniform(5.0, 12.0)

                # Broken into small steps (instead of one long time.sleep)
                # purely so progress can tick smoothly through the pause -
                # otherwise the UI sits frozen for up to a minute and a
                # half between per-lead updates, which looks identical to
                # an actual hang. Also lets a stop request land mid-pause
                # instead of waiting out the full delay first.
                elapsed = 0.0
                step = 1.0
                stopped_mid_pause = False
                while elapsed < pause:
                    if should_stop and should_stop():
                        stopped_mid_pause = True
                        break
                    wait = min(step, pause - elapsed)
                    time.sleep(wait)
                    elapsed += wait
                    if on_progress:
                        frac = elapsed / pause if pause else 1
                        on_progress(global_i - 1 + frac, total_count, name)

                lead = {
                    "Name": b["name"] or "",
                    "Category": b["category"] or "",
                    "Adress": b["address"] or "",
                    "Phone": b["phone"] or "",
                    "Rating": b["rating"] or "",
                    "Reveiw count": b["review_count"] or "",
                    "Has website": str(b["has_website"]),
                    "Has facebook": str(bool(facebook_url)),
                    "Facebook URL": facebook_url or "",
                    "Maps URL": b["maps_url"] or "",
                    "Search query": b["search_query"] or "",
                }
                leads.append(lead)
                if on_lead_found:
                    on_lead_found(lead)

                if stopped_mid_pause:
                    # This business's own lead is done and already
                    # counted above - only what comes AFTER it is
                    # actually "remaining".
                    remaining = businesses[local_i + 1:]
                    break
            else:
                remaining = []
        finally:
            fb_browser.close()

    return leads, remaining


def run_scrape(niche: str, location: str, on_progress=None, on_scroll_progress=None, on_lead_found=None, should_stop=None) -> List[dict]:
    """
    Convenience wrapper: runs scrape_maps() then check_facebook_pages()
    back to back and returns just the leads, discarding resume info.
    Used by callers (e.g. agent.py's overnight runs) that don't need
    stop/resume - they have their own per-job retry logic instead.
    For a caller that DOES need stop/resume (e.g. main.py's live
    website-triggered scrapes), call scrape_maps() and
    check_facebook_pages() directly so the "remaining businesses" list
    can be kept and passed back in on the next run.
    """
    businesses = scrape_maps(niche, location, on_scroll_progress=on_scroll_progress, should_stop=should_stop)
    if should_stop and should_stop():
        return []
    leads, _remaining = check_facebook_pages(
        businesses, location, on_progress=on_progress, on_lead_found=on_lead_found, should_stop=should_stop,
    )
    return leads
