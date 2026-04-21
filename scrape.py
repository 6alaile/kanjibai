"""
scrape.py — Scout Fixture & Odds Scraper
Primary source: BetPawa Tanzania (server-rendered HTML, fully scrapeable)

BetPawa structure confirmed from live page analysis:
- Event container: [data-event-id="XXXXXXXX"]
- Teams: .ScoreBoard_scoreboardPeriodParticipantName__* spans
- League: [data-test-id="eventPath"] — "Football / Country / League"
- Time: .SportEvents_times__* div
- Odds: three sequential X.XX numbers per event (home, draw, away)

Dependencies: requests, beautifulsoup4
"""

import re
import json
import time
import random
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="[scrape] %(message)s")
log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

EXCLUDED_KEYWORDS = {"israel", "ligat", "leumit", "toto cup"}

BETPAWA_URL = "https://www.betpawa.co.tz/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC"


def is_excluded(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in EXCLUDED_KEYWORDS)


def safe_float(val: str, fallback: float = 0.0) -> float:
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return fallback


def parse_time_eat(time_str: str) -> str:
    time_str = time_str.strip()
    try:
        dt = datetime.strptime(time_str, "%I:%M %p")
        return dt.strftime("%H:%M")
    except ValueError:
        return time_str


def parse_league(league_path: str) -> tuple:
    parts = [p.strip() for p in league_path.split("/")]
    if len(parts) >= 3:
        return f"{parts[2]} ({parts[1]})", parts[1]
    elif len(parts) == 2:
        return parts[1], parts[0]
    return league_path, ""


def fetch_betpawa(page: int = 0) -> list:
    url = BETPAWA_URL + (f"&page={page}" if page > 0 else "")
    log.info(f"Fetching BetPawa (page {page})...")

    try:
        resp = SESSION.get(url, timeout=20)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.warning(f"BetPawa fetch failed: {e}")
        return []

    html = resp.text
    fixtures = []
    event_blocks = re.split(r'(?=data-event-id="\d+")', html)

    for block in event_blocks:
        eid_match = re.match(r'data-event-id="(\d+)"', block)
        if not eid_match:
            continue
        event_id = eid_match.group(1)

        try:
            teams = re.findall(
                r'ScoreBoard_scoreboardPeriodParticipantName__\w+">([^<]+)<', block)
            if len(teams) < 2:
                continue
            home = teams[0].strip().replace("&amp;", "&")
            away = teams[1].strip().replace("&amp;", "&")

            league_raw = re.findall(r'data-test-id="eventPath">([^<]+)<', block)
            if not league_raw:
                continue
            league_display, country = parse_league(league_raw[0])
            if is_excluded(league_display) or is_excluded(country):
                continue

            time_raw = re.findall(r'SportEvents_times__\w+"><div>([^<]+)<', block)
            kick_off = parse_time_eat(time_raw[0]) if time_raw else "TBD"

            odds_vals = re.findall(r'>(\d{1,3}\.\d{2})<', block)
            if len(odds_vals) < 3:
                continue

            odds_home = safe_float(odds_vals[0])
            odds_draw = safe_float(odds_vals[1])
            odds_away = safe_float(odds_vals[2])

            if not all(o > 1.0 for o in [odds_home, odds_draw, odds_away]):
                continue

            fixtures.append({
                "id": f"bp_{event_id}",
                "league": league_display,
                "country": country,
                "time": kick_off,
                "home": home,
                "away": away,
                "source": "betpawa",
                "total_teams_in_league": 20,
                "odds": {
                    "home": odds_home,
                    "draw": odds_draw,
                    "away": odds_away,
                    "over_15": None,
                    "over_25": None,
                    "btts_yes": None
                },
                "home_stats": None,
                "away_stats": None,
                "h2h": None,
                "home_id": None,
                "away_id": None,
                "league_id": None,
                "season": None,
            })

        except Exception as e:
            log.warning(f"  Error parsing event {event_id}: {e}")
            continue

    log.info(f"BetPawa page {page}: {len(fixtures)} fixtures")
    return fixtures


def deduplicate(fixtures: list) -> list:
    seen = set()
    result = []
    for f in fixtures:
        key = (f["home"].lower().strip(), f["away"].lower().strip())
        if key not in seen:
            seen.add(key)
            result.append(f)
    return result


def fetch_fixtures(date_str: Optional[str] = None) -> list:
    fixtures = fetch_betpawa(page=0)
    if len(fixtures) >= 15:
        time.sleep(random.uniform(1.0, 2.0))
        fixtures += fetch_betpawa(page=1)
    fixtures = deduplicate(fixtures)
    log.info(f"Total unique fixtures: {len(fixtures)}")
    return fixtures


if __name__ == "__main__":
    fixtures = fetch_fixtures()
    print(json.dumps(fixtures[:3], indent=2))
    print(f"\n✓ {len(fixtures)} total fixtures fetched")
    print("\nAll fixtures:")
    for f in fixtures:
        o = f["odds"]
        print(f"  {f['time']} | {f['home']} vs {f['away']} | "
              f"{o['home']:.2f} / {o['draw']:.2f} / {o['away']:.2f} | {f['league']}")
