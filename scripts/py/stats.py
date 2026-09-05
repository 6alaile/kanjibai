"""
stats.py — Scout Stats Enricher
Uses football-data.org free tier for form, standings, H2H.
Falls back to Transfermarkt cache for additional data.
Current 2024/25 season data, no IP restrictions, no cost.

Covered leagues (free tier):
PL, PD, BL1, SA, FL1, CL, EC, PPL, DED, BSA, WC

Register free at: https://www.football-data.org/client/register
Set FOOTBALL_DATA_TOKEN as a GitHub Actions secret.

Dependencies: requests
"""

import os
import json
import time
import logging
import sys
from typing import Optional

import requests

# Add scripts directory to path for transfermarkt_cache import
sys.path.insert(0, os.path.dirname(__file__))
from transfermarkt_cache import get_team_data, get_recent_form, has_team, get_cache_stats

logging.basicConfig(level=logging.INFO, format="[stats] %(message)s")
log = logging.getLogger(__name__)

FD_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "")
FD_BASE = "https://api.football-data.org/v4"
FD_HEADERS = {"X-Auth-Token": FD_TOKEN}

SESSION = requests.Session()
SESSION.headers.update(FD_HEADERS)

# football-data.org league code map
# Maps common league name fragments to their API codes
# Order matters: more specific patterns first
LEAGUE_CODE_MAP = {
    "premier league": "PL",
    "la liga": "PD",
    "primera division": "PD",
    "laliga": "PD",
    "la liga ea sports": "PD",
    "spain primera": "PD",
    "bundesliga": "BL1",
    "serie a (brazil)": "BSA",
    "brasileiro serie a": "BSA",
    "campeonato brasileiro": "BSA",
    "brasileirao": "BSA",
    "serie a": "SA",
    "ligue 1": "FL1",
    "champions league": "CL",
    "european championship": "EC",
    "primeira liga": "PPL",
    "liga portugal": "PPL",
    "eredivisie": "DED",
    "championship": "ELC",
    "allsvenskan": "DED",
    "superliga": "DED",  # Denmark/Sweden
    "k league": None,  # Not in free tier
    "liga 2": None,  # Not in free tier
}

# Rate limit: 10 requests/minute on free tier
RATE_DELAY = 7  # seconds between calls to stay safe


def delay():
    time.sleep(RATE_DELAY)


def fd_get(endpoint: str, params: dict = None) -> Optional[dict]:
    if not FD_TOKEN:
        log.warning("FOOTBALL_DATA_TOKEN not set — skipping enrichment.")
        return None
    try:
        resp = SESSION.get(f"{FD_BASE}/{endpoint}", params=params, timeout=15)
        if resp.status_code == 429:
            log.warning("Rate limited, waiting 60s...")
            time.sleep(60)
            resp = SESSION.get(f"{FD_BASE}/{endpoint}", params=params, timeout=15)
        if resp.status_code == 403:
            log.warning(f"403 on {endpoint} — league may not be in free tier")
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        log.warning(f"football-data.org request failed {endpoint}: {e}")
        return None


def get_league_code(league_name: str) -> Optional[str]:
    """Match league display name to football-data.org code."""
    low = league_name.lower()
    for key, code in LEAGUE_CODE_MAP.items():
        if key in low:
            return code
    return None


def safe_int(val, fallback: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return fallback


import unicodedata

def _normalize_name(name: str) -> str:
    """Strip diacritics, lower case, remove common prefixes."""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower().strip()
    for prefix in ("fc ", "sc ", "afc ", "cf ", "ac ", "rc "):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n


# ─── FIND TEAM ID ────────────────────────────────────────────────────────────
 
def find_team_id(team_name: str, league_code: str) -> Optional[int]:
    """Find team ID by searching within a league."""
    data = fd_get(f"competitions/{league_code}/teams")
    delay()
    if not data:
        return None
    teams = data.get("teams", [])
    target = _normalize_name(team_name)
    best_match = None
    best_score = 0
    for team in teams:
        for field in ("name", "shortName", "tla"):
            val = team.get(field, "")
            if not val:
                continue
            norm = _normalize_name(val)
            if target == norm:
                return team.get("id")
            if target in norm or norm in target:
                score = len(set(target.split()) & set(norm.split()))
                if score > best_score:
                    best_score = score
                    best_match = team.get("id")
    return best_match
    return None


# ─── TEAM FORM ────────────────────────────────────────────────────────────────

def fetch_team_form(team_id: int, n: int = 15) -> dict:
    """Fetch last N finished matches for a team."""
    data = fd_get(f"teams/{team_id}/matches", params={"status": "FINISHED", "limit": n})
    delay()
    if not data:
        return {}

    matches = data.get("matches", [])
    # Sort by date descending, take last N
    matches = sorted(matches, key=lambda m: m.get("utcDate", ""), reverse=True)[:n]

    form, goals_scored, goals_conceded = [], [], []

    for match in matches:
        home_team = match.get("homeTeam", {})
        away_team = match.get("awayTeam", {})
        score = match.get("score", {}).get("fullTime", {})
        home_goals = safe_int(score.get("home", 0))
        away_goals = safe_int(score.get("away", 0))

        is_home = home_team.get("id") == team_id
        scored = home_goals if is_home else away_goals
        conceded = away_goals if is_home else home_goals

        winner = match.get("score", {}).get("winner", "")
        if winner == "HOME_TEAM":
            result = "W" if is_home else "L"
        elif winner == "AWAY_TEAM":
            result = "L" if is_home else "W"
        else:
            result = "D"

        form.append(result)
        goals_scored.append(scored)
        goals_conceded.append(conceded)

    # Reverse so oldest first
    form.reverse()
    goals_scored.reverse()
    goals_conceded.reverse()

    return {
        "form": form,
        "goals_scored": goals_scored,
        "goals_conceded": goals_conceded,
        "opponent_positions": None
    }


# ─── STANDINGS ────────────────────────────────────────────────────────────────

def fetch_standings(league_code: str) -> dict:
    """Fetch current standings for a league."""
    data = fd_get(f"competitions/{league_code}/standings")
    delay()
    if not data:
        return {}

    result = {}
    standings = data.get("standings", [])
    # Use TOTAL standings table
    for table in standings:
        if table.get("type") == "TOTAL":
            rows = table.get("table", [])
            total = len(rows)
            for row in rows:
                tid = row.get("team", {}).get("id")
                pos = row.get("position")
                if tid and pos:
                    result[tid] = {"position": pos, "total_teams": total}
    return result


# ─── H2H ─────────────────────────────────────────────────────────────────────

def fetch_h2h(match_id: int) -> Optional[dict]:
    """Fetch H2H for a specific match."""
    data = fd_get(f"matches/{match_id}/head2head", params={"limit": 10})
    delay()
    if not data:
        return None

    matches = data.get("matches", [])
    if not matches:
        return None

    h2h_list = []
    for match in matches:
        score = match.get("score", {}).get("fullTime", {})
        h2h_list.append({
            "home": match.get("homeTeam", {}).get("name", ""),
            "away": match.get("awayTeam", {}).get("name", ""),
            "score": [
                safe_int(score.get("home", 0)),
                safe_int(score.get("away", 0))
            ]
        })

    return {"matches": h2h_list}


# ─── FIND MATCH ID ───────────────────────────────────────────────────────────

def find_match_id(home_name: str, away_name: str, league_code: str) -> Optional[int]:
    """Find today's match ID in football-data.org for H2H lookup."""
    from datetime import datetime, timezone, timedelta
    EAT = timezone(timedelta(hours=3))
    today = datetime.now(EAT).strftime("%Y-%m-%d")

    data = fd_get(f"competitions/{league_code}/matches", params={"dateFrom": today, "dateTo": today})
    delay()
    if not data:
        return None

    matches = data.get("matches", [])
    home_low = home_name.lower()
    away_low = away_name.lower()

    for match in matches:
        mh = match.get("homeTeam", {}).get("name", "").lower()
        ma = match.get("awayTeam", {}).get("name", "").lower()
        if home_low[:5] in mh and away_low[:5] in ma:
            return match.get("id")

    return None


# ─── CACHE ────────────────────────────────────────────────────────────────────

_standings_cache: dict = {}
_team_id_cache: dict = {}


def get_standings_cached(league_code: str) -> dict:
    if league_code not in _standings_cache:
        _standings_cache[league_code] = fetch_standings(league_code)
    return _standings_cache[league_code]


def get_team_id_cached(name: str, league_code: str) -> Optional[int]:
    key = f"{name}_{league_code}"
    if key not in _team_id_cache:
        _team_id_cache[key] = find_team_id(name, league_code)
    return _team_id_cache[key]


# ─── ENRICH MATCH ─────────────────────────────────────────────────────────────

def enrich_match(match: dict) -> dict:
    home_name = match["home"]
    away_name = match["away"]
    league_name = match.get("league", "")
    enriched = dict(match)
    total_teams = 20

    log.info(f"  Enriching: {home_name} vs {away_name}")

    league_code = get_league_code(league_name)

    # Try Transfermarkt cache first for form data
    tm_home_form = get_recent_form(home_name, n=10) if has_team(home_name) else {"form": [], "goals_scored": [], "goals_conceded": []}
    tm_away_form = get_recent_form(away_name, n=10) if has_team(away_name) else {"form": [], "goals_scored": [], "goals_conceded": []}

    if not league_code:
        log.info(f"  League '{league_name}' not in free tier — using Transfermarkt cache only")
        enriched["home_stats"] = _build_stats(home_name, tm_home_form, None, total_teams)
        enriched["away_stats"] = _build_stats(away_name, tm_away_form, None, total_teams)
        enriched["h2h"] = None
        return enriched

    # Get team IDs from football-data.org
    home_id = get_team_id_cached(home_name, league_code)
    away_id = get_team_id_cached(away_name, league_code)

    # Form data - merge football-data.org with Transfermarkt
    fd_home_form = fetch_team_form(home_id) if home_id else {}
    fd_away_form = fetch_team_form(away_id) if away_id else {}

    # Prefer football-data.org form, fall back to Transfermarkt
    home_form = fd_home_form.get("form") or tm_home_form.get("form", [])
    home_goals_scored = fd_home_form.get("goals_scored") or tm_home_form.get("goals_scored", [])
    home_goals_conceded = fd_home_form.get("goals_conceded") or tm_home_form.get("goals_conceded", [])

    away_form = fd_away_form.get("form") or tm_away_form.get("form", [])
    away_goals_scored = fd_away_form.get("goals_scored") or tm_away_form.get("goals_scored", [])
    away_goals_conceded = fd_away_form.get("goals_conceded") or tm_away_form.get("goals_conceded", [])

    # Standings
    home_pos = away_pos = None
    standings = get_standings_cached(league_code)
    if standings and home_id:
        home_pos = standings.get(home_id, {}).get("position")
        total_teams = standings.get(home_id, {}).get("total_teams", 20)
    if standings and away_id:
        away_pos = standings.get(away_id, {}).get("position")

    # H2H
    h2h = None
    match_id = find_match_id(home_name, away_name, league_code)
    if match_id:
        h2h = fetch_h2h(match_id)

    enriched["home_stats"] = _build_stats(home_name, {
        "form": home_form,
        "goals_scored": home_goals_scored,
        "goals_conceded": home_goals_conceded
    }, home_pos, total_teams)
    enriched["away_stats"] = _build_stats(away_name, {
        "form": away_form,
        "goals_scored": away_goals_scored,
        "goals_conceded": away_goals_conceded
    }, away_pos, total_teams)
    enriched["h2h"] = h2h
    enriched["total_teams_in_league"] = total_teams
    return enriched


def _build_stats(name: str, form_data: dict, position: Optional[int], total_teams: int) -> dict:
    return {
        "name": name,
        "form": form_data.get("form", []),
        "goals_scored": form_data.get("goals_scored", []),
        "goals_conceded": form_data.get("goals_conceded", []),
        "league_position": position,
        "total_teams_in_league": total_teams,
        "opponent_positions": None
    }


def _empty_stats(name: str) -> dict:
    return {
        "name": name, "form": [], "goals_scored": [],
        "goals_conceded": [], "league_position": None,
        "total_teams_in_league": 20, "opponent_positions": None
    }


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
        "id": "test_001",
        "league": "Premier League (England)",
        "league_id": 39, "season": 2025, "time": "13:30",
        "home": "Arsenal", "away": "Wolves",
        "home_id": None, "away_id": None,
        "source": "api-football", "total_teams_in_league": 20,
        "odds": {"home": 1.55, "draw": 4.20, "away": 6.50,
                 "over_15": None, "over_25": None, "btts_yes": None},
        "home_stats": None, "away_stats": None, "h2h": None
    }]
    result = enrich_all(sample)
    print(json.dumps(result, indent=2))
