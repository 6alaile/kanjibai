"""
main.py — Scout Orchestrator
Fetches fixtures + enriches with stats → writes enriched.json
Scoring happens in the browser (app.js).
"""

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from scrape import fetch_fixtures
from stats import enrich_all

logging.basicConfig(level=logging.INFO, format="[scrape] %(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))
OUTPUT_PATH = Path(__file__).parent / "enriched.json"
DATE_FILE = Path(__file__).parent / "scout_date.txt"


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

    # Step 1: Fetch fixtures + odds
    log.info("Step 1/2: Fetching fixtures...")
    raw_fixtures = fetch_fixtures(date_str=today)
    scanned_count = len(raw_fixtures)

    if not raw_fixtures:
        log.warning("No fixtures found. Writing empty enriched.json.")
        with open(OUTPUT_PATH, "w") as f:
            json.dump({
                "meta": {
                    "date": today,
                    "generatedAt": start.isoformat(),
                    "scanned": 0,
                    "error": "No fixtures returned by scraper"
                },
                "matches": []
            }, f, indent=2)
        sys.exit(0)

    log.info(f"  {scanned_count} fixtures fetched")

    # Step 2: Enrich with stats
    log.info("Step 2/2: Enriching with form, H2H, standings...")
    enriched = enrich_all(raw_fixtures)
    log.info(f"  {len(enriched)} matches enriched")

    end = datetime.now(EAT)
    elapsed = round((end - start).total_seconds(), 1)

    output = {
        "meta": {
            "date": today,
            "generatedAt": end.isoformat(),
            "scanned": scanned_count,
            "elapsedSeconds": elapsed,
        },
        "matches": enriched
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    log.info("=" * 50)
    log.info(f"Done in {elapsed}s — {scanned_count} matches written to enriched.json")
    log.info("=" * 50)


if __name__ == "__main__":
    run()