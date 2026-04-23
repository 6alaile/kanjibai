"""
stats.py — Scout Stats Enricher
Uses Sofascore unofficial API for current season form, H2H, standings.
Current season data (2025/26) — no API key required.

Rate limit: 25-30 second delays between calls to avoid Cloudflare blocks.
Dependencies: requests
"""

import os
import json
import time
import random
import logging
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="[stats] %(message)s")
log = logging.getLogger(__name__)

SOFASCORE_BASE = "https://www.sofascore.com/api/v1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
    "Cache-Control": "no-cache",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def delay():
    """Polite delay to avoid rate limiting."""
    time.sleep(random.uniform(2.0, 4.0))


def sf_get(endpoint: str) -> Optional[dict]:
    """GET from Sofascore API."""
    url = f"{SOFASCORE_BASE}/{endpoint}"
    try:
        resp = SESSION.get(url, timeout=15)
        if resp.status_code == 429:
            log.warning(f"Rate limited on {endpoint}, waiting 30s...")
            time.sleep(30)
            resp = SESSION.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        log.warning(f"Sofascore request failed {endpoint}: {e}")
        return None


def safe_int(val, fallback: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return fallback


# ─── TEAM SEARCH ─────────────────────────────────────────────────────────────

def search_team(name: str) -> Optional[dict]:
    """Search Sofascore for a team by name, return first football result."""
    data = sf_get(f"search/multi-suggest?q={requests.utils.quote(name)}")
    delay()
    if not data:
        return None
    teams = data.get("teams", []) or []
    for team in teams:
        sport = team.get("sport", {}).get("slug", "")
        if sport == "football":
            return team
    return None


# ─── TEAM FORM ────────────────────────────────────────────────────────────────

def fetch_team_form(team_id: int, n: int = 10) -> dict:
    """Fetch last N matches for a team from Sofascore."""
    data = sf_get(f"team/{team_id}/events/last/0")
    delay()
    if not data:
        return {}

    events = data.get("events", []) or []
    # Filter finished matches only
    finished = [e for e in events if e.get("status", {}).get("type") == "finished"]
    # Most recent N
    finished = finished[-n:]

    form, goals_scored, goals_conceded = [], [], []

    for event in finished:
        home_team = event.get("homeTeam", {})
        away_team = event.get("awayTeam", {})
        home_score = event.get("homeScore", {}).get("current", 0) or 0
        away_score = event.get("awayScore", {}).get("current", 0) or 0

        is_home = home_team.get("id") == team_id
        scored = home_score if is_home else away_score
        conceded = away_score if is_home else home_score

        if scored > conceded:
            result = "W"
        elif scored == conceded:
            result = "D"
        else:
            result = "L"

        form.append(result)
        goals_scored.append(int(scored))
        goals_conceded.append(int(conceded))

    return {
        "form": form,
        "goals_scored": goals_scored,
        "goals_conceded": goals_conceded,
        "opponent_positions": None
    }


# ─── STANDINGS ────────────────────────────────────────────────────────────────

def fetch_standings(tournament_id: int, season_id: int) -> dict:
    """Fetch league standings from Sofascore."""
    data = sf_get(f"tournament/{tournament_id}/season/{season_id}/standings/total")
    delay()
    if not data:
        return {}

    result = {}
    standings = data.get("standings", []) or []
    for group in standings:
        rows = group.get("rows", []) or []
        total = len(rows)
        for row in rows:
            tid = row.get("team", {}).get("id")
            pos = row.get("position")
            if tid and pos:
                result[tid] = {"position": pos, "total_teams": total}
    return result


# ─── H2H ─────────────────────────────────────────────────────────────────────

def fetch_h2h(event_id: int) -> Optional[dict]:
    """Fetch H2H matches for a given Sofascore event ID."""
    data = sf_get(f"event/{event_id}/h2h/events")
    delay()
    if not data:
        return None

    h2h_list = []
    for section in ["previousEvents", "previousHomeAwayEvents"]:
        events = data.get(section, []) or []
        for e in events:
            if e.get("status", {}).get("type") != "finished":
                continue
            h2h_list.append({
                "home": e.get("homeTeam", {}).get("name", ""),
                "away": e.get("awayTeam", {}).get("name", ""),
                "score": [
                    e.get("homeScore", {}).get("current", 0) or 0,
                    e.get("awayScore", {}).get("current", 0) or 0
                ]
            })

    if not h2h_list:
        return None
    return {"matches": h2h_list[:10]}


# ─── FIND EVENT ON SOFASCORE ─────────────────────────────────────────────────

def find_event(home: str, away: str) -> Optional[dict]:
    """Search for a specific match on Sofascore to get event ID for H2H."""
    query = f"{home} {away}"
    data = sf_get(f"search/multi-suggest?q={requests.utils.quote(query)}")
    delay()
    if not data:
        return None
    events = data.get("events", []) or []
    for event in events:
        eh = event.get("homeTeam", {}).get("name", "").lower()
        ea = event.get("awayTeam", {}).get("name", "").lower()
        if home.lower()[:5] in eh or away.lower()[:5] in ea:
            return event
    return None


# ─── CACHE ────────────────────────────────────────────────────────────────────

_team_cache: dict = {}
_standings_cache: dict = {}


def get_team_cached(name: str) -> Optional[dict]:
    if name not in _team_cache:
        _team_cache[name] = search_team(name)
    return _team_cache[name]


# ─── ENRICH MATCH ─────────────────────────────────────────────────────────────

def enrich_match(match: dict) -> dict:
    home_name = match["home"]
    away_name = match["away"]
    enriched = dict(match)
    total_teams = 20

    log.info(f"  Enriching: {home_name} vs {away_name}")

    # Search for teams on Sofascore
    home_team_sf = get_team_cached(home_name)
    away_team_sf = get_team_cached(away_name)

    home_form_data = {}
    away_form_data = {}
    home_pos = away_pos = None
    h2h = None

    if home_team_sf:
        home_id_sf = home_team_sf.get("id")
        if home_id_sf:
            home_form_data = fetch_team_form(home_id_sf)

            # Get standings via team's tournament
            tournament = home_team_sf.get("tournament", {})
            t_id = tournament.get("uniqueTournament", {}).get("id")
            # Try to get season from last event
            last_event_data = sf_get(f"team/{home_id_sf}/events/last/0")
            delay()
            if last_event_data:
                events = last_event_data.get("events", [])
                if events:
                    season_id = events[-1].get("season", {}).get("id")
                    uniq_t_id = events[-1].get("tournament", {}).get("uniqueTournament", {}).get("id")
                    if uniq_t_id and season_id:
                        cache_key = f"{uniq_t_id}_{season_id}"
                        if cache_key not in _standings_cache:
                            _standings_cache[cache_key] = fetch_standings(uniq_t_id, season_id)
                        standings = _standings_cache.get(cache_key, {})
                        if standings:
                            home_pos = standings.get(home_id_sf, {}).get("position")
                            total_teams = standings.get(home_id_sf, {}).get("total_teams", 20)

    if away_team_sf:
        away_id_sf = away_team_sf.get("id")
        if away_id_sf:
            away_form_data = fetch_team_form(away_id_sf)
            # Get away position from same standings if available
            if home_team_sf:
                cache_key = list(_standings_cache.keys())[-1] if _standings_cache else None
                if cache_key:
                    standings = _standings_cache.get(cache_key, {})
                    away_pos = standings.get(away_id_sf, {}).get("position")

    # H2H via event search
    sf_event = find_event(home_name, away_name)
    if sf_event:
        event_id = sf_event.get("id")
        if event_id:
            h2h = fetch_h2h(event_id)

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


def enrich_all(matches: list) -> list:
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
        "id": "test_001", "league": "Premier League (England)",
        "league_id": 39, "season": 2025, "time": "13:30",
        "home": "Arsenal", "away": "Wolves",
        "home_id": None, "away_id": None, "source": "api-football",
        "total_teams_in_league": 20,
        "odds": {"home": 1.55, "draw": 4.20, "away": 6.50,
                 "over_15": None, "over_25": None, "btts_yes": None},
        "home_stats": None, "away_stats": None, "h2h": None
    }]
    result = enrich_all(sample)
    print(json.dumps(result, indent=2))
