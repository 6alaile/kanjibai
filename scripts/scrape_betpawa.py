"""
scrape_betpawa.py — Scout BetPawa Odds Scraper
Uses Playwright to render dynamic SPAs (Cloudflare-protected).

DOM Structure per match:
  div.SportEvents_eventMatch_acfzx
    ├─ div.ScoreBoard_scoreboardPeriodParticipantNameWrapper__P7Xgx (home)
    ├─ div.ScoreBoard_scoreboardPeriodParticipantNameWrapper__P7Xgx (away)
    ├─ p.SportEvents_subTitle__yJAJG[data-test-id="event-path"]    (league)
    └─ div.BetlineList_betlineList__PWLcK
        ├─ button[data-test-id="odd-{evt}-*"]  (1 / home)
        ├─ button[data-test-id="odd-{evt}-*"]  (X / draw)
        └─ button[data-test-id="odd-{evt}-*"]  (2 / away)
"""

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

from playwright.sync_api import Page, sync_playwright

logging.basicConfig(level=logging.DEBUG, format="[betpawa] %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))

BETPAWA_URLS = [
    "https://www.betpawa.co.tz/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC",
    "https://www.betpawa.ke/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC",
    "https://www.betpawa.ug/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC",
    "https://www.betpawa.co.tz/events/popular?categoryId=2&marketId=1X2",
    "https://www.betpawa.ke/events/popular?categoryId=2&marketId=1X2",
    "https://www.betpawa.ug/events/popular?categoryId=2&marketId=1X2",
]

RENDER_WAIT = 20
MAX_ATTEMPTS = 6

EXCLUDED_COUNTRIES = {"israel"}
EXCLUDED_KEYWORDS = {"israel", "ligat", "leumit", "toto cup"}


def is_excluded(league_name: str) -> bool:
    """Check if league should be excluded (Israeli leagues)."""
    name = league_name.lower()
    return any(kw in name for kw in EXCLUDED_KEYWORDS)


def _safe_float(val: str, fallback: float = 0.0) -> float:
    try:
        return float(val.strip())
    except (TypeError, ValueError):
        return fallback


def _extract_odds_from_buttons(btns) -> Optional[Dict]:
    """
    Extract 1X2 odds from bet buttons.
    Buttons have text like "1\n1.81", "X\n3.72", "2\n4.36"
    Map based on the label (1/X/2) not market ID.
    """
    odds = {}

    for btn in btns:
        test_id = btn.get_attribute("data-test-id") or ""
        if not test_id.startswith("odd-"):
            continue

        btn_text = btn.inner_text().strip()
        lines = [line.strip() for line in btn_text.split("\n") if line.strip()]

        if not lines:
            continue

        label = lines[0]  # "1", "X", or "2"

        # Find numeric value in remaining lines
        value = None
        for line in lines[1:]:
            try:
                val = float(line)
                if 1.01 <= val <= 100.0:
                    value = val
                    break
            except ValueError:
                continue

        if value is not None:
            if label == "1":
                odds["home"] = value
            elif label == "X":
                odds["draw"] = value
            elif label == "2":
                odds["away"] = value

    return odds if len(odds) == 3 else None


def _extract_matches_from_page(page: Page) -> List[Dict]:
    """
    Extract all matches from the current BetPawa page.
    """
    matches = []

    try:
        event_divs = page.query_selector_all(
            "[class*='SportEvents_eventMatch']"
        )

        log.info(f"  Found {len(event_divs)} event divs")

        # DEBUG: Dump first event div HTML to understand structure
        if event_divs:
            first_div_html = event_divs[0].inner_html()
            log.debug(f"  First event div HTML (first 2000 chars): {first_div_html[:2000]}")

        for idx, evt_div in enumerate(event_divs):
            try:
                # Extract team names - try multiple selector patterns
                team_wraps = evt_div.query_selector_all(
                    "[class*='ScoreBoard_scoreboardPeriodParticipantNameWrapper']"
                )

                log.debug(f"    Event {idx}: found {len(team_wraps)} team wrappers (primary)")

                if len(team_wraps) < 2:
                    # Fallback: try alternative selectors
                    team_wraps = evt_div.query_selector_all(
                        "[class*='ParticipantName'], [class*='teamName'], [data-test-id*='team'], [class*='participant']"
                    )
                    log.debug(f"    Event {idx}: fallback found {len(team_wraps)} team wrappers")

                if len(team_wraps) < 2:
                    # Last resort: get all text and try to parse
                    all_text = evt_div.inner_text().strip()
                    log.debug(f"    Event {idx}: insufficient team wrappers. Full text: {all_text[:200]}")
                    continue

                home_name = team_wraps[0].inner_text().strip()
                away_name = team_wraps[1].inner_text().strip()

                log.debug(f"    Event {idx}: teams = '{home_name}' vs '{away_name}'")

                if not home_name or not away_name:
                    log.debug(f"    Event {idx}: empty team names, skipping")
                    continue

                # Extract league - try multiple selectors
                league_el = evt_div.query_selector(
                    "p[class*='SportEvents_subTitle']"
                )
                if not league_el:
                    league_el = evt_div.query_selector(
                        "[class*='subTitle'], [class*='league'], [data-test-id*='event-path'], [data-test-id*='league'], [class*='competition']"
                    )
                league = league_el.inner_text().strip() if league_el else "Unknown League"
                log.debug(f"    Event {idx}: league = '{league}'")

                if is_excluded(league):
                    log.info(f"    Skipping Israeli league: {league}")
                    continue

                # Extract odds from bet buttons - try multiple selectors
                bet_buttons = evt_div.query_selector_all(
                    "button[data-test-id^='odd-']"
                )
                log.debug(f"    Event {idx}: found {len(bet_buttons)} odds buttons (primary)")

                if not bet_buttons:
                    bet_buttons = evt_div.query_selector_all(
                        "button[data-test-id*='odd'], [class*='Betline'] button, [class*='odd'] button, button[class*='bet']"
                    )
                    log.debug(f"    Event {idx}: fallback found {len(bet_buttons)} odds buttons")

                odds = _extract_odds_from_buttons(bet_buttons)

                if not odds:
                    log.warning(f"    No valid odds for {home_name} vs {away_name} (found {len(bet_buttons)} buttons)")
                    # Debug: dump button info
                    for btn in bet_buttons:
                        tid = btn.get_attribute("data-test-id") or "no-test-id"
                        txt = btn.inner_text().strip()[:50]
                        log.debug(f"      Button: test-id={tid}, text='{txt}'")
                    # Also dump the betline container HTML
                    betline = evt_div.query_selector("[class*='Betline']")
                    if betline:
                        log.debug(f"      Betline HTML: {betline.inner_html()[:500]}")
                    continue

                match_id = re.sub(
                    r"[^a-z0-9]", "",
                    f"{home_name}_{away_name}".lower()
                )[:30]

                matches.append({
                    "id": match_id,
                    "league": league,
                    "home": home_name,
                    "away": away_name,
                    "odds": odds,
                    "home_stats": None,
                    "away_stats": None,
                    "h2h": None,
                    "source": "betpawa"
                })
                log.info(
                    f"    {home_name} vs {away_name}: "
                    f"H={odds['home']} D={odds['draw']} A={odds['away']}"
                )

            except Exception as e:
                log.warning(f"  Error parsing event div {idx}: {e}")
                import traceback
                log.debug(f"    Traceback: {traceback.format_exc()}")
                continue

    except Exception as e:
        log.warning(f"  Error extracting matches: {e}")
        import traceback
        log.debug(f"  Traceback: {traceback.format_exc()}")

    return matches


# ── Main Scraper ────────────────────────────────────────────────────────

def fetch_fixtures(date_str: Optional[str] = None) -> List[Dict]:
    """
    Fetch fixtures + odds from BetPawa using Playwright.
    Returns list of match dicts compatible with app.js scoring engine.
    """
    if not date_str:
        date_str = datetime.now(EAT).strftime("%Y-%m-%d")

    log.info(f"Fetching BetPawa fixtures for {date_str}...")

    all_matches = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        page.add_script_tag(
            content="Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        for i, url in enumerate(BETPAWA_URLS):
            if i >= MAX_ATTEMPTS:
                break

            log.info(f"  Trying URL ({i+1}/{MAX_ATTEMPTS}): {url}")
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)

                # Wait for match elements to appear
                try:
                    page.wait_for_selector("[class*='SportEvents_eventMatch']", timeout=15000)
                    log.info("    Match elements loaded")
                except Exception:
                    log.warning("    Match elements not found, waiting extra...")
                    time.sleep(RENDER_WAIT)

                time.sleep(3)  # Additional buffer for JS rendering

                matches = _extract_matches_from_page(page)
                all_matches.extend(matches)
                log.info(f"  Got {len(matches)} matches from this URL")

            except Exception as e:
                log.warning(f"  Failed: {e}")
                continue

        browser.close()

    # Deduplicate by match ID
    seen = set()
    unique = []
    for m in all_matches:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)

    log.info(f"Total BetPawa matches: {len(unique)}")
    return unique


if __name__ == "__main__":
    fixtures = fetch_fixtures()
    print(json.dumps(fixtures, indent=2))
    print(f"\n[OK] {len(fixtures)} fixtures fetched from BetPawa")