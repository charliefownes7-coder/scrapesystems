"""
Scraper module - your real Google Maps scraper, wrapped for the app.

This is your original parsing/scrolling logic from test.py, changed to:
  - run headless (no visible browser window)
  - take a single (niche, location) instead of reading searches.txt
  - return leads shaped to match your sheet's real columns
  - skip businesses that already have a website, same as before
"""

import random
import re
import time
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

_DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"

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


def _brand_token(business_name: str) -> str | None:
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


def find_facebook_page(page, business_name: str, location: str, retries: int = 3) -> str | None:
    """
    Looks for a public Facebook page for a business, using a real
    headless Firefox page (see new_facebook_lookup_browser) navigating
    DuckDuckGo's HTML results endpoint - confirmed via side-by-side
    testing to get past blocking that both plain requests and a
    TLS-fingerprint-faking HTTP client both hit.

    `page` should be a Playwright Page from an already-open Firefox
    browser context - reuse the same one across the whole run so
    cookies persist across lookups, don't open a fresh browser per
    business.

    Checks the top 3 results for a real facebook.com business/profile
    page, validated two ways: it must actually be on facebook.com's
    domain (not just contain that text somewhere in the URL), and the
    business's own distinctive name must appear in the page's URL
    slug (catches an unrelated similarly-named business slipping into
    the results, which happened during testing).

    DuckDuckGo sometimes responds to automated requests with a
    block/bot-check page instead of real results. That's NOT the same
    as "this business has no Facebook page" - if we treated it that
    way we'd wrongly mark real leads as False. So when that happens,
    we back off and retry instead of giving up immediately.
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
    url = f"{_DUCKDUCKGO_URL}?{urlencode({'q': query})}"

    for attempt in range(retries + 1):
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
        results = soup.select("a.result__a")

        # A 200 with zero results is normal (genuinely nothing found).
        # Anything else with zero results is a block, not a real answer.
        looks_blocked = status != 200 and not results

        if looks_blocked:
            if attempt < retries:
                wait = 30 * (2 ** attempt)  # 30s, 60s, 120s
                print(f"  [{business_name}] looks blocked (status={status}), waiting {wait}s and retrying")
                time.sleep(wait)
                continue
            print(f"  [{business_name}] STILL BLOCKED after {retries} retries - result unknown, not a confirmed False")
            return None

        if not results:
            return None

        for result in results[:3]:
            href = result.get("href", "")
            fb_url = _resolve_result_url(href)
            if not _is_facebook_link(fb_url):
                continue
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


def _scrape_search(page, query, on_scroll_progress=None):
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

    previous_count = 0
    no_change_count = 0
    max_scrolls = 400
    scroll_step = 800
    interrupted = False

    for i in range(max_scrolls):
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


def run_scrape(niche: str, location: str, on_progress=None, on_scroll_progress=None) -> list[dict]:
    """
    Run one scrape for a given niche + location, e.g.
    run_scrape("construction", "Nova Scotia") searches
    "construction in Nova Scotia" on Google Maps.

    Returns only businesses WITHOUT an existing website (pitch-ready
    leads) - same filtering your original script did before adding
    rows to the sheet.

    If given, on_progress(current, total, business_name) is called
    once per business while checking for a Facebook page, so a caller
    (e.g. the Streamlit app) can show live progress.

    If given, on_scroll_progress(current, total, cards_found) is called
    on every scroll attempt during the Google Maps stage - cards_found
    is the last-known count of businesses loaded so far (updates each
    time the results list actually grows). Most runs finish in well
    under "total" (400) scrolls, so a caller is generally better off
    displaying cards_found than the raw scroll count.
    """
    query = f"{niche} in {location}".strip()
    all_businesses = []

    with sync_playwright() as p:
        # Chromium is fine for Google Maps scraping - the blocking
        # problem was specific to DuckDuckGo detecting Chromium's CDP
        # automation protocol, not Chromium itself being unusable.
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        try:
            results, interrupted = _scrape_search(page, query, on_scroll_progress)
            combined = list(results)

            if interrupted:
                print(f"  '{query}' stopped early -- retrying once\n")
                page.wait_for_timeout(5000)
                retry_results, retry_interrupted = _scrape_search(page, query, on_scroll_progress)
                combined = _dedupe(combined + retry_results)

            all_businesses.extend(combined)
        finally:
            browser.close()

        no_website_businesses = [b for b in _dedupe(all_businesses) if not b["has_website"]]
        total = len(no_website_businesses)

        # Separate Firefox browser for Facebook lookups - this is the
        # part that was getting blocked under Chromium, confirmed
        # fixed by switching engines. One page reused across the whole
        # run so cookies persist between lookups.
        fb_browser = new_facebook_lookup_browser(p)
        fb_page = fb_browser.new_page(viewport={"width": 1920, "height": 1080})

        leads = []
        try:
            for i, b in enumerate(no_website_businesses, start=1):
                name = b["name"] or "(unnamed)"
                print(f"  [{i}/{total}] Checking {name} for Facebook page...")
                if on_progress:
                    on_progress(i - 1, total, name)

                facebook_url = find_facebook_page(fb_page, b["name"] or "", location)

                # Slower, more human-looking pacing: a random gap after every
                # lookup, plus a longer pause every 15 businesses to break up
                # the steady rhythm that gets flagged as a bot sweep. Slower,
                # but much less likely to get blocked mid-run.
                if i % 15 == 0:
                    pause = random.uniform(45, 90)
                else:
                    pause = random.uniform(5.0, 12.0)

                # Broken into small steps (instead of one long time.sleep)
                # purely so progress can tick smoothly through the pause -
                # otherwise the UI sits frozen for up to a minute and a
                # half between per-lead updates, which looks identical to
                # an actual hang.
                elapsed = 0.0
                step = 1.0
                while elapsed < pause:
                    wait = min(step, pause - elapsed)
                    time.sleep(wait)
                    elapsed += wait
                    if on_progress:
                        frac = elapsed / pause if pause else 1
                        on_progress(i - 1 + frac, total, name)

                leads.append({
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
                })
        finally:
            fb_browser.close()

    return leads
