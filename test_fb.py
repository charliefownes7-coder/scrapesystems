from playwright.sync_api import sync_playwright

from scraper import find_facebook_page, new_facebook_lookup_browser

test_businesses = [
    ("The Groom Room", "Halifax, NS"),
    ("Darling Dogs Grooming", "Halifax, NS"),
    ("Elaine's Poodle Parlour", "Halifax, NS"),
    ("Barra Construction Ltd", "Halifax, NS"),
    ("Curry Construction Ltd", "Halifax, NS"),
]

with sync_playwright() as p:
    browser = new_facebook_lookup_browser(p)
    page = browser.new_page(viewport={"width": 1920, "height": 1080})

    for name, loc in test_businesses:
        result = find_facebook_page(page, name, loc)
        print(f"{name} -> {result}\n")

    browser.close()
