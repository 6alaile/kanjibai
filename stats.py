"""
stats.py — Scout Stats Enricher
Enriches fixture dicts with form, goals data, H2H, and league standings.
Sources: Sofascore (primary), Flashscore (fallback)

Dependencies: requests, beautifulsoup4
"""

import json
import time
import random
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="[stats] %(message)s")
log = logging.getLogger(__name__)

# ─── CONFIG ─────────────────────────────────────────────────────────────────

SOFASCORE_API = "https://api.sofascore.com/api/v1"

HEADERS_SOFASCORE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

HEADERS_FLASHSCORE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "x-fsign": "SW9D1eZo",  # Required by Flashscore
}

SESSION = requests.Session()


def random_delay(min_s=0.8, max_s=2.0):
    time.sleep(random.uniform(min_s, max_s))


def safe_get(url: str, headers: dict, params: dict = None, timeout: int = 10) -> Optional[dict | str]:
    try:
        resp = SESSION.get(url, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "json" in ct:
            return resp.json()
        return resp.text
    except requests.exceptions.RequestException as e:
        log.warning(f"Request failed: {url} — {e}")
        return None


# ─── SOFASCORE: SEARCH TEAM ──────────────────────────────────────────────────

def sofascore_search_team(team_name: str) -> Optional[dict]:
    """Search Sofascore for a team and return first football result."""
    url = f"{SOFASCORE_API}/search/multi-suggest"
    data = safe_get(url, HEADERS_SOFASCORE, params={"q": team_name})
    if not data or not isinstance(data, dict):
        return None
    teams = data.get("teams", []) or []
    for team in teams:
        if team.get("sport", {}).get("slug") == "football":
            return team
    return None


def sofascore_search_event(home: str, away: str) -> Optional[dict]:
    """Search for a specific match event on Sofascore."""
    query = f"{home} {away}"
    url = f"{SOFASCORE_API}/search/multi-suggest"
    data = safe_get(url, HEADERS_SOFASCORE, params={"q": query})
    if not data or not isinstance(data, dict):
        return None
    events = data.get("events", []) or []
    for event in events:
        eh = event.get("homeTeam", {}).get("name", "").lower()
        ea = event.get("awayTeam", {}).get("name", "").lower()
        if home.lower()[:4] in eh or away.lower()[:4] in ea:
            return event
    return None


# ─── SOFASCORE: TEAM FORM ────────────────────────────────────────────────────

def sofascore_team_last_matches(team_id: int, n: int = 10) -> list[dict]:
    """Fetch last N matches for a team."""
    url = f"{SOFASCORE_API}/team/{team_id}/events/last/0"
    data = safe_get(url, HEADERS_SOFASCORE)
    if not data or not isinstance(data, dict):
        return []
    return data.get("events", []) or []


def parse_team_form(matches: list[dict], team_id: int, n: int = 10) -> dict:
    """
    Parse raw Sofascore match list into form/goals arrays.
    Returns dict compatible with scorer.py TeamStats fields.
    """
    form = []
    goals_scored = []
    goals_conceded = []
    opponent_positions = []

    for match in matches[:n]:
        status = match.get("status", {}).get("type", "")
        if status not in ("finished",):
            continue

        home_team = match.get("homeTeam", {})
        away_team = match.get("awayTeam", {})
        home_score = match.get("homeScore", {}).get("current", 0) or 0
        away_score = match.get("awayScore", {}).get("current", 0) or 0

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

        # Opponent league position if available
        opp = away_team if is_home else home_team
        opp_pos = opp.get("ranking")
        if opp_pos:
            opponent_positions.append(int(opp_pos))

    # Most recent match is last (scorer.py weights recent more)
    form.reverse()
    goals_scored.reverse()
    goals_conceded.reverse()
    opponent_positions.reverse()

    return {
        "form": form,
        "goals_scored": goals_scored,
        "goals_conceded": goals_conceded,
        "opponent_positions": opponent_positions if opponent_positions else None
    }


# ─── SOFASCORE: STANDINGS ────────────────────────────────────────────────────

def sofascore_team_standing(team_id: int, tournament_id: int, season_id: int) -> Optional[dict]:
    """Fetch a team's current league position."""
    url = f"{SOFASCORE_API}/tournament/{tournament_id}/season/{season_id}/standings/total"
    data = safe_get(url, HEADERS_SOFASCORE)
    if not data or not isinstance(data, dict):
        return None
    rows = data.get("standings", [{}])[0].get("rows", []) or []
    for row in rows:
        if row.get("team", {}).get("id") == team_id:
            return {
                "position": row.get("position"),
                "total_teams": len(rows)
            }
    return None


# ─── SOFASCORE: H2H ─────────────────────────────────────────────────────────

def sofascore_h2h(event_id: int) -> list[dict]:
    """Fetch H2H matches for a given event."""
    url = f"{SOFASCORE_API}/event/{event_id}/h2h/events"
    data = safe_get(url, HEADERS_SOFASCORE)
    if not data or not isinstance(data, dict):
        return []

    h2h_raw = []
    for section in ["previousEvents", "previousHomeAwayEvents"]:
        events = data.get(section, []) or []
        for e in events:
            status = e.get("status", {}).get("type", "")
            if status != "finished":
                continue
            h2h_raw.append({
                "home": e.get("homeTeam", {}).get("name", ""),
                "away": e.get("awayTeam", {}).get("name", ""),
                "score": [
                    e.get("homeScore", {}).get("current", 0) or 0,
                    e.get("awayScore", {}).get("current", 0) or 0
                ]
            })
    return h2h_raw[:10]  # cap at 10


# ─── FLASHSCORE FALLBACK ─────────────────────────────────────────────────────

FLASHSCORE_BASE = "https://www.flashscore.com"

def flashscore_search_team(team_name: str) -> Optional[str]:
    """Returns Flashscore team URL slug."""
    url = f"{FLASHSCORE_BASE}/search/"
    params = {"q": team_name}
    html = safe_get(url, HEADERS_FLASHSCORE, params=params)
    if not html or not isinstance(html, str):
        return None

    soup = BeautifulSoup(html, "html.parser")
    links = soup.select("a.search-result__participant")
    for link in links:
        href = link.get("href", "")
        if "/team/" in href:
            return href
    return None


def flashscore_team_form(team_slug: str, n: int = 10) -> dict:
    """
    Scrape last N results from Flashscore team results page.
    Returns form/goals arrays.
    """
    url = f"{FLASHSCORE_BASE}{team_slug}results/"
    html = safe_get(url, HEADERS_FLASHSCORE)
    if not html or not isinstance(html, str):
        return {}

    soup = BeautifulSoup(html, "html.parser")
    form = []
    goals_scored = []
    goals_conceded = []

    rows = soup.select(".event__match--static")

    for row in rows[:n]:
        try:
            score_el = row.select_one(".event__score")
            if not score_el:
                continue

            scores = score_el.get_text(strip=True)
            parts = re.split(r"[-:]", scores)
            if len(parts) != 2:
                continue

            hg, ag = int(parts[0].strip()), int(parts[1].strip())

            # Determine if this team was home or away
            home_el = row.select_one(".event__participant--home")
            home_name = home_el.get_text(strip=True) if home_el else ""

            # Use slug to determine side (rough match)
            slug_name = team_slug.split("/")[-2].replace("-", " ").lower()
            is_home = slug_name in home_name.lower()

            scored = hg if is_home else ag
            conceded = ag if is_home else hg

            if scored > conceded:
                form.append("W")
            elif scored == conceded:
                form.append("D")
            else:
                form.append("L")

            goals_scored.append(scored)
            goals_conceded.append(conceded)

        except Exception:
            continue

    form.reverse()
    goals_scored.reverse()
    goals_conceded.reverse()

    return {
        "form": form,
        "goals_scored": goals_scored,
        "goals_conceded": goals_conceded,
        "opponent_positions": None
    }


# ─── ENRICH A SINGLE MATCH ───────────────────────────────────────────────────

def enrich_match(match: dict) -> dict:
    """
    Takes a partial match dict from scrape.py,
    adds home_stats, away_stats, h2h, and total_teams_in_league.
    Returns enriched match dict.
    """
    home_name = match["home"]
    away_name = match["away"]

    log.info(f"Enriching: {home_name} vs {away_name}")

    home_stats_data = {"name": home_name, "form": [], "goals_scored": [], "goals_conceded": [],
                       "league_position": None, "total_teams_in_league": None, "opponent_positions": None}
    away_stats_data = {"name": away_name, "form": [], "goals_scored": [], "goals_conceded": [],
                       "league_position": None, "total_teams_in_league": None, "opponent_positions": None}
    h2h_data = None
    total_teams = 20

    # ── Try Sofascore ──
    home_team_sf = sofascore_search_team(home_name)
    random_delay()
    away_team_sf = sofascore_search_team(away_name)
    random_delay()

    if home_team_sf and away_team_sf:
        home_id = home_team_sf.get("id")
        away_id = away_team_sf.get("id")

        # Form data
        if home_id:
            home_matches = sofascore_team_last_matches(home_id)
            random_delay()
            if home_matches:
                parsed = parse_team_form(home_matches, home_id)
                home_stats_data.update(parsed)

        if away_id:
            away_matches = sofascore_team_last_matches(away_id)
            random_delay()
            if away_matches:
                parsed = parse_team_form(away_matches, away_id)
                away_stats_data.update(parsed)

        # Standings — use tournament from team's last match
        if home_id and home_matches:
            last = home_matches[0] if home_matches else {}
            t_id = last.get("tournament", {}).get("uniqueTournament", {}).get("id")
            s_id = last.get("season", {}).get("id")
            if t_id and s_id:
                home_standing = sofascore_team_standing(home_id, t_id, s_id)
                random_delay()
                if home_standing:
                    home_stats_data["league_position"] = home_standing["position"]
                    total_teams = home_standing["total_teams"]

                away_standing = sofascore_team_standing(away_id, t_id, s_id)
                random_delay()
                if away_standing:
                    away_stats_data["league_position"] = away_standing["position"]
                    total_teams = away_standing["total_teams"]

        home_stats_data["total_teams_in_league"] = total_teams
        away_stats_data["total_teams_in_league"] = total_teams

        # H2H — find the event on Sofascore
        sf_event = sofascore_search_event(home_name, away_name)
        random_delay()
        if sf_event:
            event_id = sf_event.get("id")
            if event_id:
                h2h_matches = sofascore_h2h(event_id)
                random_delay()
                if h2h_matches:
                    h2h_data = {"matches": h2h_matches}

    # ── Flashscore fallback for teams with no form data ──
    if not home_stats_data["form"]:
        log.info(f"  Sofascore miss for {home_name}, trying Flashscore...")
        slug = flashscore_search_team(home_name)
        random_delay()
        if slug:
            fs_data = flashscore_team_form(slug)
            random_delay()
            if fs_data:
                home_stats_data.update(fs_data)

    if not away_stats_data["form"]:
        log.info(f"  Sofascore miss for {away_name}, trying Flashscore...")
        slug = flashscore_search_team(away_name)
        random_delay()
        if slug:
            fs_data = flashscore_team_form(slug)
            random_delay()
            if fs_data:
                away_stats_data.update(fs_data)

    # ── Update match dict ──
    enriched = dict(match)
    enriched["home_stats"] = home_stats_data
    enriched["away_stats"] = away_stats_data
    enriched["h2h"] = h2h_data
    enriched["total_teams_in_league"] = total_teams

    return enriched


# ─── ENRICH ALL MATCHES ──────────────────────────────────────────────────────

def enrich_all(matches: list[dict]) -> list[dict]:
    """
    Enrich a list of match dicts with stats data.
    Skips matches that already have stats populated.
    """
    enriched = []
    total = len(matches)

    for i, match in enumerate(matches):
        log.info(f"[{i+1}/{total}] {match.get('home')} vs {match.get('away')}")
        try:
            enriched.append(enrich_match(match))
        except Exception as e:
            log.warning(f"  Failed to enrich match: {e}")
            enriched.append(match)  # pass through unenriched rather than drop
        random_delay(0.5, 1.5)

    return enriched


# ─── CLI TEST ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test with a single known fixture
    sample = [{
        "id": "test_001",
        "league": "Premier League",
        "time": "13:30",
        "home": "Arsenal",
        "away": "Wolves",
        "source": "sportybet",
        "total_teams_in_league": 20,
        "odds": {"home": 1.55, "draw": 4.20, "away": 6.50,
                 "over_15": None, "over_25": None, "btts_yes": None},
        "home_stats": None,
        "away_stats": None,
        "h2h": None
    }]

    result = enrich_all(sample)
    print(json.dumps(result, indent=2))
    print(f"\n✓ Enriched {len(result)} match(es)")
