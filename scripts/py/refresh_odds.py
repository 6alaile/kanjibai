"""
refresh_odds.py — Re-query BetPawa odds for the fixtures already enriched.

Run it nearer kick-off: one list-API pass re-prices every market of the
scout date and rewrites ``odds``, ``markets`` and ``odds_captured_at`` in
data/enriched.json in place.  Flashscore/Transfermarkt data is untouched,
so this takes seconds instead of a full pipeline run.

Fixtures that have already kicked off are gone from the UPCOMING feed and
keep their last captured odds.

    python refresh_odds.py [YYYY-MM-DD]     # default: enriched.json's date
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrape_betpawa import fetch_fixtures

logging.basicConfig(level=logging.INFO, format="[odds] %(message)s")
log = logging.getLogger(__name__)

OUTPUT_PATH = Path(__file__).resolve().parents[2] / "data" / "enriched.json"


def main() -> int:
    if not OUTPUT_PATH.exists():
        log.error("no enriched.json at %s", OUTPUT_PATH)
        return 1

    doc = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    matches = doc.get("matches") or []
    date_str = (sys.argv[1] if len(sys.argv) > 1
                else (doc.get("meta") or {}).get("date"))
    if not date_str:
        log.error("no date: pass YYYY-MM-DD or meta.date")
        return 1

    fresh = {m["id"]: m for m in fetch_fixtures(date_str)}

    updated = 0
    for match in matches:
        fixture = fresh.get(str(match.get("id")))
        if not fixture:
            continue
        match["odds"] = fixture["odds"]
        match["markets"] = fixture["markets"]
        match["odds_captured_at"] = fixture["odds_captured_at"]
        updated += 1

    OUTPUT_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    log.info("re-priced %d/%d matches for %s (%d already kicked off)",
             updated, len(matches), date_str, len(matches) - updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
