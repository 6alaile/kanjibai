"""
main.py — Scout Orchestrator
Runs the daily pipeline:
1. Scrape fixtures + odds (scrape.py)
2. Enrich with form/H2H/standings (stats.py)
3. Write enriched.json — scoring happens in the browser

No scoring here. Browser applies filters and scores in real time.
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


def run():
    start = datetime.now(EAT)
    log.info("=" * 50)
    log.info(f"Scout daily scan starting — {start.strftime('%Y-%m-%d %H:%M EAT')}")
    log.info("=" * 50)

    # Step 1: Fetch fixtures + odds
    log.info("Step 1/2: Fetching fixtures...")
    raw_fixtures = fetch_fixtures()
    scanned_count = len(raw_fixtures)

    if not raw_fixtures:
        log.warning("No fixtures found. Writing empty enriched.json.")
        with open(OUTPUT_PATH, "w") as f:
            json.dump({
                "meta": {
                    "date": start.strftime("%Y-%m-%d"),
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

    # Write enriched.json — all matches with raw stats, no scoring
    end = datetime.now(EAT)
    elapsed = round((end - start).total_seconds(), 1)

    output = {
        "meta": {
            "date": start.strftime("%Y-%m-%d"),
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
