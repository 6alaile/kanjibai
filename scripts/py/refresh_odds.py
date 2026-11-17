"""
refresh_odds.py — Re-query BetPawa odds for the fixtures already enriched.

Run it nearer kick-off: one list-API pass re-prices every market of the
scout date and rewrites ``odds``, ``markets`` and ``odds_captured_at`` in
data/enriched.json in place.  Fixtures that have already kicked off are
dropped from the file; a future fixture missing from the feed (suspended
or postponed) keeps its last capture.  Flashscore/Transfermarkt data of
the remaining fixtures is untouched, so this takes seconds instead of a
full pipeline run.

    python refresh_odds.py [YYYY-MM-DD]     # default: enriched.json's date
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrape_betpawa import fetch_fixtures, EAT

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
    if not fresh:
        log.warning("feed empty for %s — leaving enriched.json untouched", date_str)
        return 0

    now = datetime.now(EAT)

    def kicked_off(match: dict) -> bool:
        """Kick-off has passed (EAT) — a future fixture absent from the feed
        is suspended or postponed, not played, so it keeps its last odds."""
        try:
            ko = datetime.strptime(f"{date_str} {match.get('time', '')}",
                                   "%Y-%m-%d %H:%M")
        except ValueError:
            return False
        return ko.replace(tzinfo=EAT) <= now

    updated = dropped = 0
    kept = []
    for match in matches:
        fixture = fresh.get(str(match.get("id")))
        if fixture:
            match["odds"] = fixture["odds"]
            match["markets"] = fixture["markets"]
            match["odds_captured_at"] = fixture["odds_captured_at"]
            updated += 1
            kept.append(match)
        elif match.get("source") == "betpawa" and kicked_off(match):
            dropped += 1          # played — absent from the UPCOMING feed
        else:
            kept.append(match)    # suspended/other-source: keep as-is
    doc["matches"] = kept

    OUTPUT_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    log.info("re-priced %d, dropped %d kicked off, kept %d unpriced (%s)",
             updated, dropped, len(kept) - updated, date_str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
