"""
flashscore_cache.py — Cache layer for the Flashscore provider.

Mirrors transfermarkt_cache.py so that stats.py can treat both scraped
providers the same way.  Unlike the Transfermarkt cache (which is expensive
to rebuild — a full Playwright scrape) this one is cheap, so it is rebuilt
from scratch whenever an entry goes stale.

Cache structure:
{
  "version": 1,
  "lastUpdated": "ISO8601",
  "teams": {
    "<name as it appears in the odds feed>": {
      "id": "8jbnAT1B", "name": "Kagoshima Utd", "slug": "kagoshima-united",
      "gender": "Men", "country": "Japan",
      "league_name": "J3 League",
      "league_position": 3, "total_teams": 20,
      "form_summary": {"form": ["W","L","D","D","L"],
                       "goals_scored": [3,1,1,1,0],
                       "goals_conceded": [1,2,1,1,1]},
      "results": [{"ts": 1794114000, "opponent": "Sagamihara",
                   "opponent_id": "nXumKCoo", "home_away": "A",
                   "score": [0, 1]}],
      "last_scraped": "ISO8601"
    }
  }
}
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flashscore_source import fetch_team_data

logging.basicConfig(level=logging.INFO, format="[fscache] %(message)s")
log = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "flashscore_cache.json"

# Entries older than this are refetched.
MAX_AGE_HOURS = 12

# Pause between requests so Flashscore is not hammered.
REQUEST_DELAY = 0.4

_cache: Optional[Dict[str, Any]] = None


# ── cache file ────────────────────────────────────────────────────────────────

def _load_cache() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    _cache = json.load(f)
            except (ValueError, OSError) as exc:
                log.warning("Flashscore cache unreadable (%s) — starting empty", exc)
                _cache = {"version": 1, "lastUpdated": None, "teams": {}}
        else:
            _cache = {"version": 1, "lastUpdated": None, "teams": {}}
        _cache.setdefault("teams", {})
    return _cache


def _save_cache(cache: Dict[str, Any]) -> None:
    cache["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _key(name: str) -> str:
    return name.lower().replace(" ", "_").replace(".", "").replace("'", "").replace("&", "").replace("-", "_")


def _is_stale(entry: Optional[Dict[str, Any]]) -> bool:
    if not entry or not entry.get("last_scraped"):
        return True
    try:
        scraped = datetime.fromisoformat(entry["last_scraped"].replace("Z", "+00:00"))
    except ValueError:
        return True
    return scraped < datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)


# ── refreshing ───────────────────────────────────────────────────────────────

def _to_entry(data: Dict[str, Any]) -> Dict[str, Any]:
    league = data.get("league") or {}
    return {
        "id": data["id"],
        "name": data["name"],
        "slug": data["slug"],
        "gender": data.get("gender"),
        "country": data.get("country"),
        "league_name": league.get("league_name"),
        "league_position": data.get("league_position"),
        "total_teams": data.get("total_teams"),
        "form_summary": {
            "form": data.get("form", []),
            "goals_scored": data.get("goals_scored", []),
            "goals_conceded": data.get("goals_conceded", []),
        },
        "results": [
            {
                "ts": m["ts"],
                "opponent": m["opponent"],
                "opponent_id": m["opponent_id"],
                "home_away": m["home_away"],
                "score": [m["scored"], m["conceded"]],
            }
            for m in data.get("recent_matches", [])
        ],
        "last_scraped": datetime.now(timezone.utc).isoformat(),
    }


def refresh(names: List[str]) -> Dict[str, Any]:
    """Fetch any missing or stale team and update the cache on disk."""
    cache = _load_cache()
    teams = cache.setdefault("teams", {})

    for name in names:
        if not name:
            continue
        key = _key(name)
        if not _is_stale(teams.get(key)):
            continue
        try:
            data = fetch_team_data(name)
        except Exception as exc:                        # noqa: BLE001
            log.warning("  Flashscore fetch failed for %s: %s", name, exc)
            continue
        if not data:
            log.warning("  No Flashscore match for %s", name)
            continue
        teams[key] = _to_entry(data)
        log.info("  %s -> %s (form %s, pos %s/%s)", name, data["name"],
                 "".join(data.get("form", [])), data.get("league_position"),
                 data.get("total_teams"))
        time.sleep(REQUEST_DELAY)

    _save_cache(cache)
    return cache


# ── lookups ──────────────────────────────────────────────────────────────────

def _find_team(team_name: str) -> Optional[Dict[str, Any]]:
    """Find a cached team by its odds-feed name, then by Flashscore id."""
    teams = _load_cache()["teams"]
    entry = teams.get(_key(team_name))
    if entry:
        return entry
    # Fall back to a match on the Flashscore name (sponsor prefixes differ)
    target = set(re.findall(r"[a-z0-9]+", team_name.lower()))
    for candidate in teams.values():
        name = set(re.findall(r"[a-z0-9]+", (candidate.get("name") or "").lower()))
        if name and target and name <= target:
            return candidate
    return None


def has_team(team_name: str) -> bool:
    return _find_team(team_name) is not None


def get_recent_form(team_name: str, n: int = 5) -> Dict[str, List]:
    """Recent form as {form, goals_scored, goals_conceded}."""
    team = _find_team(team_name)
    if not team:
        return {"form": [], "goals_scored": [], "goals_conceded": []}
    summary = team.get("form_summary") or {}
    return {
        "form": summary.get("form", [])[:n],
        "goals_scored": summary.get("goals_scored", [])[:n],
        "goals_conceded": summary.get("goals_conceded", [])[:n],
    }


def get_league_position(team_name: str) -> Optional[int]:
    team = _find_team(team_name)
    return team.get("league_position") if team else None


def get_total_teams(team_name: str) -> int:
    team = _find_team(team_name)
    if not team:
        return 20
    return team.get("total_teams") or 20


def get_h2h_data(home_team: str, away_team: str) -> Optional[List[Dict[str, Any]]]:
    """Head-to-head history, derived from the two teams' own result lists.

    Flashscore has no H2H page for teams, but every result names the
    opponent by id, so the intersection is the head-to-head record.

    Team names are echoed back as they appear in the odds feed: app.js
    compares them against the fixture's team names to work out who won.
    """
    home = _find_team(home_team)
    away = _find_team(away_team)
    if not home or not away:
        return None

    away_id = away.get("id")
    meetings = []
    for match in home.get("results", []):
        if match.get("opponent_id") != away_id:
            continue
        score = match.get("score") or []
        if len(score) != 2:
            continue
        is_home = match.get("home_away") == "H"
        meetings.append({
            "home": home_team if is_home else away_team,
            "away": away_team if is_home else home_team,
            "score": score,
            "ts": match.get("ts", 0),
        })

    if not meetings:
        return None
    meetings.sort(key=lambda m: m.get("ts", 0), reverse=True)
    return meetings[:5]


def get_cache_stats() -> Dict[str, Any]:
    cache = _load_cache()
    teams = cache.get("teams", {})
    return {
        "teams": len(teams),
        "lastUpdated": cache.get("lastUpdated", "never"),
    }


if __name__ == "__main__":
    import sys
    refresh(sys.argv[1:])
    print("cache stats:", get_cache_stats())
