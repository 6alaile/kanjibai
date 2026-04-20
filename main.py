"""
main.py — Scout Orchestrator
Runs the full daily pipeline:
1. Scrape fixtures + odds (scrape.py)
2. Enrich with form/H2H/standings (stats.py)
3. Score and filter (scorer.py)
4. Write results.json (read by frontend)
5. Send Telegram notification (notify.py)

Run: python main.py
"""

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from scrape import fetch_fixtures
from stats import enrich_all
from scorer import score_all_matches, FilterConfig
from notify import notify

logging.basicConfig(
    level=logging.INFO,
    format="[scout] %(asctime)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))
OUTPUT_PATH = Path(__file__).parent / "results.json"


# ─── DEFAULT CONFIG ──────────────────────────────────────────────────────────
# These are the defaults. The frontend sliders write back a config.json
# which overrides these at runtime if present.

DEFAULT_CONFIG = FilterConfig(
    home_odds_max=2.00,
    away_odds_min=5.00,
    min_form_matches=5,
    league_position_weight=0.5,
    opp_strength_weight=0.5,
    markets=["1x2", "ou", "btts", "combo"]
)

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> FilterConfig:
    """Load filter config from config.json if it exists, else use defaults."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            cfg = FilterConfig(
                home_odds_max=float(data.get("homeOddsMax", DEFAULT_CONFIG.home_odds_max)),
                away_odds_min=float(data.get("awayOddsMin", DEFAULT_CONFIG.away_odds_min)),
                min_form_matches=int(data.get("minFormMatches", DEFAULT_CONFIG.min_form_matches)),
                league_position_weight=float(data.get("leaguePositionWeight", DEFAULT_CONFIG.league_position_weight)),
                opp_strength_weight=float(data.get("oppStrengthWeight", DEFAULT_CONFIG.opp_strength_weight)),
                markets=data.get("markets", DEFAULT_CONFIG.markets)
            )
            log.info(f"Loaded config from config.json")
            return cfg
        except Exception as e:
            log.warning(f"Failed to parse config.json: {e}. Using defaults.")
    return DEFAULT_CONFIG


def write_results(results: list[dict], meta: dict):
    """Write results.json with metadata envelope."""
    output = {
        "meta": meta,
        "matches": results
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"results.json written — {len(results)} qualified matches")


def run():
    start = datetime.now(EAT)
    log.info("=" * 50)
    log.info(f"Scout daily scan starting — {start.strftime('%Y-%m-%d %H:%M EAT')}")
    log.info("=" * 50)

    cfg = load_config()

    # ── Step 1: Fetch fixtures ──
    log.info("Step 1/4: Fetching fixtures...")
    raw_fixtures = fetch_fixtures()
    scanned_count = len(raw_fixtures)

    if not raw_fixtures:
        log.warning("No fixtures found. Writing empty results.")
        write_results([], {
            "date": start.strftime("%Y-%m-%d"),
            "generatedAt": start.isoformat(),
            "scanned": 0,
            "qualified": 0,
            "error": "No fixtures returned by scraper"
        })
        sys.exit(0)

    log.info(f"  {scanned_count} fixtures fetched")

    # ── Step 2: Enrich with stats ──
    log.info("Step 2/4: Enriching with form, H2H, standings...")
    enriched = enrich_all(raw_fixtures)
    log.info(f"  {len(enriched)} matches enriched")

    # ── Step 3: Score and filter ──
    log.info("Step 3/4: Scoring and filtering...")
    results = score_all_matches(enriched, cfg)
    qualified_count = len(results)
    log.info(f"  {qualified_count} matches passed filters")

    # ── Step 4: Write results.json ──
    end = datetime.now(EAT)
    elapsed = round((end - start).total_seconds(), 1)

    meta = {
        "date": start.strftime("%Y-%m-%d"),
        "generatedAt": end.isoformat(),
        "scanned": scanned_count,
        "qualified": qualified_count,
        "elapsedSeconds": elapsed,
        "config": {
            "homeOddsMax": cfg.home_odds_max,
            "awayOddsMin": cfg.away_odds_min,
            "minFormMatches": cfg.min_form_matches,
            "leaguePositionWeight": cfg.league_position_weight,
            "oppStrengthWeight": cfg.opp_strength_weight,
            "markets": cfg.markets
        }
    }

    write_results(results, meta)

    # ── Step 5: Telegram ──
    log.info("Step 4/4: Sending Telegram notification...")
    notify(results, scanned_count=scanned_count)

    log.info("=" * 50)
    log.info(f"Done in {elapsed}s — {qualified_count}/{scanned_count} qualified")
    log.info("=" * 50)


if __name__ == "__main__":
    run()
