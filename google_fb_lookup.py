"""
Facebook-page lookup using Google's Custom Search JSON API instead of
scraping DuckDuckGo. Reliable (a real sanctioned API, not fighting bot
detection) but capped at 100 free queries/day - this module tracks
that quota itself and refuses to go over it, so you never accidentally
get billed.

Setup needed before this works:
  1. Enable "Custom Search API" in your sterling-digital-leads Google
     Cloud project
  2. Create an API key under APIs & Services -> Credentials
  3. Create a Programmable Search Engine at
     programmablesearchengine.google.com set to "Search the entire web"
     and copy its Search Engine ID (cx)
  4. Put both values in google_config.py (see google_config_example.py)
"""

import json
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests

from scraper import _NON_BUSINESS_FACEBOOK_PATHS, _is_facebook_link

_DAILY_LIMIT = 100
_QUOTA_FILE = Path.home() / ".scrapesystems_google_quota.json"

_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"


def _load_quota_state() -> dict:
    """Quota resets automatically at the start of a new day - if the
    stored date isn't today, treat it as a fresh 0/100."""
    if _QUOTA_FILE.exists():
        try:
            state = json.loads(_QUOTA_FILE.read_text())
            if state.get("date") == str(date.today()):
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"date": str(date.today()), "used": 0}


def _save_quota_state(state: dict) -> None:
    _QUOTA_FILE.write_text(json.dumps(state))


def queries_used_today() -> int:
    return _load_quota_state()["used"]


def queries_remaining_today() -> int:
    return max(0, _DAILY_LIMIT - queries_used_today())


def _increment_quota() -> None:
    state = _load_quota_state()
    state["used"] += 1
    _save_quota_state(state)


def find_facebook_page_google(
    business_name: str, location: str, api_key: str, cx: str, retries: int = 2
) -> str | None:
    """
    Looks up a business's Facebook page via Google's Custom Search API.
    Returns None if the daily 100-query quota is already used up for
    today - check queries_remaining_today() before calling this in a
    loop so you know how many businesses you can actually check.
    """
    if not business_name:
        return None

    if queries_remaining_today() <= 0:
        return None

    query = f"site:facebook.com {business_name} {location}"
    params = {"key": api_key, "cx": cx, "q": query, "num": 3}

    for attempt in range(retries + 1):
        try:
            resp = requests.get(_SEARCH_URL, params=params, timeout=10)
            _increment_quota()  # counts against quota whether it succeeds or not - Google bills/counts the request itself
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"  Google lookup failed for {business_name}: {e}")
            return None

        if resp.status_code == 429:
            # Quota exhausted server-side (shouldn't normally happen if
            # our own counter is accurate, but Google's count is the
            # real source of truth) - stop entirely for today.
            print(f"  Google API quota hit (429) - stopping for today")
            state = _load_quota_state()
            state["used"] = _DAILY_LIMIT
            _save_quota_state(state)
            return None

        if resp.status_code != 200:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            try:
                error_detail = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:
                error_detail = resp.text[:300]
            print(f"  [{business_name}] Google API error: status={resp.status_code} - {error_detail}")
            return None

        data = resp.json()
        for item in data.get("items", []):
            link = item.get("link", "")
            if not _is_facebook_link(link):
                continue
            if any(path in link for path in _NON_BUSINESS_FACEBOOK_PATHS):
                continue
            return link

        return None

    return None
