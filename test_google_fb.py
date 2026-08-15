from google_config import GOOGLE_API_KEY, GOOGLE_SEARCH_ENGINE_ID
from google_fb_lookup import find_facebook_page_google, queries_remaining_today, queries_used_today

print(f"Queries used today: {queries_used_today()} / 100")
print(f"Queries remaining: {queries_remaining_today()}\n")

test_businesses = [
    ("The Groom Room", "Halifax, NS"),
    ("Darling Dogs Grooming", "Halifax, NS"),
    ("Elaine's Poodle Parlour", "Halifax, NS"),
    ("Barra Construction Ltd", "Halifax, NS"),
    ("Curry Construction Ltd", "Halifax, NS"),
]

for name, loc in test_businesses:
    result = find_facebook_page_google(name, loc, GOOGLE_API_KEY, GOOGLE_SEARCH_ENGINE_ID)
    print(f"{name} -> {result}")

print(f"\nQueries used today after this test: {queries_used_today()} / 100")
