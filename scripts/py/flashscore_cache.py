"""
flashscore_cache.py — Cache layer for the Flashscore provider.

Mirrors transfermarkt_cache.py so that stats.py can treat both scraped
providers the same way.  Unlike the Transfermarkt cache (which is expensive
to rebuild — a full Playwright scrape) this one is cheap, so it is rebuilt
from scratch whenever an entry goes stale.

Two sources feed the cache:

* per team   — search -> team page -> "results" feed (form, league position).
  Kept as the fallback for fixtures the day feed does not carry.
* per fixture — Flashscore's `df_hh_1_<match_id>` feed, reached through the
  day's fixtures feed.  This is the primary source: it is current, it names
  both teams, and it resolves clubs whose bookmaker name does not match a
  Flashscore name (women's sides, lower-division sides).

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
  },
  "fixtures": {
    "<home>__vs__<away>": {
      "match_id": "S8C03d8l", "kickoff": 1794819600,
      "h2h": [{"home": "Probiy Horodenka", "away": "Inhulets",
               "score": [2, 2], "ts": 1785063600}],
      "home": {"form": ["D","L","W","W","L"],
               "goals_scored": [1,1,2,1,0], "goals_conceded": [1,2,0,4,1],
               "results": [{"ts": 1793957400, "opponent": "Kulikiv",
                            "opponent_id": "6o5vRpTE", "home_away": "H",
                            "score": [1, 1]}]},
      "away": {...},
      "home_position": 10, "away_position": 14, "total_teams": 16
    }
  }
}

Form arrays are newest first everywhere (the Transfermarkt scraper already
works that way), so a card can render the last five matches newest on top.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flashscore_source import fetch_team_data
from flashscore_fixture_source import (
    fetch_day_fixtures, fetch_match_detail, fetch_table, match_fixtures,
)

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
                "match_id": m.get("match_id", ""),
                "opponent": m["opponent"],
                "opponent_id": m["opponent_id"],
                "home_away": m["home_away"],
                "score": [m["scored"], m["conceded"]],
            }
            for m in data.get("recent_matches", [])
        ],
        "last_scraped": datetime.now(timezone.utc).isoformat(),
    }


def _team_form(data: Dict[str, Any]) -> Dict[str, Any]:
    """One side of a fixture: form, goals and results, newest first."""
    results = sorted(data.get("results", []), key=lambda r: r["ts"], reverse=True)
    return {
        "form": data.get("form", []),
        "goals_scored": data.get("goals_scored", []),
        "goals_conceded": data.get("goals_conceded", []),
        "results": results,
    }


def _book_names(meetings: List[Dict[str, Any]], fs: Dict[str, Any],
                home_name: str, away_name: str) -> List[Dict[str, Any]]:
    """Rename the meetings' sides to the bookmaker's names.

    The card compares the head-to-head sides with the team names it displays, so
    the two spellings have to agree.
    """
    out = []
    for meeting in meetings:
        if meeting.get("home") == fs.get("home"):
            out.append({**meeting, "home": home_name, "away": away_name})
        elif meeting.get("home") == fs.get("away"):
            out.append({**meeting, "home": away_name, "away": home_name})
        else:
            out.append(meeting)
    return out


def refresh(names: List[str], fixtures: Optional[List[Dict[str, Any]]] = None,
            date_str: Optional[str] = None) -> Dict[str, Any]:
    """Fetch any missing or stale team and update the cache on disk.

    If fixtures is provided (list of {home, away, id, ...} from BetPawa), the
    day's Flashscore fixtures feed is matched against them and every match
    gets its own entry: both teams' last five matches and their head-to-head
    history, taken from Flashscore's per-fixture feed.
    """
    cache = _load_cache()
    teams = cache.setdefault("teams", {})
    fixtures_store = cache.setdefault("fixtures", {})

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

    if fixtures:
        _refresh_fixtures(fixtures, date_str, fixtures_store)

    _save_cache(cache)
    return cache


def _refresh_fixtures(fixtures: List[Dict[str, Any]], date_str: Optional[str],
                      fixtures_store: Dict[str, Any]) -> None:
    """Fill the per-fixture entries from Flashscore's day feed and match feeds."""
    try:
        day_fixtures = fetch_day_fixtures()
    except Exception as exc:                            # noqa: BLE001
        log.warning("  Flashscore day feed failed: %s", exc)
        return

    matches = match_fixtures(fixtures, day_fixtures, date_str or "")
    log.info("  Matched %d/%d fixtures against Flashscore", len(matches), len(fixtures))

    tables: Dict[str, Dict[str, Dict]] = {}

    for fix in fixtures:
        fs = matches.get(fix.get("id", ""))
        if not fs:
            continue
        home_name, away_name = fix.get("home", ""), fix.get("away", "")
        key = f"{_key(home_name)}__vs__{_key(away_name)}"
        if key in fixtures_store:
            continue

        try:
            detail = fetch_match_detail(fs["id"], fs.get("home_id", ""),
                                        fs.get("home", ""), fs.get("away_id", ""),
                                        fs.get("away", ""))
        except Exception as exc:                        # noqa: BLE001
            log.warning("  H2H fetch failed for %s vs %s: %s", home_name, away_name, exc)
            continue

        league = fs.get("league") or {}
        table_key = f"{league.get('table_id')}__{league.get('season_id')}"
        if table_key not in tables:
            try:
                tables[table_key] = fetch_table(league.get("table_id", ""),
                                                league.get("season_id", ""))
            except Exception as exc:                    # noqa: BLE001
                log.warning("  standings fetch failed for %s: %s", table_key, exc)
                tables[table_key] = {}
        table = tables[table_key]

        home_row = table.get(fs.get("home_id", "")) or {}
        away_row = table.get(fs.get("away_id", "")) or {}
        fixtures_store[key] = {
            "match_id": fs["id"],
            "kickoff": fs["ts"],
            "h2h": _book_names(detail["h2h"], fs, home_name, away_name),
            "home": _team_form(detail["home"]),
            "away": _team_form(detail["away"]),
            "home_position": home_row.get("position"),
            "away_position": away_row.get("position"),
            "total_teams": len(table) or None,
        }
        log.info("  %s vs %s: form %s/%s, pos %s/%s, %d H2H", home_name, away_name,
                 "".join(detail["home"]["form"]), "".join(detail["away"]["form"]),
                 home_row.get("position"), away_row.get("position"),
                 len(detail["h2h"]))
        time.sleep(REQUEST_DELAY)


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
    """Recent form as {form, goals_scored, goals_conceded}, newest first."""
    team = _find_team(team_name)
    if not team:
        return {"form": [], "goals_scored": [], "goals_conceded": []}
    summary = team.get("form_summary") or {}
    # The team feed yields the last five oldest first; the card shows them
    # newest first, like Flashscore.
    form = list(reversed(summary.get("form", [])))[:n]
    goals_scored = list(reversed(summary.get("goals_scored", [])))[:n]
    goals_conceded = list(reversed(summary.get("goals_conceded", [])))[:n]
    return {
        "form": form,
        "goals_scored": goals_scored,
        "goals_conceded": goals_conceded,
    }


def get_recent_results(team_name: str, n: int = 5) -> List[Dict[str, Any]]:
    """Per-match results: {opponent, home_away (H/A), score: [scored, conceded]}.

    The five most recent matches, newest first, aligned with the form/goals
    arrays returned by get_recent_form().
    """
    team = _find_team(team_name)
    if not team:
        return []
    results = team.get("results") or []
    # results are stored most-recent-first
    return results[:n]


def get_league_position(team_name: str) -> Optional[int]:
    team = _find_team(team_name)
    return team.get("league_position") if team else None


def get_total_teams(team_name: str) -> int:
    team = _find_team(team_name)
    if not team:
        return 20
    return team.get("total_teams") or 20


def get_fixture_data(home_team: str, away_team: str) -> Optional[Dict[str, Any]]:
    """Everything Flashscore knows about one fixture, keyed by the book names.

    Holds both teams' last five matches, their head-to-head history, the
    league positions and the true kick-off time.
    """
    cache = _load_cache()
    store = cache.get("fixtures", {})
    return store.get(f"{_key(home_team)}__vs__{_key(away_team)}")


def get_h2h_data(home_team: str, away_team: str) -> Optional[List[Dict[str, Any]]]:
    """Head-to-head history, newest first.

    The per-fixture feed is preferred; failing that the history is derived by
    intersecting the two teams' result lists.
    """
    fixture = get_fixture_data(home_team, away_team)
    if fixture and fixture.get("h2h"):
        return fixture["h2h"]

    # Fallback: derive from result intersection
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
            # score is always from home_team's view; flip it when the meeting's
            # displayed home becomes the opponent.
            "score": score if is_home else [score[1], score[0]],
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
