"""
main.py — Scout Orchestrator
Fetches fixtures + enriches with stats → writes enriched.json
Scoring happens in the browser (app.js).

Data sources:
- BetPawa (sportsbook JSON API) → primary odds
- API-Football          → fallback odds
- Flashscore            → enrichment (primary: form, standings, H2H)
- Transfermarkt         → enrichment (fallback for teams not on Flashscore)
                         — PAUSED, see TRANSFERMARKT_FALLBACK in stats.py
"""

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from scrape import fetch_fixtures as fetch_api_fixtures
from stats import enrich_all, TRANSFERMARKT_FALLBACK

from scrape_betpawa import fetch_fixtures as fetch_betpawa_fixtures

sys.path.insert(0, str(Path(__file__).parent))
from transfermarkt_scraper import run_daily_scrape
from flashscore_cache import refresh as refresh_flashscore

CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "transfermarkt_cache.json"


def _ensure_cache_v2():
    """Initialize cache with v2 structure if missing or outdated."""
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
        if cache.get("version") == 2:
            return
    # Create fresh v2 cache
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump({"version": 2, "lastUpdated": None, "leagues": {}}, f, indent=2)

logging.basicConfig(level=logging.INFO, format="[scrape] %(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))
OUTPUT_PATH = Path(__file__).parent.parent.parent / "data" / "enriched.json"
DATE_FILE = Path(__file__).parent.parent.parent / "scout_date.txt"


def get_today_eat() -> str:
    """Get today's date in EAT — reads from scout_date.txt if available."""
    if DATE_FILE.exists():
        date_str = DATE_FILE.read_text().strip()
        if date_str:
            log.info(f"Using EAT date from file: {date_str}")
            return date_str
    date_str = datetime.now(EAT).strftime("%Y-%m-%d")
    log.info(f"Using computed EAT date: {date_str}")
    return date_str


def _covered_by_flashscore(team_name: str, fixtures: list) -> bool:
    """True when Flashscore already has form and a position for this team.

    The per-fixture feed counts: it carries both teams of a fixture, so a club
    the name search never resolves is still covered.
    """
    from flashscore_cache import (has_team as fs_has_team, get_recent_form as fs_get_form,
                                  get_league_position as fs_get_pos,
                                  get_fixture_data as fs_get_fixture)
    for fix in fixtures:
        home, away = fix.get("home"), fix.get("away")
        if team_name not in (home, away):
            continue
        fixture = fs_get_fixture(home, away)
        if fixture:
            side = "home" if team_name == home else "away"
            data = fixture.get(side) or {}
            if data.get("form") and fixture.get(f"{side}_position"):
                return True
        break

    return bool(fs_has_team(team_name)
                and fs_get_form(team_name).get("form")
                and fs_get_pos(team_name) is not None)


def _secs(t0: datetime) -> float:
    """Seconds since t0 — per-step timing for the log."""
    return round((datetime.now(EAT) - t0).total_seconds(), 1)


def run():
    start = datetime.now(EAT)
    log.info("=" * 50)
    log.info(f"Scout daily scan starting — {start.strftime('%Y-%m-%d %H:%M EAT')}")
    log.info("=" * 50)

    today = get_today_eat()

    _ensure_cache_v2()

    # ── Step 1: Fetch fixtures + odds ────────────────────────────────────
    log.info("Step 1/3: Fetching fixtures from BetPawa (primary) ...")
    t0 = datetime.now(EAT)

    # Try BetPawa first (sportsbook JSON API: odds + markets)
    raw_fixtures = fetch_betpawa_fixtures(date_str=today)

    # Fallback to API-Football if BetPawa returned no matches
    if not raw_fixtures:
        log.warning("BetPawa returned no fixtures — falling back to API-Football")
        raw_fixtures = fetch_api_fixtures(date_str=today)
    else:
        log.info(f"  {len(raw_fixtures)} fixtures fetched from BetPawa")

    if not raw_fixtures:
        log.warning("No fixtures found. Writing empty enriched.json.")
        with open(OUTPUT_PATH, "w") as f:
            json.dump({
                "meta": {
                    "date": today,
                    "generatedAt": start.isoformat(),
                    "scanned": 0,
                    "error": "No fixtures returned by any scraper"
                },
                "matches": []
            }, f, indent=2)
        sys.exit(0)

    step1 = _secs(t0)
    log.info(f"  {len(raw_fixtures)} fixtures fetched ({step1}s)")

    # ── Step 2: Refresh the Flashscore cache for today's teams (primary) ──
    log.info("Step 2/3: Refreshing Flashscore cache for today's teams...")
    t0 = datetime.now(EAT)
    team_names = []
    for fixture in raw_fixtures:
        for name in (fixture.get("home"), fixture.get("away")):
            if name and name not in team_names:
                team_names.append(name)
    try:
        refresh_flashscore(team_names, fixtures=raw_fixtures, date_str=today)
        log.info(f"  Flashscore cache refreshed for {len(team_names)} teams")
    except Exception as e:
        log.warning(f"  Flashscore refresh failed: {e}")
        import traceback
        traceback.print_exc()
    step2 = _secs(t0)
    log.info(f"  Step 2/3 done in {step2}s")

    # ── Transfermarkt fallback — paused (stats.py TRANSFERMARKT_FALLBACK) ──
    # Not a pipeline step any more: with the flag off, teams Flashscore
    # cannot resolve stay unresolved and the model falls back to its
    # league-average priors. The branch below only runs if the flag is
    # switched back on.
    from flashscore_cache import (has_team as fs_has_team, get_recent_form as fs_get_form,
                                  get_league_position as fs_get_pos,
                                  get_fixture_data as fs_get_fixture)
    missing = [n for n in team_names if not _covered_by_flashscore(n, raw_fixtures)]
    if missing and not TRANSFERMARKT_FALLBACK:
        log.info(f"Transfermarkt fallback paused — {len(missing)} team(s) left unresolved")
        log.info(f"  Unresolved: {', '.join(missing)}")
    elif missing:
        log.info(f"Transfermarkt scrape for {len(missing)} teams not fully covered by Flashscore...")
        log.info(f"  Missing: {', '.join(missing)}")
        missing_fixtures = [
            f for f in raw_fixtures
            if f.get("home") in missing or f.get("away") in missing
        ]
        try:
            run_daily_scrape(max_teams=len(missing), betpawa_fixtures=missing_fixtures)
            log.info("  Transfermarkt scrape complete")
        except Exception as e:
            log.warning(f"  Transfermarkt scrape failed: {e}")
            import traceback
            traceback.print_exc()
    else:
        log.info("All teams covered by Flashscore — skipping Transfermarkt")

    # ── Step 3: Enrich with form, standings, H2H ─────────────────────────
    log.info("Step 3/3: Enriching with form, H2H, standings...")
    t0 = datetime.now(EAT)
    enriched = enrich_all(raw_fixtures)
    step3 = _secs(t0)
    log.info(f"  {len(enriched)} matches enriched ({step3}s)")

    end = datetime.now(EAT)
    elapsed = round((end - start).total_seconds(), 1)

    output = {
        "meta": {
            "date": today,
            "generatedAt": end.isoformat(),
            "scanned": len(raw_fixtures),
            "elapsedSeconds": elapsed,
        },
        "matches": enriched
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    log.info("=" * 50)
    log.info(f"Done in {elapsed}s — {len(raw_fixtures)} matches written to enriched.json")
    log.info(f"  Step 1 fetch {step1}s · Step 2 flashscore {step2}s · Step 3 enrich {step3}s")
    log.info("=" * 50)


if __name__ == "__main__":
    run()