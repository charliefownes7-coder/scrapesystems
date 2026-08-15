"""
One clean, side-by-side test of three fundamentally different approaches
to see which one(s) actually get past DuckDuckGo's bot detection -
deliberately designed to use only 3 total requests (one per method,
each on a different business) so we get real signal without another
big testing burst.

METHOD 1 - plain requests (the current baseline). Fast, but has a
distinctive, easily-flagged TLS fingerprint that no header-spoofing can
hide, since it happens before any HTTP headers are even sent.

METHOD 2 - curl_cffi with Chrome impersonation. A lightweight HTTP
client (not a real browser - no automation protocol, nothing for
CDP-based detection to catch) that fakes a genuine Chrome TLS
fingerprint at the network level. Untested until now.

METHOD 3 - Playwright with real Firefox (not Chromium). Chromium
automation goes through CDP (Chrome DevTools Protocol), which has its
own well-known detection signature separate from TLS or headers.
Firefox automation doesn't use CDP at all, so that specific detection
vector doesn't apply - while still being a real browser with a real
TLS signature. This is also a genuinely different browser engine than
anything tried so far.

All three hit the exact same URL (DuckDuckGo's /html/ lite endpoint)
so the only thing that changes between them is the client technology -
that keeps the comparison clean.

Run once: python3 diagnose_blocking.py
Send Claude the full output.
"""

from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from scraper import (
    _DUCKDUCKGO_URL,
    _SEARCH_HEADERS,
    _NON_BUSINESS_FACEBOOK_PATHS,
    _is_facebook_link,
    _looks_like_real_match,
    _resolve_result_url,
)

TEST_CASES = [
    ("The Groom Room", "Halifax, NS"),
    ("Darling Dogs Grooming", "Halifax, NS"),
    ("Barra Construction Ltd", "Halifax, NS"),
]


def _evaluate(business_name: str, status, soup: BeautifulSoup):
    results = soup.select("a.result__a")
    blocked = status != 200 and not results
    match = None
    for r in results[:3]:
        url = _resolve_result_url(r.get("href", ""))
        if not _is_facebook_link(url):
            continue
        if any(p in url for p in _NON_BUSINESS_FACEBOOK_PATHS):
            continue
        if not _looks_like_real_match(business_name, url):
            continue
        match = url
        break
    return blocked, match, len(results)


def method_1_plain_requests(business_name: str, location: str):
    print("\n[Method 1: plain requests - current baseline]")
    query = f"site:facebook.com {business_name} {location}"
    try:
        resp = requests.get(_DUCKDUCKGO_URL, params={"q": query}, headers=_SEARCH_HEADERS, timeout=10)
    except requests.RequestException as e:
        print(f"  Request failed: {e}")
        return
    soup = BeautifulSoup(resp.text, "html.parser")
    blocked, match, count = _evaluate(business_name, resp.status_code, soup)
    print(f"  status={resp.status_code}  results_found={count}  blocked={blocked}  match={match}")


def method_2_curl_cffi(business_name: str, location: str):
    print("\n[Method 2: curl_cffi - real Chrome TLS fingerprint, no browser automation]")
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        print("  curl_cffi not installed - run: pip install curl_cffi")
        return
    query = f"site:facebook.com {business_name} {location}"
    try:
        session = curl_requests.Session(impersonate="chrome131")
        resp = session.get(_DUCKDUCKGO_URL, params={"q": query}, timeout=10)
    except Exception as e:
        print(f"  Request failed: {e}")
        return
    soup = BeautifulSoup(resp.text, "html.parser")
    blocked, match, count = _evaluate(business_name, resp.status_code, soup)
    print(f"  status={resp.status_code}  results_found={count}  blocked={blocked}  match={match}")


def method_3_playwright_firefox(business_name: str, location: str):
    print("\n[Method 3: Playwright + real Firefox (not Chromium, no CDP)]")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed")
        return
    query = f"site:facebook.com {business_name} {location}"
    url = f"{_DUCKDUCKGO_URL}?{urlencode({'q': query})}"
    try:
        with sync_playwright() as p:
            browser = p.firefox.launch(
                headless=True,
                firefox_user_prefs={
                    "dom.webdriver.enabled": False,
                    "useAutomationExtension": False,
                },
            )
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            response = page.goto(url, timeout=20000)
            page.wait_for_timeout(1500)
            status = response.status if response else None
            html = page.content()
            browser.close()
    except Exception as e:
        print(f"  Request failed: {e}")
        print("  (if this says something about a missing browser, run: playwright install firefox)")
        return
    soup = BeautifulSoup(html, "html.parser")
    blocked, match, count = _evaluate(business_name, status, soup)
    print(f"  status={status}  results_found={count}  blocked={blocked}  match={match}")


if __name__ == "__main__":
    method_1_plain_requests(*TEST_CASES[0])
    method_2_curl_cffi(*TEST_CASES[1])
    method_3_playwright_firefox(*TEST_CASES[2])
    print("\nDone - send this full output back.")
