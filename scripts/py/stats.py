"""
stats.py — Scout Stats Enricher

Provider order (first source that has the field wins):
  1. Flashscore fixture feed — form, goals, results, H2H and league positions
     for both teams of a fixture, taken from the day's fixtures feed and the
     per-match feed.  This is the primary source.
  2. Flashscore per-team cache — form, goals and league position for a team
     that the day feed did not carry.
  3. Transfermarkt — fallback for teams Flashscore does not cover.
     **PAUSED** (2026-11-18): the Transfermarkt fallback is switched off
     (see TRANSFERMARKT_FALLBACK below). Teams Flashscore cannot resolve now
     stay unresolved — the UI shows them as low-data-confidence cards and the
     probability model falls back to its league-average priors. Turn the flag
     back on to restore the old behaviour.

Both providers are scraped ahead of time and read from their own cache:
  scripts/py/flashscore_cache.py      → data/flashscore_cache.json
  scripts/py/transfermarkt_cache.py   → data/transfermarkt_cache.json

Form arrays are newest first: index 0 is the most recent match.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transfermarkt_cache import (
    get_recent_form, has_team,
    get_league_position, get_opposition_position, get_h2h_data, get_total_teams
)
from flashscore_cache import (
    get_recent_form as fs_get_recent_form,
    has_team as fs_has_team,
    get_league_position as fs_get_league_position,
    get_total_teams as fs_get_total_teams,
    get_h2h_data as fs_get_h2h_data,
    get_recent_results as fs_get_recent_results,
    get_fixture_data as fs_get_fixture_data,
)

logging.basicConfig(level=logging.INFO, format="[stats] %(message)s")
log = logging.getLogger(__name__)

EMPTY_FORM = {"form": [], "goals_scored": [], "goals_conceded": []}

# The Transfermarkt fallback is paused: Flashscore is the only source of
# form, positions and H2H. Set this to True to bring the fallback back.
TRANSFERMARKT_FALLBACK = False

EAT = timezone(timedelta(hours=3))


# ─── ENRICH MATCH ─────────────────────────────────────────────────────────────

def _fixture_side(name: str, side: dict, position: Optional[int],
                  total_teams: Optional[int], opposition_position: Optional[int]) -> dict:
    """One side of a fixture, built from Flashscore's per-fixture feed."""
    return {
        "name": name,
        "form": side.get("form", []),
        "goals_scored": side.get("goals_scored", []),
        "goals_conceded": side.get("goals_conceded", []),
        "recent_results": side.get("results", []),
        "league_position": position,
        "total_teams_in_league": total_teams,
        "opposition_position": opposition_position,
    }


def _kickoff_time(epoch: int) -> str:
    """Kick-off in EAT wall clock, 24-hour."""
    return datetime.fromtimestamp(epoch, tz=EAT).strftime("%H:%M")


def enrich_match(match: dict) -> dict:
    home_name = match["home"]
    away_name = match["away"]
    enriched = dict(match)

    log.info(f"  Enriching: {home_name} vs {away_name}")

    # Flashscore per-fixture feed: both teams at once, current form, the real
    # head-to-head and the league positions straight from the standings feed.
    fixture = fs_get_fixture_data(home_name, away_name)
    if fixture and (fixture.get("home") or {}).get("form"):
        total_teams = fixture.get("total_teams")
        home_pos = fixture.get("home_position")
        away_pos = fixture.get("away_position")
        enriched["home_stats"] = _fixture_side(home_name, fixture.get("home") or {},
                                                home_pos, total_teams, away_pos)
        enriched["away_stats"] = _fixture_side(away_name, fixture.get("away") or {},
                                                away_pos, total_teams, home_pos)
        h2h_matches = fixture.get("h2h")
        enriched["h2h"] = {"matches": h2h_matches} if h2h_matches else None
        enriched["total_teams_in_league"] = total_teams
        if fixture.get("kickoff"):
            enriched["time"] = _kickoff_time(fixture["kickoff"])
        return enriched

    # Flashscore cache (primary)
    fs_home_form = fs_get_recent_form(home_name, n=5) if fs_has_team(home_name) else EMPTY_FORM
    fs_away_form = fs_get_recent_form(away_name, n=5) if fs_has_team(away_name) else EMPTY_FORM

    fs_home_pos = fs_get_league_position(home_name) if fs_has_team(home_name) else None
    fs_away_pos = fs_get_league_position(away_name) if fs_has_team(away_name) else None

    fs_total = fs_get_total_teams(home_name) if fs_has_team(home_name) else None

    fs_h2h = fs_get_h2h_data(home_name, away_name) if fs_has_team(home_name) else None

    # Transfermarkt cache (fallback) — paused, see TRANSFERMARKT_FALLBACK.
    if TRANSFERMARKT_FALLBACK:
        tm_home_form = get_recent_form(home_name, n=5) if has_team(home_name) else EMPTY_FORM
        tm_away_form = get_recent_form(away_name, n=5) if has_team(away_name) else EMPTY_FORM

        tm_home_pos = get_league_position(home_name) if has_team(home_name) else None
        tm_away_pos = get_league_position(away_name) if has_team(away_name) else None

        tm_total = get_total_teams(home_name)

        tm_opposition_pos = get_opposition_position(home_name, away_name) if has_team(away_name) else None

        tm_h2h = get_h2h_data(home_name, away_name) if has_team(home_name) else None
    else:
        tm_home_form = EMPTY_FORM
        tm_away_form = EMPTY_FORM
        tm_home_pos = None
        tm_away_pos = None
        tm_total = None
        tm_opposition_pos = None
        tm_h2h = None

    # Form: Flashscore, then Transfermarkt
    home_form = fs_home_form.get("form") or tm_home_form.get("form", [])
    home_goals_scored = fs_home_form.get("goals_scored") or tm_home_form.get("goals_scored", [])
    home_goals_conceded = fs_home_form.get("goals_conceded") or tm_home_form.get("goals_conceded", [])

    away_form = fs_away_form.get("form") or tm_away_form.get("form", [])
    away_goals_scored = fs_away_form.get("goals_scored") or tm_away_form.get("goals_scored", [])
    away_goals_conceded = fs_away_form.get("goals_conceded") or tm_away_form.get("goals_conceded", [])

    # Standings: Flashscore, then Transfermarkt
    home_pos = fs_home_pos if fs_home_pos is not None else tm_home_pos
    away_pos = fs_away_pos if fs_away_pos is not None else tm_away_pos

    # Total teams: Flashscore, then Transfermarkt
    total_teams = fs_total or tm_total

    # Opposition position: Flashscore, then Transfermarkt
    opposition_pos = fs_away_pos if fs_away_pos is not None else tm_opposition_pos

    # H2H: Flashscore, then Transfermarkt
    h2h_matches = fs_h2h or tm_h2h
    h2h = {"matches": h2h_matches} if h2h_matches else None

    enriched["home_stats"] = _build_stats(home_name, {
        "form": home_form,
        "goals_scored": home_goals_scored,
        "goals_conceded": home_goals_conceded
    }, home_pos, total_teams, opposition_pos,
       fs_get_recent_results(home_name, n=5) if fs_has_team(home_name) else [])

    enriched["away_stats"] = _build_stats(away_name, {
        "form": away_form,
        "goals_scored": away_goals_scored,
        "goals_conceded": away_goals_conceded
    }, away_pos, total_teams, opposition_pos,
       fs_get_recent_results(away_name, n=5) if fs_has_team(away_name) else [])
    enriched["h2h"] = h2h
    enriched["total_teams_in_league"] = total_teams
    return enriched


def _build_stats(name: str, form_data: dict, position: Optional[int], total_teams: int, opposition_pos: Optional[Any] = None, recent_results: Optional[list] = None) -> dict:
    return {
        "name": name,
        "form": form_data.get("form", []),
        "goals_scored": form_data.get("goals_scored", []),
        "goals_conceded": form_data.get("goals_conceded", []),
        "recent_results": recent_results or [],
        "league_position": position,
        "total_teams_in_league": total_teams,
        "opposition_position": opposition_pos
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
        "home": "Arsenal", "away": "Wolves",
        "time": "13:30",
        "odds": {"home": 1.55, "draw": 4.20, "away": 6.50},
        "home_stats": None, "away_stats": None, "h2h": None
    }]
    print(json.dumps(enrich_all(sample), indent=2))
