"""
main.py — Scout Orchestrator
Fetches fixtures + enriches with stats → writes enriched.json
Scoring happens in the browser (app.js).

Data sources:
- BetPawa (Playwright) → primary odds
- API-Football → fallback odds
- Transfermarkt → enrichment (team form, league position, H2H)
- football-data.org → enrichment (free tier leagues)
"""

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from scrape import fetch_fixtures as fetch_api_fixtures
from stats import enrich_all, get_league_code

from scrape_betpawa import fetch_fixtures as fetch_betpawa_fixtures

# Add transfermarkt scraper to path
sys.path.insert(0, str(Path(__file__).parent))
from transfermarkt_scraper import run_daily_scrape

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


def run():
    start = datetime.now(EAT)
    log.info("=" * 50)
    log.info(f"Scout daily scan starting — {start.strftime('%Y-%m-%d %H:%M EAT')}")
    log.info("=" * 50)

    today = get_today_eat()

    # ── Step 1: Fetch fixtures + odds ────────────────────────────────────
    log.info("Step 1/3: Fetching fixtures from BetPawa (primary) ...")

    # Try BetPawa first (Playwright-rendered odds)
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

    log.info(f"  {len(raw_fixtures)} fixtures fetched")

    # ── Step 2: Run Transfermarkt scraper for teams in today's fixtures ──
    log.info("Step 2/3: Running Transfermarkt scraper for today's teams...")
    try:
        run_daily_scrape(max_teams=20, betpawa_fixtures=raw_fixtures)
        log.info("  Transfermarkt scrape complete")
    except Exception as e:
        log.warning(f"  Transfermarkt scrape failed: {e}")
        import traceback
        traceback.print_exc()

    # Step 3: Enrich with stats (football-data.org free tier + Transfermarkt cache)
    log.info("Step 3/3: Enriching with form, H2H, standings...")
    enriched = enrich_all(raw_fixtures)
    log.info(f"  {len(enriched)} matches enriched")

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
    log.info("=" * 50)


if __name__ == "__main__":
    run()