"""
scrape.py — Scout Fixture & Odds Scraper
Uses API-Football (api-sports.io) free tier.
Free plan: 100 requests/day — sufficient for daily scan.

Register free at: https://dashboard.api-football.com
Set API_FOOTBALL_KEY as a GitHub Actions secret.

Dependencies: requests
"""

import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="[scrape] %(message)s")
log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))
API_KEY = os.environ.get("API_FOOTBALL_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"

HEADERS = {
    "x-apisports-key": API_KEY,
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

EXCLUDED_COUNTRIES = {"israel"}
EXCLUDED_KEYWORDS = {"israel", "ligat", "leumit", "toto cup"}


def is_excluded(league_name: str, country: str) -> bool:
    name = league_name.lower()
    ctry = country.lower()
    return ctry in EXCLUDED_COUNTRIES or any(kw in name for kw in EXCLUDED_KEYWORDS)


def safe_float(val, fallback: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return fallback


def api_get(endpoint: str, params: dict) -> Optional[dict]:
    if not API_KEY:
        log.error("API_FOOTBALL_KEY not set. Add it as a GitHub secret.")
        return None
    try:
        resp = SESSION.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        errors = data.get("errors", {})
        if errors:
            log.warning(f"API error on /{endpoint}: {errors}")
            return None
        remaining = resp.headers.get("x-ratelimit-requests-remaining", "?")
        log.info(f"  API requests remaining today: {remaining}")
        return data
    except requests.exceptions.RequestException as e:
        log.warning(f"Request failed /{endpoint}: {e}")
        return None


def fetch_fixtures(date_str: Optional[str] = None) -> list:
    if not date_str:
        date_str = datetime.now(EAT).strftime("%Y-%m-%d")

    log.info(f"Fetching fixtures for {date_str} from API-Football...")

    data = api_get("fixtures", {"date": date_str, "timezone": "Africa/Dar_es_Salaam"})
    if not data:
        log.warning("No fixture data returned.")
        return []

    raw_fixtures = data.get("response", [])
    log.info(f"  {len(raw_fixtures)} total fixtures found")

    # Filter excluded leagues
    fixtures = []
    for f in raw_fixtures:
        league = f.get("league", {})
        league_name = league.get("name", "")
        country = league.get("country", "")
        if is_excluded(league_name, country):
            continue
        fixtures.append(f)

    log.info(f"  {len(fixtures)} fixtures after excluding Israeli leagues")

    if not fixtures:
        return []

    # Fetch odds
    odds_map = {}
    odds_data = api_get("odds", {
        "date": date_str,
        "bookmaker": 8,
        "bet": 1
    })

    if odds_data:
        for item in odds_data.get("response", []):
            fixture_id = item.get("fixture", {}).get("id")
            for bm in item.get("bookmakers", []):
                for bet in bm.get("bets", []):
                    if bet.get("id") == 1:
                        values = {v["value"]: safe_float(v["odd"]) for v in bet.get("values", [])}
                        odds_map[fixture_id] = {
                            "home": values.get("Home", 0.0),
                            "draw": values.get("Draw", 0.0),
                            "away": values.get("Away", 0.0),
                            "over_15": None,
                            "over_25": None,
                            "btts_yes": None
                        }

    log.info(f"  Odds found for {len(odds_map)} fixtures")

    result = []
    for f in fixtures:
        fixture_id = f.get("fixture", {}).get("id")
        league = f.get("league", {})
        teams = f.get("teams", {})
        kick_off_utc = f.get("fixture", {}).get("date", "")

        kick_off = "TBD"
        if kick_off_utc:
            try:
                dt = datetime.fromisoformat(kick_off_utc.replace("Z", "+00:00"))
                kick_off = dt.astimezone(EAT).strftime("%H:%M")
            except Exception:
                kick_off = kick_off_utc[:16]

        home_name = teams.get("home", {}).get("name", "")
        away_name = teams.get("away", {}).get("name", "")
        home_id = teams.get("home", {}).get("id")
        away_id = teams.get("away", {}).get("id")
        league_id = league.get("id")
        season = league.get("season")

        odds = odds_map.get(fixture_id, {
            "home": 0.0, "draw": 0.0, "away": 0.0,
            "over_15": None, "over_25": None, "btts_yes": None
        })

        # Skip if no odds
        if not odds["home"] or not odds["away"]:
            continue

        result.append({
            "id": str(fixture_id),
            "league": f"{league.get('name', '')} ({league.get('country', '')})",
            "league_id": league_id,
            "season": season,
            "time": kick_off,
            "home": home_name,
            "away": away_name,
            "home_id": home_id,
            "away_id": away_id,
            "source": "api-football",
            "total_teams_in_league": 20,
            "odds": odds,
            "home_stats": None,
            "away_stats": None,
            "h2h": None
        })

    log.info(f"  {len(result)} fixtures with odds ready for enrichment")
    return result


if __name__ == "__main__":
    fixtures = fetch_fixtures()
    print(json.dumps(fixtures[:3], indent=2))
    print(f"\n✓ {len(fixtures)} fixtures fetched with odds")
