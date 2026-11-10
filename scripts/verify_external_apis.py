"""One-shot connectivity checks for GDELT DOC 2.0 and Tavily Search.

Run:
    python -m pip install requests python-dotenv
    # Put TAVILY_API_KEY=... in a local .env file, excluded from Git
    python verify_external_apis.py

Makes at most one request to each service. A successful HTTP status does
not guarantee relevant or factually verified content.
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

def show_results(label, response, list_key):
    print(f"\n{label} — HTTP {response.status_code}")
    if not response.ok:
        if response.status_code == 429:
            print("Rate limited: not usable for this test; do not retry rapidly.")
        else:
            print("Request failed; examine credentials, parameters, or service status.")
        return
    try:
        payload = response.json()
    except ValueError:
        print("Response was not valid JSON.")
        return
    results = payload.get(list_key, [])
    print(f"JSON parsed; {len(results)} results found.")
    if not results:
        print("No usable article results. Check query and response format.")
    for item in results[:5]:
        print(f"- {item.get('title', '[untitled]')[:90]}")
        print(f"  URL: {item.get('url', '[missing]')}")
        print(f"  Date: {item.get('published_date') or item.get('seendate') or 'not supplied'}")

print("=== GDELT DOC 2.0 (no key, one request) ===")
try:
    response = requests.get(
        "https://api.gdeltproject.org/api/v2/doc/doc",
        params={
            "query": '"sunflower oil"',
            "mode": "artlist",
            "format": "json",
            "maxrecords": 5,
            "timespan": "1week",
        },
        timeout=30,
    )
    show_results("GDELT", response, "articles")
except requests.RequestException as exc:
    print(f"GDELT network error: {type(exc).__name__}")

print("\n=== Tavily Search (one basic search, typically one credit) ===")
key = os.getenv("TAVILY_API_KEY")
if not key:
    print("SKIPPED: set TAVILY_API_KEY in your local .env file. Do not share it.")
else:
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "query": "sunflower oil supply disruption Europe",
                "topic": "news",
                "time_range": "month",
                "search_depth": "basic",
                "max_results": 5,
                "include_published_date": True,
                "include_answer": False,
            },
            timeout=30,
        )
        show_results("Tavily", response, "results")
    except requests.RequestException as exc:
        print(f"Tavily network error: {type(exc).__name__}")