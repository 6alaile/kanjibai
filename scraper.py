"""
scrape.py — Scout Fixture & Odds Scraper
Scrapes today's football fixtures and 1X2 odds from SportyBet Tanzania.
Falls back to BetPawa if SportyBet returns no data.

Dependencies: requests, beautifulsoup4, playwright (optional, for JS-rendered pages)
Install: pip install requests beautifulsoup4 playwright
         playwright install chromium
"""

import json
import time
import random
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="[scrape] %(message)s")
log = logging.getLogger(__name__)

# ─── CONFIG ─────────────────────────────────────────────────────────────────

# Leagues to EXCLUDE (Israeli + any tournament-only competitions)
EXCLUDED_LEAGUES = {
    "israeli premier league",
    "ligat ha'al",
    "leumit league",
    "israel state cup",
    "toto cup",
    "israel",
}

# EAT = UTC+3
EAT = timezone(timedelta(hours=3))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.sportybet.com/tz/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ─── DATA STRUCTURE ─────────────────────────────────────────────────────────

@dataclass
class RawFixture:
    id: str
    league: str
    home: str
    away: str
    time: str           # HH:MM EAT
    odds_home: float
    odds_draw: float
    odds_away: float
    source: str         # "sportybet" | "betpawa"


# ─── HELPERS ────────────────────────────────────────────────────────────────

def is_excluded_league(league_name: str) -> bool:
    return any(excl in league_name.lower() for excl in EXCLUDED_LEAGUES)


def random_delay(min_s: float = 1.0, max_s: float = 3.0):
    """Polite delay between requests."""
    time.sleep(random.uniform(min_s, max_s))


def safe_float(val: str, fallback: float = 0.0) -> float:
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return fallback


def utc_to_eat(utc_str: str) -> str:
    """Convert UTC ISO string to HH:MM EAT."""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        eat_dt = dt.astimezone(EAT)
        return eat_dt.strftime("%H:%M")
    except Exception:
        return utc_str


# ─── SPORTYBET API ATTEMPT ───────────────────────────────────────────────────
# SportyBet uses an internal API. We attempt the JSON endpoint first
# before falling back to HTML scraping.

SPORTYBET_API = "https://www.sportybet.com/api/tz/factsCenter/publicSportEvents"

def fetch_sportybet_api(date_str: Optional[str] = None) -> list[RawFixture]:
    """
    Attempt to pull fixtures from SportyBet's internal API.
    date_str format: YYYY-MM-DD (defaults to today EAT)
    """
    if not date_str:
        date_str = datetime.now(EAT).strftime("%Y-%m-%d")

    params = {
        "sportId": "sr:sport:1",   # football
        "marketId": "1",           # 1X2
        "date": date_str,
        "_t": int(time.time() * 1000)
    }

    log.info(f"Trying SportyBet API for {date_str}...")

    try:
        resp = SESSION.get(SPORTYBET_API, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        log.warning(f"SportyBet API HTTP error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        log.warning(f"SportyBet API request failed: {e}")
        return []
    except json.JSONDecodeError:
        log.warning("SportyBet API returned non-JSON response")
        return []

    fixtures = []
    tournaments = data.get("data", {}).get("tournaments", []) or []

    for tournament in tournaments:
        league_name = tournament.get("name", "Unknown League")

        if is_excluded_league(league_name):
            log.info(f"  Skipping excluded league: {league_name}")
            continue

        events = tournament.get("events", []) or []

        for event in events:
            try:
                home = event.get("homeTeamName", "")
                away = event.get("awayTeamName", "")
                event_id = str(event.get("eventId", ""))
                start_time = event.get("estimateStartTime", "")

                # Parse kick-off time
                if start_time:
                    try:
                        ts_seconds = int(start_time) / 1000
                        dt = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
                        kick_off = dt.astimezone(EAT).strftime("%H:%M")
                    except Exception:
                        kick_off = "TBD"
                else:
                    kick_off = "TBD"

                # Extract 1X2 odds from markets
                odds_home = odds_draw = odds_away = 0.0
                markets = event.get("markets", []) or []

                for market in markets:
                    if market.get("id") in ("1", 1):  # 1X2 market
                        outcomes = market.get("outcomes", []) or []
                        for outcome in outcomes:
                            desc = str(outcome.get("desc", "")).upper()
                            odds_val = safe_float(outcome.get("odds", 0))
                            if desc in ("1", "HOME", "H"):
                                odds_home = odds_val
                            elif desc in ("X", "DRAW", "D"):
                                odds_draw = odds_val
                            elif desc in ("2", "AWAY", "A"):
                                odds_away = odds_val

                if not (odds_home and odds_draw and odds_away):
                    continue

                fixtures.append(RawFixture(
                    id=f"sb_{event_id}",
                    league=league_name,
                    home=home,
                    away=away,
                    time=kick_off,
                    odds_home=odds_home,
                    odds_draw=odds_draw,
                    odds_away=odds_away,
                    source="sportybet"
                ))

            except Exception as e:
                log.warning(f"  Error parsing event: {e}")
                continue

        random_delay(0.3, 0.8)

    log.info(f"SportyBet API: found {len(fixtures)} fixtures")
    return fixtures


# ─── SPORTYBET HTML FALLBACK ─────────────────────────────────────────────────

SPORTYBET_URL = "https://www.sportybet.com/tz/sport/football"

def fetch_sportybet_html() -> list[RawFixture]:
    """
    HTML scrape fallback. SportyBet is heavily JS-rendered so this
    attempts to get what's available in the initial HTML.
    For full JS rendering, use fetch_sportybet_playwright() below.
    """
    log.info("Trying SportyBet HTML scrape...")

    try:
        resp = SESSION.get(SPORTYBET_URL, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.warning(f"SportyBet HTML fetch failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    fixtures = []

    # SportyBet injects some data as JSON in <script> tags
    scripts = soup.find_all("script")
    for script in scripts:
        content = script.string or ""
        if "homeTeamName" in content or "tournaments" in content:
            try:
                # Find JSON blob
                start = content.find("{")
                end = content.rfind("}") + 1
                if start != -1 and end > start:
                    blob = json.loads(content[start:end])
                    # Try to parse same structure as API
                    tournaments = blob.get("data", {}).get("tournaments", []) or []
                    for t in tournaments:
                        league = t.get("name", "Unknown")
                        if is_excluded_league(league):
                            continue
                        for event in t.get("events", []):
                            home = event.get("homeTeamName", "")
                            away = event.get("awayTeamName", "")
                            event_id = str(event.get("eventId", ""))
                            kick_off = "TBD"

                            odds_home = odds_draw = odds_away = 0.0
                            for market in event.get("markets", []):
                                if market.get("id") in ("1", 1):
                                    for outcome in market.get("outcomes", []):
                                        desc = str(outcome.get("desc", "")).upper()
                                        val = safe_float(outcome.get("odds", 0))
                                        if desc in ("1", "HOME", "H"):
                                            odds_home = val
                                        elif desc in ("X", "DRAW", "D"):
                                            odds_draw = val
                                        elif desc in ("2", "AWAY", "A"):
                                            odds_away = val

                            if odds_home and odds_draw and odds_away:
                                fixtures.append(RawFixture(
                                    id=f"sb_{event_id}",
                                    league=league,
                                    home=home,
                                    away=away,
                                    time=kick_off,
                                    odds_home=odds_home,
                                    odds_draw=odds_draw,
                                    odds_away=odds_away,
                                    source="sportybet"
                                ))
            except Exception:
                continue

    log.info(f"SportyBet HTML: found {len(fixtures)} fixtures")
    return fixtures


# ─── PLAYWRIGHT (JS-RENDERED FALLBACK) ───────────────────────────────────────

def fetch_sportybet_playwright() -> list[RawFixture]:
    """
    Full JS rendering via Playwright — use when API and HTML both fail.
    Requires: pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed. Skipping JS render fallback.")
        return []

    log.info("Trying SportyBet via Playwright...")
    fixtures = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers(HEADERS)

        intercepted = []

        def handle_response(response):
            if "factsCenter" in response.url or "publicSportEvents" in response.url:
                try:
                    intercepted.append(response.json())
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            page.goto(SPORTYBET_URL, timeout=30000, wait_until="networkidle")
            page.wait_for_timeout(3000)
        except Exception as e:
            log.warning(f"Playwright navigation error: {e}")
            browser.close()
            return []

        browser.close()

        # Parse any intercepted API responses
        for data in intercepted:
            tournaments = data.get("data", {}).get("tournaments", []) or []
            for t in tournaments:
                league = t.get("name", "Unknown")
                if is_excluded_league(league):
                    continue
                for event in t.get("events", []):
                    try:
                        home = event.get("homeTeamName", "")
                        away = event.get("awayTeamName", "")
                        event_id = str(event.get("eventId", ""))

                        start_time = event.get("estimateStartTime", "")
                        kick_off = "TBD"
                        if start_time:
                            ts = int(start_time) / 1000
                            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                            kick_off = dt.astimezone(EAT).strftime("%H:%M")

                        odds_home = odds_draw = odds_away = 0.0
                        for market in event.get("markets", []):
                            if market.get("id") in ("1", 1):
                                for outcome in market.get("outcomes", []):
                                    desc = str(outcome.get("desc", "")).upper()
                                    val = safe_float(outcome.get("odds", 0))
                                    if desc in ("1", "HOME", "H"):
                                        odds_home = val
                                    elif desc in ("X", "DRAW", "D"):
                                        odds_draw = val
                                    elif desc in ("2", "AWAY", "A"):
                                        odds_away = val

                        if odds_home and odds_draw and odds_away:
                            fixtures.append(RawFixture(
                                id=f"sb_{event_id}",
                                league=league,
                                home=home,
                                away=away,
                                time=kick_off,
                                odds_home=odds_home,
                                odds_draw=odds_draw,
                                odds_away=odds_away,
                                source="sportybet"
                            ))
                    except Exception:
                        continue

    log.info(f"Playwright: found {len(fixtures)} fixtures")
    return fixtures


# ─── BETPAWA FALLBACK ────────────────────────────────────────────────────────

BETPAWA_URL = "https://www.betpawa.co.tz/events"

def fetch_betpawa() -> list[RawFixture]:
    """
    BetPawa fallback — attempts HTML scrape.
    BetPawa's structure may vary; this targets common patterns.
    """
    log.info("Trying BetPawa fallback...")

    today = datetime.now(EAT).strftime("%Y-%m-%d")
    params = {"sport": "football", "date": today}

    try:
        resp = SESSION.get(BETPAWA_URL, params=params, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.warning(f"BetPawa fetch failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    fixtures = []
    fixture_index = 0

    # BetPawa wraps events in rows — adjust selectors if structure changes
    event_rows = soup.select(".event-row, .match-row, [data-event-id]")

    for row in event_rows:
        try:
            league = row.select_one(".league-name, .competition-name, .event-league")
            league_name = league.get_text(strip=True) if league else "Unknown"

            if is_excluded_league(league_name):
                continue

            home_el = row.select_one(".home-team, .team-home, [data-team='home']")
            away_el = row.select_one(".away-team, .team-away, [data-team='away']")
            home = home_el.get_text(strip=True) if home_el else ""
            away = away_el.get_text(strip=True) if away_el else ""

            if not home or not away:
                continue

            time_el = row.select_one(".event-time, .match-time, .kick-off")
            kick_off = time_el.get_text(strip=True) if time_el else "TBD"

            odds_els = row.select(".odds-value, .odd-btn, [data-market='1x2'] span")
            if len(odds_els) >= 3:
                odds_home = safe_float(odds_els[0].get_text(strip=True))
                odds_draw = safe_float(odds_els[1].get_text(strip=True))
                odds_away = safe_float(odds_els[2].get_text(strip=True))
            else:
                continue

            if not (odds_home and odds_draw and odds_away):
                continue

            event_id = row.get("data-event-id", f"bp_{fixture_index}")
            fixture_index += 1

            fixtures.append(RawFixture(
                id=f"bp_{event_id}",
                league=league_name,
                home=home,
                away=away,
                time=kick_off,
                odds_home=odds_home,
                odds_draw=odds_draw,
                odds_away=odds_away,
                source="betpawa"
            ))

        except Exception as e:
            log.warning(f"  BetPawa row parse error: {e}")
            continue

    log.info(f"BetPawa: found {len(fixtures)} fixtures")
    return fixtures


# ─── DEDUP ───────────────────────────────────────────────────────────────────

def deduplicate(fixtures: list[RawFixture]) -> list[RawFixture]:
    """Remove duplicate fixtures by normalised team name pair."""
    seen = set()
    unique = []
    for f in fixtures:
        key = (f.home.lower().strip(), f.away.lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


# ─── MAIN ENTRY ─────────────────────────────────────────────────────────────

def fetch_fixtures(date_str: Optional[str] = None) -> list[dict]:
    """
    Main entry point. Returns list of fixture dicts compatible
    with scorer.py's score_all_matches() input format.

    Tries in order:
    1. SportyBet internal API
    2. SportyBet HTML scrape
    3. SportyBet Playwright (JS render)
    4. BetPawa HTML fallback
    """
    fixtures: list[RawFixture] = []

    # Attempt chain
    fixtures = fetch_sportybet_api(date_str)

    if not fixtures:
        log.info("API returned nothing, trying HTML scrape...")
        fixtures = fetch_sportybet_html()

    if not fixtures:
        log.info("HTML scrape returned nothing, trying Playwright...")
        fixtures = fetch_sportybet_playwright()

    if not fixtures:
        log.info("All SportyBet methods failed, trying BetPawa...")
        fixtures = fetch_betpawa()

    if not fixtures:
        log.warning("No fixtures found from any source.")
        return []

    fixtures = deduplicate(fixtures)
    log.info(f"Total unique fixtures after dedup: {len(fixtures)}")

    # Convert to scorer-compatible format
    # Note: home_stats and away_stats are populated by stats.py
    # This output is the partial match dict — stats.py enriches it
    result = []
    for f in fixtures:
        result.append({
            "id": f.id,
            "league": f.league,
            "time": f.time,
            "home": f.home,
            "away": f.away,
            "source": f.source,
            "total_teams_in_league": 20,  # default; stats.py updates this
            "odds": {
                "home": f.odds_home,
                "draw": f.odds_draw,
                "away": f.odds_away,
                "over_15": None,
                "over_25": None,
                "btts_yes": None
            },
            # Placeholders — filled by stats.py
            "home_stats": None,
            "away_stats": None,
            "h2h": None
        })

    return result


# ─── CLI TEST ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    fixtures = fetch_fixtures()
    print(json.dumps(fixtures[:3], indent=2))
    print(f"\n✓ {len(fixtures)} total fixtures fetched")
