"""
transfermarkt_cache.py — Python interface to the Transfermarkt cache (v2)
Provides team/form/position/H2H data from the league-centric cached JSON.

Cache structure (v2):
{
  "version": 2,
  "lastUpdated": "ISO8601",
  "leagues": {
    "GB1": {
      "name": "Premier League",
      "table_fetched_at": "ISO8601",
      "teams": {
        "manchester_united": {
          "id": 985, "name": "Manchester United",
          "league_position": 8, "total_teams": 20,
          "recent_matches": [...], "h2h": {...}
        }
      }
    }
  }
}
"""

import json
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
            _cache = {"version": 2, "lastUpdated": None, "leagues": {}}
    return _cache


def _normalize_key(name: str) -> str:
    """Normalize team/player name for cache lookup."""
    return name.lower().replace(" ", "_").replace(".", "").replace("'", "").replace("&", "").replace("-", "_")


# ── Team lookup ───────────────────────────────────────────────────────────────

def _find_team(team_name: str) -> Optional[Dict]:
    """Find a team across all leagues. Returns the team dict or None.
    Tries exact match first, then fuzzy matching by word overlap.
    """
    cache = _load_cache()
    key = _normalize_key(team_name)

    # Exact match
    for league in cache.get("leagues", {}).values():
        team = league.get("teams", {}).get(key)
        if team:
            return team

    # Fuzzy match: find best match by word overlap
    target_words = set(team_name.lower().split())
    best_match = None
    best_score = 0

    for league in cache.get("leagues", {}).values():
        for tk, team in league.get("teams", {}).items():
            name = team.get("name", "")
            name_words = set(name.lower().split())
            # Exact word match
            if target_words == name_words:
                return team
            # Partial match
            score = len(target_words & name_words)
            if score > best_score:
                best_score = score
                best_match = team

    if best_match and best_score >= 1:
        return best_match

    return None


def has_team(team_name: str) -> bool:
    """Check if team exists in cache."""
    return _find_team(team_name) is not None


def get_team_data(team_name: str) -> Optional[Dict[str, Any]]:
    """Get full team data dict."""
    return _find_team(team_name)


# ── Form ───────────────────────────────────────────────────────────────────────

def get_recent_form(team_name: str, n: int = 5) -> Dict[str, List]:
    """Get recent form from Transfermarkt cache.
    Returns {form: [...], goals_scored: [...], goals_conceded: [...]}.
    Uses recent_matches stored by the scraper (last 5 across all comps).
    """
    team = _find_team(team_name)
    if not team:
        return {"form": [], "goals_scored": [], "goals_conceded": []}

    # Prefer precomputed form_summary from scraper
    summary = team.get("form_summary")
    if summary and len(summary.get("form", [])) >= n:
        return {
            "form": summary["form"][:n],
            "goals_scored": summary.get("goals_scored", [])[:n],
            "goals_conceded": summary.get("goals_conceded", [])[:n],
        }

    # Fallback: compute from recent_matches
    matches = team.get("recent_matches", [])
    if not matches:
        return {"form": [], "goals_scored": [], "goals_conceded": []}

    form = []
    gs = []
    gc = []

    for m in matches[:n]:
        score = m.get("score", [])
        if len(score) != 2:
            continue
        home_goals, away_goals = score

        # Determine if our team was home or away
        # Fixture structure uses home_away field (H/A), not home field
        is_home = m.get("home_away", "H") == "H"
        if is_home:
            scored, conceded = home_goals, away_goals
        else:
            scored, conceded = away_goals, home_goals

        form.append("W" if scored > conceded else ("L" if scored < conceded else "D"))
        gs.append(scored)
        gc.append(conceded)

    return {"form": form, "goals_scored": gs, "goals_conceded": gc}


# ── League position ────────────────────────────────────────────────────────────

def get_league_position(team_name: str) -> Optional[int]:
    """Get league position from Transfermarkt cache."""
    team = _find_team(team_name)
    if not team:
        return None
    return team.get("league_position")


def get_total_teams(team_name: str) -> int:
    """Get total teams in the league."""
    team = _find_team(team_name)
    if not team:
        return 20
    return team.get("total_teams", 20)


# ── Opposition position ────────────────────────────────────────────────────────

def get_opposition_position(home_team: str, away_team: str, is_knockout: bool = False) -> Optional[Any]:
    """Get opposition position for a match.
    For league: returns opponent's league position (int).
    For knockout: returns None (round name handled by competition parsing).
    """
    if is_knockout:
        return None

    # For league matches, return away team's league position
    return get_league_position(away_team)


# ── H2H ────────────────────────────────────────────────────────────────────────

def get_h2h_data(home_team: str, away_team: str) -> Optional[List[Dict]]:
    """Get H2H data between two teams from cache.
    Returns list of past matches or None.
    """
    cache = _load_cache()
    home_key = _normalize_key(home_team)
    away_key = _normalize_key(away_team)

    # Search all leagues for H2H data
    for league in cache.get("leagues", {}).values():
        teams = league.get("teams", {})
        team_data = teams.get(home_key, {})
        h2h = team_data.get("h2h", {})
        if away_key in h2h:
            return h2h[away_key]

    return None


# ── League data ────────────────────────────────────────────────────────────────

def get_league_teams(league_id: str) -> Dict[str, Dict]:
    """Get all teams in a league."""
    cache = _load_cache()
    return cache.get("leagues", {}).get(league_id, {}).get("teams", {})


def get_league_data(league_id: str) -> Optional[Dict]:
    """Get league metadata."""
    cache = _load_cache()
    return cache.get("leagues", {}).get(league_id)


# ── Search / Stats ─────────────────────────────────────────────────────────────

def search_teams(query: str) -> List[Dict[str, Any]]:
    """Search cached teams by partial name match."""
    cache = _load_cache()
    query_norm = _normalize_key(query)
    results = []
    for league in cache.get("leagues", {}).values():
        for key, team in league.get("teams", {}).items():
            if query_norm in key:
                results.append({"key": key, "data": team})
    return results


def get_cache_stats() -> Dict[str, int]:
    """Get cache statistics."""
    cache = _load_cache()
    leagues = cache.get("leagues", {})
    total_teams = sum(len(l.get("teams", {})) for l in leagues.values())
    total_h2h = sum(
        len(t.get("h2h", {}))
        for l in leagues.values()
        for t in l.get("teams", {}).values()
    )
    return {
        "version": cache.get("version", 2),
        "leagues": len(leagues),
        "teams": total_teams,
        "h2h_entries": total_h2h,
        "lastUpdated": cache.get("lastUpdated", "never"),
    }


if __name__ == "__main__":
    stats = get_cache_stats()
    print("Cache stats:", stats)
