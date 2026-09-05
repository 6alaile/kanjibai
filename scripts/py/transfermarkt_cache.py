"""
transfermarkt_cache.py — Python interface to the Transfermarkt cache
Provides team/player/fixture data from the cached JSON for enrichment.
"""

import json
import os
from pathlib import Path
from typing import Optional, Dict, Any, List

CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "transfermarkt_cache.json"

_cache = None

def _load_cache() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, "r") as f:
                _cache = json.load(f)
        else:
            _cache = {"version": 1, "teams": {}, "players": {}, "fixtures": {}, "leagues": {}}
    return _cache

def _normalize_key(name: str) -> str:
    """Normalize team/player name for cache lookup."""
    return name.lower().replace(" ", "_").replace(".", "").replace("'", "").replace("&", "").replace("-", "_")

def get_team_data(team_name: str) -> Optional[Dict[str, Any]]:
    """Get cached team data by name."""
    cache = _load_cache()
    key = _normalize_key(team_name)
    return cache.get("teams", {}).get(key, {}).get("data")

def get_player_data(player_name: str) -> Optional[Dict[str, Any]]:
    """Get cached player data by name."""
    cache = _load_cache()
    key = _normalize_key(player_name)
    return cache.get("players", {}).get(key, {}).get("data")

def get_fixtures(team_name: str) -> Optional[List[Dict[str, Any]]]:
    """Get cached fixtures for a team."""
    cache = _load_cache()
    key = _normalize_key(team_name)
    return cache.get("fixtures", {}).get(key, {}).get("data", {}).get("fixtures")

def get_league_data(league_name: str) -> Optional[Dict[str, Any]]:
    """Get cached league data."""
    cache = _load_cache()
    key = _normalize_key(league_name)
    return cache.get("leagues", {}).get(key, {}).get("data")

def has_team(team_name: str) -> bool:
    """Check if team data exists in cache."""
    return get_team_data(team_name) is not None

def has_fixtures(team_name: str) -> bool:
    """Check if fixtures exist in cache for a team."""
    fixtures = get_fixtures(team_name)
    return fixtures is not None and len(fixtures) > 0

def get_recent_form(team_name: str, n: int = 5) -> Dict[str, List]:
    """
    Get recent form from Transfermarkt fixtures.
    Returns dict with form, goals_scored, goals_conceded arrays.
    """
    fixtures = get_fixtures(team_name)
    if not fixtures:
        return {"form": [], "goals_scored": [], "goals_conceded": []}
    
    # Filter finished matches with scores
    finished = [f for f in fixtures if f.get("score") and ("-" in f.get("score", "") or ":" in f.get("score", ""))]
    # Sort by date descending (most recent first)
    finished.sort(key=lambda x: x.get("date", ""), reverse=True)
    finished = finished[:n]
    
    form = []
    goals_scored = []
    goals_conceded = []
    
    for fixture in finished:
        score = fixture.get("score", "")
        home_away = fixture.get("homeAway", "H")
        opponent = fixture.get("opponent", "")
        
        try:
            # Score format: "2-1" or "1:2"
            score_clean = score.replace(":", "-")
            home_goals, away_goals = map(int, score_clean.split("-"))
            
            if home_away == "H":
                scored, conceded = home_goals, away_goals
            else:
                scored, conceded = away_goals, home_goals
            
            goals_scored.append(scored)
            goals_conceded.append(conceded)
            
            if scored > conceded:
                form.append("W")
            elif scored < conceded:
                form.append("L")
            else:
                form.append("D")
        except (ValueError, AttributeError):
            continue
    
    # Reverse to chronological order (oldest first)
    form.reverse()
    goals_scored.reverse()
    goals_conceded.reverse()
    
    return {
        "form": form,
        "goals_scored": goals_scored,
        "goals_conceded": goals_conceded
    }


def get_league_position(team_name: str) -> Optional[int]:
    """Get league position from Transfermarkt cache."""
    data = get_team_data(team_name)
    if not data:
        return None
    return data.get("league_position")


def get_opposition_position(home_team: str, away_team: str, is_knockout: bool = False) -> Optional[Any]:
    """
    Get opposition position for a match.
    For league: returns opponent's league position (int).
    For knockout: returns round name (str) like "R16", "QF", "SF", "F".
    """
    if is_knockout:
        # For knockout tournaments, we'd need competition-specific logic
        # For now return None - will be handled by competition name parsing
        return None
    
    # For league matches, return away team's league position
    return get_league_position(away_team)


def get_h2h_data(home_team: str, away_team: str) -> Optional[List[Dict]]:
    """Get H2H data between two teams from cache."""
    cache = _load_cache()
    home_key = _normalize_key(home_team)
    away_key = _normalize_key(away_team)
    
    h2h_data = cache.get("h2h", {}).get(home_key, {}).get("data", {})
    if h2h_data:
        return h2h_data.get(away_key, [])
    return None

def get_team_market_value(team_name: str) -> Optional[str]:
    """Get team market value from profile."""
    data = get_team_data(team_name)
    if not data:
        return None
    profile = data.get("profile", {})
    observations = profile.get("observations", [])
    for obs in observations:
        text = obs.get("text", "")
        if "marketValue" in text or "market value" in text.lower():
            return text
    return None

def search_teams(query: str) -> List[Dict[str, Any]]:
    """Search cached teams by partial name match."""
    cache = _load_cache()
    query_norm = _normalize_key(query)
    results = []
    for key, entry in cache.get("teams", {}).items():
        if query_norm in key:
            results.append({"key": key, "data": entry.get("data"), "fetchedAt": entry.get("fetchedAt")})
    return results

def get_cache_stats() -> Dict[str, int]:
    """Get cache statistics."""
    cache = _load_cache()
    return {
        "teams": len(cache.get("teams", {})),
        "players": len(cache.get("players", {})),
        "fixtures": len(cache.get("fixtures", {})),
        "leagues": len(cache.get("leagues", {})),
        "lastUpdated": cache.get("lastUpdated", "never")
    }

if __name__ == "__main__":
    print("Cache stats:", get_cache_stats())
    print("\nSample teams:", list(_load_cache().get("teams", {}).keys())[:5])