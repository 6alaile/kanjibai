"""
debug_scrape.py — Run this temporarily to see what BetPawa actually returns
in the GitHub Actions environment. Dumps raw HTML snippet and parsed patterns.

Add to repo, run once via workflow, then remove.
"""

import re
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

URL = "https://www.betpawa.co.tz/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC"

print("Fetching BetPawa...")
resp = requests.get(URL, headers=HEADERS, timeout=20)
print(f"Status: {resp.status_code}")
print(f"Content-Type: {resp.headers.get('Content-Type')}")
print(f"Response length: {len(resp.text)} chars")
print()

html = resp.text

# Check for key patterns
patterns = [
    'data-event-id',
    'ScoreBoard_scoreboardPeriodParticipantName',
    'SportEvents_times',
    'data-test-id="eventPath"',
    'betpawa',
    'Football',
    'redirected',
    'cloudflare',
    'cf-ray',
    'captcha',
    'access denied',
    '__NEXT_DATA__',
]

print("=== PATTERN SEARCH ===")
for p in patterns:
    count = html.lower().count(p.lower())
    print(f"  '{p}': {count} occurrences")

print()
print("=== FIRST 3000 CHARS OF HTML ===")
print(html[:3000])
print()
print("=== LAST 1000 CHARS ===")
print(html[-1000:])

# Check event IDs
event_ids = re.findall(r'data-event-id="(\d+)"', html)
print(f"\n=== EVENT IDs FOUND: {len(event_ids)} ===")
print(event_ids[:10])

# Check response headers for bot detection
print("\n=== RESPONSE HEADERS ===")
for k, v in resp.headers.items():
    print(f"  {k}: {v}")
