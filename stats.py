"""
stats.py — Scout Stats Enricher
Uses API-Football to enrich fixtures with form, H2H, and standings.
Same API key as scrape.py — set API_FOOTBALL_KEY as a GitHub secret.

Dependencies: requests
"""

import os
import json
import logging
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="[stats] %(message)s")
log = logging.getLogger(__name__)

API_KEY = os.environ.get("API_FOOTBALL_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"

HEADERS = {
    "x-apisports-key": API_KEY,
    "x-rapidapi-key": API_KEY,
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def api_get(endpoint: str, params: dict) -> Optional[dict]:
    if not API_KEY:
        log.error("API_FOOTBALL_KEY not set.")
        return None
    try:
        resp = SESSION.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            log.warning(f"API error on /{endpoint}: {data['errors']}")
            return None
        return data
    except requests.exceptions.RequestException as e:
        log.warning(f"Request failed /{endpoint}: {e}")
        return None


def safe_int(val, fallback: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return fallback


def fetch_team_form(team_id: int, league_id: int, season: int, n: int = 10) -> dict:
    data = api_get("fixtures", {
        "team": team_id, "league": league_id,
        "season": season, "last": n, "status": "FT"
    })
    if not data:
        return {}

    form, goals_scored, goals_conceded = [], [], []

    for match in data.get("response", []):
        teams = match.get("teams", {})
        goals = match.get("goals", {})
        is_home = teams.get("home", {}).get("id") == team_id
        hg = safe_int(goals.get("home", 0))
        ag = safe_int(goals.get("away", 0))
        scored = hg if is_home else ag
        conceded = ag if is_home else hg
        hw = teams.get("home", {}).get("winner")
        aw = teams.get("away", {}).get("winner")
        if (is_home and hw) or (not is_home and aw):
            result = "W"
        elif hw is None and aw is None:
            result = "D"
        else:
            result = "L"
        form.append(result)
        goals_scored.append(scored)
        goals_conceded.append(conceded)

    form.reverse()
    goals_scored.reverse()
    goals_conceded.reverse()

    return {"form": form, "goals_scored": goals_scored,
            "goals_conceded": goals_conceded, "opponent_positions": None}


def fetch_standings(league_id: int, season: int) -> dict:
    data = api_get("standings", {"league": league_id, "season": season})
    if not data:
        return {}
    result = {}
    for group in data.get("response", []):
        for standing_group in group.get("league", {}).get("standings", []):
            total = len(standing_group)
            for row in standing_group:
                tid = row.get("team", {}).get("id")
                pos = row.get("rank")
                if tid and pos:
                    result[tid] = {"position": pos, "total_teams": total}
    return result


def fetch_h2h(home_id: int, away_id: int, n: int = 10) -> Optional[dict]:
    data = api_get("fixtures/headtohead", {
        "h2h": f"{home_id}-{away_id}", "last": n, "status": "FT"
    })
    if not data:
        return None
    matches = data.get("response", [])
    if not matches:
        return None
    h2h_list = []
    for match in matches:
        teams = match.get("teams", {})
        goals = match.get("goals", {})
        h2h_list.append({
            "home": teams.get("home", {}).get("name", ""),
            "away": teams.get("away", {}).get("name", ""),
            "score": [safe_int(goals.get("home", 0)), safe_int(goals.get("away", 0))]
        })
    return {"matches": h2h_list}


_standings_cache: dict = {}

def get_standings_cached(league_id: int, season: int) -> dict:
    key = f"{league_id}_{season}"
    if key not in _standings_cache:
        _standings_cache[key] = fetch_standings(league_id, season)
    return _standings_cache[key]


def enrich_match(match: dict) -> dict:
    home_name = match["home"]
    away_name = match["away"]
    home_id = match.get("home_id")
    away_id = match.get("away_id")
    league_id = match.get("league_id")
    season = match.get("season")
    total_teams = 20

    log.info(f"  Enriching: {home_name} vs {away_name}")
    enriched = dict(match)

    home_form_data = fetch_team_form(home_id, league_id, season) if home_id and league_id and season else {}
    away_form_data = fetch_team_form(away_id, league_id, season) if away_id and league_id and season else {}

    home_pos = away_pos = None
    if league_id and season:
        standings = get_standings_cached(league_id, season)
        if standings:
            home_pos = standings.get(home_id, {}).get("position")
            away_pos = standings.get(away_id, {}).get("position")
            total_teams = standings.get(home_id, {}).get("total_teams", 20)

    h2h = fetch_h2h(home_id, away_id) if home_id and away_id else None

    enriched["home_stats"] = {
        "name": home_name,
        "form": home_form_data.get("form", []),
        "goals_scored": home_form_data.get("goals_scored", []),
        "goals_conceded": home_form_data.get("goals_conceded", []),
        "league_position": home_pos,
        "total_teams_in_league": total_teams,
        "opponent_positions": None
    }
    enriched["away_stats"] = {
        "name": away_name,
        "form": away_form_data.get("form", []),
        "goals_scored": away_form_data.get("goals_scored", []),
        "goals_conceded": away_form_data.get("goals_conceded", []),
        "league_position": away_pos,
        "total_teams_in_league": total_teams,
        "opponent_positions": None
    }
    enriched["h2h"] = h2h
    enriched["total_teams_in_league"] = total_teams
    return enriched


def enrich_all(matches: list[dict]) -> list[dict]:
    enriched = []
    total = len(matches)
    for i, match in enumerate(matches):
        log.info(f"[{i+1}/{total}] {match.get('home')} vs {match.get('away')}")
        try:
            enriched.append(enrich_match(match))
        except Exception as e:
            log.warning(f"  Failed to enrich: {e}")
            enriched.append(match)
    return enriched


if __name__ == "__main__":
    sample = [{
        "id": "1035290", "league": "Premier League (England)",
        "league_id": 39, "season": 2024, "time": "13:30",
        "home": "Arsenal", "away": "Wolves",
        "home_id": 42, "away_id": 39, "source": "api-football",
        "total_teams_in_league": 20,
        "odds": {"home": 1.55, "draw": 4.20, "away": 6.50,
                 "over_15": None, "over_25": None, "btts_yes": None},
        "home_stats": None, "away_stats": None, "h2h": None
    }]
    result = enrich_all(sample)
    print(json.dumps(result, indent=2))
