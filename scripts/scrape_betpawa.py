"""
scrape_betpawa.py — Scout BetPawa Odds Scraper
Uses Playwright to render dynamic SPAs (Cloudflare-protected).

Data model matches app.js expectations:
{
    "id": fixture_id,
    "league": league_name,
    "home": home_team,
    "away": away_team,
    "odds": {"home": float, "draw": float, "away": float},
    "home_stats": None,
    "away_stats": None,
    "h2h": None,
    "source": "betpawa"
}
"""

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

from playwright.sync_api import Page, sync_playwright

logging.basicConfig(level=logging.INFO, format="[betpawa] %(message)s")
log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))

# ── Configuration ──────────────────────────────────────────────────────
BETPAWA_URLS = [
    "https://www.betpawa.co.tz/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC",
    "https://www.betpawa.ke/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC",
    "https://www.betpawa.ug/events?categoryId=2&marketId=1X2&sorting=competitionPriority_DESC",
]

RENDER_WAIT = 5
MAX_ATTEMPTS = 3

# ── Helpers ─────────────────────────────────────────────────────────────

def _safe_float(val, fallback: float = 0.0) -> float:
    """Convert to float, return fallback on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return fallback


def _extract_odds_from_text(page_text: str) -> Optional[Dict]:
    """
    Extract 1X2 odds from BetPawa page text.
    Page structure has pattern: "1\n4.02\nX\n3.83\n2\n1.95"
    Returns {home, draw, away} or None.
    """
    try:
        # Find all odds-like numbers (decimal format, typical odds range)
        # Match numbers like 1.50, 2.30, 3.20, etc. that appear near 1/X/2 labels
        odd_values = re.findall(r'\d+\.\d{2}', page_text)

        # Filter to valid odds range (1.01 to 100.0)
        valid_odds = []
        for v in odd_values:
            try:
                fv = float(v)
                if 1.01 <= fv <= 100.0:
                    valid_odds.append(fv)
            except ValueError:
                continue

        # We need at least 3 odds: home, draw, away
        if len(valid_odds) >= 3:
            # Take the first 3 valid odds we find
            # These typically appear in order: 1, X, 2 on BetPawa
            return {
                "home": valid_odds[0],
                "draw": valid_odds[1],
                "away": valid_odds[2]
            }

        # Alternative: look for the "1 X 2" pattern with surrounding numbers
        # Pattern: "1 [number] X [number] 2 [number]"
        pattern = re.findall(r'(?:1|X|2)\s+(\d+\.\d{2})', page_text)
        if len(pattern) >= 3:
            values = []
            for p in pattern[:6]:  # check up to 6 occurrences
                try:
                    fv = float(p)
                    if 1.01 <= fv <= 100.0:
                        values.append(fv)
                except ValueError:
                    continue
            if len(values) >= 3:
                return {"home": values[0], "draw": values[1], "away": values[2]}

        log.warning(f"Not enough valid odds found in text. Got {len(valid_odds)} valid odds.")
        return None

    except Exception as e:
        log.warning(f"Error extracting odds from text: {e}")
        return None


def _extract_odds_from_page(page: Page) -> Optional[Dict]:
    """
    Extract odds by getting page content and parsing text.
    Avoids ElementHandle.inner_text issues.
    """
    try:
        # Get full page text content
        page_text = page.inner_text("body")

        # Also get the raw HTML snippet for odds patterns
        html = page.content()

        # Try text-based extraction first
        odds = _extract_odds_from_text(page_text)

        if not odds:
            # Try extracting from HTML using regex for odd values
            # BetPawa embeds odds as text nodes near "1", "X", "2"
            html_odds = re.findall(r'<[^>]*?class="[^"]*odd[^"]*")[^>]*>', html)
            if html_odds:
                # Try to extract text from these elements
                for h in html_odds[:5]:
                    # Get text between tags
                    txt_match = re.search(r'>([^<]+)<', h)
                    if txt_match:
                        txt = txt_match.group(1).strip()
                        try:
                            v = float(txt)
                            if 1.01 <= v <= 100.0:
                                # This is a valid odd, collect all such values
                                pass
                        except ValueError:
                            pass

        if not odds:
            # Fallback: just use regex on full page text
            odds = _extract_odds_from_text(page_text)

        return odds

    except Exception as e:
        log.warning(f"Error extracting odds from page: {e}")
        return None


def _get_team_names_from_text(page_text: str) -> tuple:
    """
    Extract home/away team names from page text.
    Returns (home_team, away_team) or ("Home Team", "Away Team").
    """
    # Look for common team name patterns
    # BetPawa typically shows: "Home Team vs Away Team" or individual team names
    # Try to find words that look like team names (capitalized, 2-3 words)

    # Split text and look for capitalized sequences
    lines = page_text.split('\n')
    potential_teams = []

    for line in lines:
        line = line.strip()
        # Skip navigation, empty lines, short lines
        if not line or len(line) < 3 or len(line) > 50:
            continue
        # Skip common non-team words
        skip_words = {"LIVE", "UPCOMING", "TODAY", "MATCH", "FOOTBALL",
                      "BET", "ODDS", "1X2", "LIVE", "RESULT", "SCORE",
                      "LOGIN", "JOIN", "NOW", "Sign", "Up",
                      "BetPawa", "Tanzania", "Kenya", "Uganda"}
        if line.upper() in skip_words:
            continue

        # Check if line starts with a capital letter and contains letters/spaces
        if line[0].isupper() and any(c.isalpha() for c in line):
            # Don't add if it's just a number or odd
            try:
                float(line)
                continue  # skip if it's a number (odd)
            except ValueError:
                pass

            # Avoid very short likely-not-team strings
            if len(line) > 2:
                potential_teams.append(line)

    # Deduplicate while preserving order
    seen = set()
    unique_teams = []
    for t in potential_teams:
        if t not in seen:
            seen.add(t)
            unique_teams.append(t)

    if len(unique_teams) >= 2:
        return (unique_teams[0], unique_teams[1])

    return ("Home Team", "Away Team")


# ── Build Match ──────────────────────────────────────────────────────────

def _build_match(fixture_id: str, home: str, away: str, odds: Dict) -> Dict:
    """Build match dict in app.js format."""
    return {
        "id": fixture_id,
        "league": "BetPawa Premier League",
        "home": home,
        "away": away,
        "odds": odds,
        "home_stats": None,
        "away_stats": None,
        "h2h": None,
        "source": "betpawa"
    }


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
                "--disable-web-security",
                "--disable-dev-shm-usage",
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

        # Add stealth script to avoid bot detection
        page = context.new_page()
        page.add_script_tag(
            content="""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
            """
        )

        attempted_urls = 0
        for url in BETPAWA_URLS:
            if attempted_urls >= MAX_ATTEMPTS:
                log.warning("Max URL attempts reached, stopping.")
                break

            attempted_urls += 1
            log.info(f"Attempting BetPawa URL ({attempted_urls}/{MAX_ATTEMPTS}): {url}")

            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(RENDER_WAIT)

                # Get full page text
                page_text = page.inner_text("body")

                # Extract team names
                home, away = _get_team_names_from_text(page_text)
                log.info(f"  Teams: {home} vs {away}")

                # Extract odds from page text
                odds = _extract_odds_from_text(page_text)

                # Generate fixture ID from page title
                title = page.title()
                fixture_id = re.sub(r'[^a-z0-9]', '', title.lower())[:20] or "betpawa_01"

                if odds:
                    match = _build_match(fixture_id, home, away, odds)
                    all_matches.append(match)
                    log.info(f"  ✓ Odds extracted: home={odds['home']}, draw={odds['draw']}, away={odds['away']}")
                else:
                    log.warning("  ✗ Could not extract odds from page text")

                    # Try scrolling to load more events
                    for _ in range(3):
                        page.evaluate("window.scrollBy(0, window.innerHeight)")
                        time.sleep(1)

                    # Re-get page text after scroll
                    page_text = page.inner_text("body")
                    odds = _extract_odds_from_text(page_text)
                    if odds:
                        match = _build_match(fixture_id, home, away, odds)
                        all_matches.append(match)
                        log.info(f"  ✓ Odds extracted after scroll: {odds}")

            except Exception as e:
                log.warning(f"Failed to load {url}: {e}")
                continue

        browser.close()

    # Deduplicate matches by ID
    seen = set()
    unique_matches = []
    for m in all_matches:
        mid = m.get("id", "")
        if mid not in seen:
            seen.add(mid)
            unique_matches.append(m)

    log.info(f"  Total BetPawa matches: {len(unique_matches)}")
    return unique_matches


# ── CLI Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    fixtures = fetch_fixtures()
    output = json.dumps(fixtures, indent=2)
    print(output)
    print(f"\n[ OK ] {len(fixtures)} fixtures fetched from BetPawa")