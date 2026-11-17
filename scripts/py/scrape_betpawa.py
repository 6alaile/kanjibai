"""
scrape_betpawa.py — Scout BetPawa Odds Scraper

BetPawa's web app is a Next.js SPA backed by a sportsbook JSON API:

  events page  ->  /api/sportsbook/v4/events/lists/by-queries?q=<query>
  event page   ->  /api/sportsbook/v4/events/<event id>

Both answer plain JSON when sent ``accept: application/json`` plus the brand
headers, so no browser is needed — the Playwright DOM scrape this replaced was
slower, capped at the 20 events the page initially loads, and could not re-query
odds cheaply.

The list query's ``view.marketTypes`` chooses which markets ride along with
each event; we ask for the seven the scoring engine prices its picks from,
plus a second pass for the combo markets (the API allows seven per query).

The bookmaker prints 12-hour times in its DOM, but the API gives an ISO
kick-off — times are converted to 24-hour EAT here.

Fixture contract (written through stats.py into data/enriched.json):

{
  "id": "<event id>",
  "league": "Football / <region> / <competition>",
  "home": "...", "away": "...",
  "time": "HH:MM",                       # EAT, 24-hour
  "odds": {"home": 1.97, "draw": 3.54, "away": 3.45},   # 1X2
  "markets": {
    "1x2":         {"home": .., "draw": .., "away": ..},
    "dc":          {"home_draw": .., "draw_away": .., "home_away": ..},
    "btts":        {"yes": .., "no": ..},
    "total_goals": {"1.5": {"over": .., "under": ..}, ...},
    "home_goals":  {"0.5": {"over": .., "under": ..}, ...},
    "away_goals":  {"0.5": {"over": .., "under": ..}, ...},
    "result_total":{"1.5": {"home_over": .., "draw_over": .., "away_over": .., ...}},
    "dc_btts":     {"home_draw_yes": .., "home_away_no": .., ...},
    "x1x2_btts":   {"home_yes": .., "draw_no": .., ...}
  },
  "odds_captured_at": "ISO8601 EAT",
  "source": "betpawa"
}

Markets the event does not offer (or has suspended) are absent, so a pick
without a price simply renders without one.
"""

import json
import logging
import urllib.parse
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

logging.basicConfig(level=logging.INFO, format="[betpawa] %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))

LIST_URL = "https://www.betpawa.co.tz/api/sportsbook/v4/events/lists/by-queries?q="
PAGE_SIZE = 100          # the API refuses more than 100 per page
MAX_PAGES = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# The markets the scoring engine prices its picks from.  The list API allows
# at most seven market types per query, so the two combo markets ride along
# in a second pass that is merged by event id (see upcoming_events).
MARKET_DEFS = {
    "3743": "1x2",             # 1X2 - FT
    "4693": "dc",              # Double Chance - FT
    "3795": "btts",            # Both Teams To Score - FT
    "5000": "total_goals",     # Total Score Over/Under - FT
    "5006": "home_goals",      # Total Score Over/Under - FT - Home Team
    "5003": "away_goals",      # Total Score Over/Under - FT - Away Team
    "1096755": "result_total",  # 1X2 and Totals - FT
    "28000014": "dc_btts",     # Double Chance and Both Teams To Score - FT
    "3591790": "x1x2_btts",    # 1X2 and Both Teams To Score - FT
}
VIEW_MARKET_TYPES = ["3743", "4693", "3795", "5000", "5006", "5003", "1096755"]
COMBO_VIEW_MARKET_TYPES = ["28000014", "3591790"]

# Flat price lists (the price name carries the outcome): name -> contract key.
FLAT_PRICE_KEYS = {
    "1x2": {"1": "home", "X": "draw", "2": "away"},
    "dc": {"1X": "home_draw", "X2": "draw_away", "12": "home_away"},
    "btts": {"Yes": "yes", "No": "no"},
    "dc_btts": {"1X - Yes": "home_draw_yes", "1X - No": "home_draw_no",
                "12 - Yes": "home_away_yes", "12 - No": "home_away_no",
                "X2 - Yes": "draw_away_yes", "X2 - No": "draw_away_no"},
    "x1x2_btts": {"1 - Yes": "home_yes", "1 - No": "home_no",
                  "X - Yes": "draw_yes", "X - No": "draw_no",
                  "2 - Yes": "away_yes", "2 - No": "away_no"},
}

EXCLUDED_KEYWORDS = {"israel", "ligat", "leumit", "toto cup"}

_HEADERS = {
    "User-Agent": USER_AGENT,
    "accept": "application/json",
    "x-pawa-language": "en",
    "x-device-fingerprint": uuid.uuid4().hex,
    "x-pawa-brand": "betpawa-tanzania",
    "devicetype": "web",
}


def is_excluded(league_name: str) -> bool:
    """Check if league should be excluded (Israeli leagues)."""
    name = (league_name or "").lower()
    return any(kw in name for kw in EXCLUDED_KEYWORDS)


def _api_get(url: str, timeout: int = 30) -> Dict:
    response = requests.get(url, headers=_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _to_eat(iso: str) -> Optional[datetime]:
    """API kick-off (UTC ISO) as an EAT datetime."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EAT)
    except (ValueError, AttributeError, TypeError):
        return None


def _page_events(skip: int, view_types: List[str]) -> List[Dict]:
    """One page of upcoming football events, oldest first."""
    query = {
        "queries": [{
            "query": {"eventType": "UPCOMING", "categories": ["2"],
                      "zones": {}, "hasOdds": True},
            "view": {"marketTypes": view_types},
            "skip": skip,
            "take": PAGE_SIZE,
        }]
    }
    data = _api_get(LIST_URL + urllib.parse.quote(json.dumps(query)))
    try:
        return data["responses"][0]["responses"]
    except (KeyError, IndexError, TypeError):
        return []


def _day_events(date_str: str, view_types: List[str]) -> List[Dict]:
    """Every upcoming event kicking off on ``date_str`` (EAT) for one
    market-type view, deduped and in kick-off order.

    The feed is ordered by kick-off, so paging stops once it has moved past
    the requested day.
    """
    events: List[Dict] = []
    seen = set()
    for skip in range(0, PAGE_SIZE * MAX_PAGES, PAGE_SIZE):
        page = _page_events(skip, view_types)
        if not page:
            break
        first = _to_eat(page[0].get("startTime", ""))
        if first and first.strftime("%Y-%m-%d") > date_str:
            break
        for event in page:
            event_id = str(event.get("id") or "")
            when = _to_eat(event.get("startTime", ""))
            if not event_id or not when or event_id in seen:
                continue
            if when.strftime("%Y-%m-%d") == date_str:
                seen.add(event_id)
                events.append(event)
        if len(page) < PAGE_SIZE:
            break
    return events


def upcoming_events(date_str: str) -> List[Dict]:
    """The day's events with the core and combo market views merged.

    The list API accepts at most seven market types per query, so the combo
    markets (Double Chance & BTTS, 1X2 & BTTS) come from a second paging
    pass, merged into the core events by id.  Events the second pass finds
    but the core pass did not are skipped — without 1X2 they cannot be
    priced anyway.
    """
    events = _day_events(date_str, VIEW_MARKET_TYPES)
    by_id = {str(e.get("id") or ""): e for e in events}
    for event in _day_events(date_str, COMBO_VIEW_MARKET_TYPES):
        target = by_id.get(str(event.get("id") or ""))
        if not target:
            continue
        have = {str((m.get("marketType") or {}).get("id"))
                for m in target.get("markets") or []}
        for market in event.get("markets") or []:
            if str((market.get("marketType") or {}).get("id")) not in have:
                target.setdefault("markets", []).append(market)
    return events


def _live(price: Dict) -> bool:
    return (not price.get("suspended")
            and isinstance(price.get("odds"), (int, float)))


def parse_markets(event: Dict) -> Dict:
    """The event's markets in the fixture contract's shape.

    Flat markets (1X2, double chance, BTTS) map price names directly; the
    rest are keyed by goal line from each row's ``specifier.total``.
    """
    out: Dict = {}
    for market in event.get("markets") or []:
        market_type = market.get("marketType") or {}
        key = MARKET_DEFS.get(str(market_type.get("id")))
        if not key:
            continue

        if key in FLAT_PRICE_KEYS:
            mapping = FLAT_PRICE_KEYS[key]
            bucket: Dict = {}
            for row in market.get("row") or []:
                for price in row.get("prices") or []:
                    dst = mapping.get(price.get("name"))
                    if dst and _live(price):
                        bucket[dst] = price["odds"]
            if bucket:
                out[key] = bucket
            continue

        lines: Dict = {}
        for row in market.get("row") or []:
            total = (row.get("specifier") or {}).get("total")
            if total is None:
                continue
            bucket = lines.setdefault(str(total), {})
            for price in row.get("prices") or []:
                if not _live(price):
                    continue
                name = price.get("name") or ""
                if key == "result_total":
                    # "1 - Over" / "X - Under" / "2 - Over" ...
                    outcome, _, side = name.partition(" - ")
                    outcome = {"1": "home", "X": "draw", "2": "away"}.get(outcome)
                    side = side.lower()
                    if outcome and side in ("over", "under"):
                        bucket[f"{outcome}_{side}"] = price["odds"]
                else:
                    side = name.lower()
                    if side in ("over", "under"):
                        bucket[side] = price["odds"]
        if lines:
            out[key] = lines
    return out


def event_fixture(event: Dict, captured_at: str) -> Optional[Dict]:
    """One API event as a fixture dict, or None if it cannot be used."""
    participants = event.get("participants") or []
    if len(participants) < 2:
        return None
    home, away = participants[0].get("name"), participants[1].get("name")
    if not home or not away:
        return None

    region = (event.get("region") or {}).get("name") or ""
    competition = (event.get("competition") or {}).get("name") or ""
    category = (event.get("category") or {}).get("name") or "Football"
    league = " / ".join(p for p in (category, region, competition) if p)
    if is_excluded(league):
        return None

    markets = parse_markets(event)
    odds = markets.get("1x2") or {}
    if not all(k in odds for k in ("home", "draw", "away")):
        return None

    kickoff = _to_eat(event.get("startTime", ""))
    if not kickoff:
        return None

    return {
        "id": str(event["id"]),
        "league": league,
        "home": home,
        "away": away,
        "time": kickoff.strftime("%H:%M"),
        "odds": {"home": odds["home"], "draw": odds["draw"], "away": odds["away"]},
        "markets": markets,
        "odds_captured_at": captured_at,
        "home_stats": None,
        "away_stats": None,
        "h2h": None,
        "source": "betpawa",
    }


def fetch_fixtures(date_str: Optional[str] = None) -> List[Dict]:
    """Fixtures + odds + markets for the scout date, kick-off order.

    One to three list-API pages cover the whole day (the old browser scrape
    saw only the 20 events the page loads before scrolling).
    """
    if not date_str:
        date_str = datetime.now(EAT).strftime("%Y-%m-%d")

    captured_at = datetime.now(EAT).isoformat()
    log.info(f"Fetching BetPawa fixtures for {date_str}...")

    fixtures = []
    for event in upcoming_events(date_str):
        fixture = event_fixture(event, captured_at)
        if fixture:
            fixtures.append(fixture)

    fixtures.sort(key=lambda m: m["time"])
    log.info(f"Total BetPawa matches: {len(fixtures)}")
    return fixtures


if __name__ == "__main__":
    fixtures = fetch_fixtures()
    print(json.dumps(fixtures, indent=2))
    print(f"\n[OK] {len(fixtures)} fixtures fetched from BetPawa")
