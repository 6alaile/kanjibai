"""
transfermarkt_scraper.py — Transfermarkt Scraper using Playwright
Renders JavaScript-heavy pages, handles Cloudflare challenges.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, sync_playwright

logging.basicConfig(level=logging.INFO, format="[transfermarkt] %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "transfermarkt_cache.json"
QUEUE_FILE = Path(__file__).parent.parent / "ts" / "transfermarkt_queue.json"

RENDER_WAIT = 5
MAX_RETRIES = 2
REQUEST_DELAY = 3

BASE_URL = "https://www.transfermarkt.com"


@dataclass
class TeamData:
    name: str
    url: str
    market_value: Optional[str] = None
    squad_size: Optional[int] = None
    avg_age: Optional[float] = None
    foreigners: Optional[int] = None
    national_players: Optional[int] = None
    stadium: Optional[str] = None
    fixtures: List[Dict[str, Any]] = None


def _load_cache() -> Dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {"version": 1, "lastUpdated": datetime.now(timezone.utc).isoformat(), "teams": {}, "players": {}, "fixtures": {}, "leagues": {}}


def _save_cache(cache: Dict) -> None:
    cache["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _load_queue() -> List[Dict]:
    if QUEUE_FILE.exists():
        with open(QUEUE_FILE, "r") as f:
            data = json.load(f)
            return data.get("queue", [])
    return []


def _save_queue(queue: List[Dict]) -> None:
    with open(QUEUE_FILE, "w") as f:
        json.dump({"queue": queue, "processedToday": 0, "lastReset": datetime.now().strftime("%Y-%m-%d")}, f, indent=2)


def _cache_key(name: str) -> str:
    return name.lower().replace(" ", "_").replace(".", "").replace("'", "").replace("&", "").replace("-", "_")


def is_cached(cache: Dict, type_: str, key: str) -> bool:
    return !!cache.get(type_ + "s", {}).get(key)


def set_cached(cache: Dict, type_: str, key: str, data: Any) -> None:
    if type_ + "s" not in cache:
        cache[type_ + "s"] = {}
    cache[type_ + "s"][key] = {"data": data, "fetchedAt": datetime.now(timezone.utc).isoformat()}
    _save_cache(cache)


def get_cached(cache: Dict, type_: str, key: str) -> Optional[Any]:
    return cache.get(type_ + "s", {}).get(key, {}).get("data")


def _safe_text(el) -> str:
    try:
        return el.inner_text().strip() if el else ""
    except:
        return ""


def _extract_team_profile(page: Page, team_url: str) -> Optional[Dict]:
    """Extract team profile from Transfermarkt team page."""
    try:
        page.goto(team_url, wait_until="networkidle", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)
    except Exception as e:
        log.warning(f"Failed to load team page {team_url}: {e}")
        return None

    data = {"url": team_url, "name": ""}

    # Team name - from header
    try:
        name_el = page.query_selector("h1.data-header__headline-wrapper, h1[itemprop='name'], .data-header__club")
        if name_el:
            data["name"] = _safe_text(name_el)
    except:
        pass

    # Market value - from data-header
    try:
        mv_el = page.query_selector(".data-header__market-value-wrapper, .data-header__market-value, [itemprop='marketValue']")
        if mv_el:
            data["market_value"] = _safe_text(mv_el)
    except:
        pass

    # Squad info - from the info table
    try:
        rows = page.query_selector_all(".data-header__info-box table tr, .info-table tr, .data-header__details tr")
        for row in rows:
            th = row.query_selector("th, td:first-child")
            td = row.query_selector("td:last-child")
            if not th or not td:
                continue
            label = _safe_text(th).lower()
            value = _safe_text(td)
            if "squad" in label and "size" in label:
                data["squad_size"] = int(re.search(r"\d+", value).group()) if re.search(r"\d+", value) else None
            elif "average age" in label or "avg. age" in label:
                data["avg_age"] = float(re.search(r"[\d.]+", value).group()) if re.search(r"[\d.]+", value) else None
            elif "foreigners" in label:
                data["foreigners"] = int(re.search(r"\d+", value).group()) if re.search(r"\d+", value) else None
            elif "national" in label:
                data["national_players"] = int(re.search(r"\d+", value).group()) if re.search(r"\d+", value) else None
            elif "stadium" in label:
                data["stadium"] = value
    except:
        pass

    return data if data.get("name") else None


def _extract_team_fixtures(page: Page, team_url: str) -> List[Dict]:
    """Extract team fixtures from spielplan page."""
    fixtures_url = team_url.replace("/profil/", "/spielplan/")
    fixtures = []

    try:
        page.goto(fixtures_url, wait_until="networkidle", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)
    except Exception as e:
        log.warning(f"Failed to load fixtures page {fixtures_url}: {e}")
        return []

    try:
        # Find fixtures table
        tables = page.query_selector_all("table.items")
        fixtures_table = None
        for table in tables:
            header = table.query_selector("thead")
            if header and ("matchday" in _safe_text(header).lower() or "spieltag" in _safe_text(header).lower()):
                fixtures_table = table
                break
        if not fixtures_table and tables:
            fixtures_table = tables[0]  # fallback

        if not fixtures_table:
            return []

        rows = fixtures_table.query_selector_all("tbody tr")
        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) < 8:
                continue

            matchday = _safe_text(cells[0])
            date = _safe_text(cells[1])
            time_ = _safe_text(cells[2])
            home_away = _safe_text(cells[3])
            opponent_cell = cells[4]
            opponent_link = opponent_cell.query_selector("a")
            opponent = _safe_text(opponent_cell)
            opponent_url = urljoin(BASE_URL, opponent_link.get_attribute("href")) if opponent_link else None
            formation = _safe_text(cells[5]) if len(cells) > 5 else ""
            attendance = _safe_text(cells[6]) if len(cells) > 6 else ""
            score_cell = cells[7] if len(cells) > 7 else None
            score = _safe_text(score_cell) if score_cell else ""
            score_link = score_cell.query_selector("a") if score_cell else None
            match_report_url = urljoin(BASE_URL, score_link.get_attribute("href")) if score_link else None

            if not matchday or matchday.lower() in ("matchday", "spieltag"):
                continue

            fixtures.append({
                "matchday": matchday.replace("(", "").replace(")", "").strip(),
                "date": date,
                "time": time_,
                "homeAway": "H" if home_away.strip() == "H" else "A",
                "opponent": opponent,
                "opponentUrl": opponent_url,
                "formation": formation or None,
                "attendance": attendance or None,
                "score": score or None,
                "matchReportUrl": match_report_url
            })
    except Exception as e:
        log.warning(f"Error parsing fixtures: {e}")

    return fixtures


def _extract_match_events(page: Page, match_url: str) -> List[Dict]:
    """Extract match events (goals, cards, subs) from match report."""
    events = []
    try:
        page.goto(match_url, wait_until="networkidle", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)
    except Exception as e:
        log.warning(f"Failed to load match report {match_url}: {e}")
        return []

    try:
        rows = page.query_selector_all('tr[class*="sb-aktion-heim"], tr[class*="sb-aktion-gast"]')
        for row in rows:
            is_home = "heim" in (row.get_attribute("class") or "")
            minute_el = row.query_selector('td[class*="sb-aktion-uhr"]')
            player_el = row.query_selector('td[class*="sb-aktion-spieler"]')
            action_el = row.query_selector('td[class*="sb-aktion-aktion"]')
            score_el = row.query_selector('td[class*="sb-aktion-spielstand"]')

            minute = _safe_text(minute_el).replace("'", "")
            player_html = player_el.inner_html() if player_el else ""
            player_link = player_el.query_selector("a") if player_el else None
            player_url = urljoin(BASE_URL, player_link.get_attribute("href")) if player_link else None
            player = re.sub(r"<[^>]+>", "", player_html).strip()
            action = _safe_text(action_el)
            score = _safe_text(score_el)

            if not minute and not player and not action:
                continue

            action_lower = action.lower()
            if "yellow" in action_lower:
                etype = "yellow_card"
            elif "red" in action_lower:
                etype = "red_card"
            elif "substitut" in action_lower or "wechsl" in action_lower:
                etype = "substitution"
            elif "penalty" in action_lower or "elfmeter" in action_lower:
                etype = "penalty"
            elif "own goal" in action_lower or "eigentor" in action_lower:
                etype = "own_goal"
            else:
                etype = "goal"

            events.append({
                "minute": minute,
                "team": "home" if is_home else "away",
                "player": player,
                "playerUrl": player_url,
                "type": etype,
                "detail": action if action != etype else None
            })
    except Exception as e:
        log.warning(f"Error parsing match events: {e}")

    return events


def _extract_match_lineups(page: Page, match_url: str) -> List[Dict]:
    """Extract match lineups from match report."""
    lineups = []
    try:
        page.goto(match_url, wait_until="networkidle", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)
    except Exception as e:
        log.warning(f"Failed to load match report for lineups {match_url}: {e}")
        return []

    try:
        # Bench tables
        bench_tables = page.query_selector_all('table.ersatzbank')
        formations = page.query_selector_all('td[class*="formation"]')
        home_formation = _safe_text(formations[0]) if formations else ""
        away_formation = _safe_text(formations[1]) if len(formations) > 1 else ""

        def parse_bench(table, team_side):
            players = []
            rows = table.query_selector_all("tr")
            for row in rows:
                cells = row.query_selector_all("td")
                if len(cells) < 3:
                    continue
                num = _safe_text(cells[0])
                name_cell = cells[1]
                name_link = name_cell.query_selector("a")
                name_url = urljoin(BASE_URL, name_link.get_attribute("href")) if name_link else None
                name = _safe_text(name_cell)
                pos = _safe_text(cells[2])
                if name and num:
                    players.append({"number": num, "name": name, "position": pos, "url": name_url})
            return {"team": team_side, "formation": home_formation if team_side == "home" else away_formation,
                    "startingXI": [], "bench": players}

        if bench_tables:
            if len(bench_tables) > 0:
                lineups.append(parse_bench(bench_tables[0], "home"))
            if len(bench_tables) > 1:
                lineups.append(parse_bench(bench_tables[1], "away"))
    except Exception as e:
        log.warning(f"Error parsing lineups: {e}")

    return lineups


def scrape_team(team_url: str, team_name: str = None, browser=None) -> Optional[Dict]:
    """Scrape a single team's profile and fixtures."""
    cache = _load_cache()
    key = _cache_key(team_name or team_url.split("/")[-2])

    if is_cached(cache, "team", key):
        log.info(f"  Already cached: {team_name or team_url}")
        return get_cached(cache, "team", key)

    if browser is None:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            try:
                return _scrape_team_with_browser(browser, team_url, team_name, key)
            finally:
                browser.close()
    else:
        return _scrape_team_with_browser(browser, team_url, team_name, key)


def _scrape_team_with_browser(browser, team_url: str, team_name: str, key: str) -> Optional[Dict]:
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    try:
        # Profile
        log.info(f"  Fetching profile: {team_name}")
        profile = _extract_team_profile(page, team_url)
        if profile:
            profile["name"] = team_name
            set_cached(cache, "team", key, profile)
            log.info(f"  ✓ Profile cached")

        time.sleep(REQUEST_DELAY)

        # Fixtures
        log.info(f"  Fetching fixtures: {team_name}")
        fixtures = _extract_team_fixtures(page, team_url)
        if fixtures:
            set_cached(cache, "fixture", key, {"fixtures": fixtures, "url": team_url.replace("/profil/", "/spielplan/")})
            log.info(f"  ✓ {len(fixtures)} fixtures cached")

        return get_cached(cache, "team", key)
    finally:
        context.close()


def scrape_league(league_url: str, league_name: str, browser) -> List[str]:
    """Extract team URLs from a league page."""
    cache = _load_cache()
    key = _cache_key(league_name)

    if is_cached(cache, "league", key):
        log.info(f"  League already cached: {league_name}")
        return []

    log.info(f"  Fetching league teams: {league_name}")
    page = browser.new_page()
    team_urls = []

    try:
        page.goto(league_url, wait_until="networkidle", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)

        # Find team links in the league table
        team_links = page.query_selector_all('table.items a[href*="/profil/verein/"]')
        for link in team_links:
            href = link.get_attribute("href")
            if href and "/profil/verein/" in href:
                full_url = urljoin(BASE_URL, href)
                if full_url not in team_urls:
                    team_urls.append(full_url)

        set_cached(cache, "league", key, {"name": league_name, "url": league_url, "teams": team_urls})
        log.info(f"  ✓ Found {len(team_urls)} teams")
    except Exception as e:
        log.warning(f"Error scraping league {league_name}: {e}")
    finally:
        page.close()

    return team_urls


def run_daily_scrape(max_teams: int = 20) -> None:
    """Main entry point for daily scraping."""
    log.info("=" * 50)
    log.info(f"Transfermarkt Daily Scrape - {datetime.now(timezone.utc).isoformat()}")
    log.info("=" * 50)

    # Seed leagues if queue is empty
    queue = _load_queue()
    if not queue:
        seed_leagues = [
            {"name": "Premier League", "url": "https://www.transfermarkt.com/premier-league/startseite/wettbewerb/GB1"},
            {"name": "La Liga", "url": "https://www.transfermarkt.com/laliga/startseite/wettbewerb/ES1"},
            {"name": "Bundesliga", "url": "https://www.transfermarkt.com/bundesliga/startseite/wettbewerb/L1"},
            {"name": "Serie A", "url": "https://www.transfermarkt.com/serie-a/startseite/wettbewerb/IT1"},
            {"name": "Ligue 1", "url": "https://www.transfermarkt.com/ligue-1/startseite/wettbewerb/FR1"},
            {"name": "Eredivisie", "url": "https://www.transfermarkt.com/eredivisie/startseite/wettbewerb/NL1"},
            {"name": "Primeira Liga", "url": "https://www.transfermarkt.com/primeira-liga/startseite/wettbewerb/PO1"},
            {"name": "Championship", "url": "https://www.transfermarkt.com/championship/startseite/wettbewerb/GB2"},
        ]
        for lg in seed_leagues:
            queue.append({"id": f"league:{lg['url'].split('/')[-1]}", "type": "league", "name": lg["name"],
                          "url": lg["url"], "priority": 10, "addedAt": datetime.now(timezone.utc).isoformat(),
                          "attempts": 0})
        _save_queue(queue)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])

        try:
            # Process league items first to populate team queue
            league_items = [q for q in queue if q["type"] == "league"][:5]
            for item in league_items:
                team_urls = scrape_league(item["url"], item["name"], browser)
                for tu in team_urls:
                    tid = f"team:{tu.split('/')[-2]}"
                    if not any(q["id"] == tid for q in queue):
                        queue.append({"id": tid, "type": "team", "name": "", "url": tu,
                                      "priority": 50, "addedAt": datetime.now(timezone.utc).isoformat(), "attempts": 0})
                item["attempts"] += 1
                item["lastAttempt"] = datetime.now(timezone.utc).isoformat()
            _save_queue(queue)

            # Process team items
            team_items = [q for q in queue if q["type"] == "team" and q["attempts"] < 3][:max_teams]
            for item in team_items:
                team_name = item["name"] or item["url"].split("/")[-2].replace("-", " ").title()
                scrape_team(item["url"], team_name, browser)
                item["attempts"] += 1
                item["lastAttempt"] = datetime.now(timezone.utc).isoformat()
                time.sleep(REQUEST_DELAY)

            # Mark completed
            for item in queue:
                if item["attempts"] >= 3:
                    item["priority"] = 1000
            _save_queue(queue)

        finally:
            browser.close()

    log.info("=" * 50)
    log.info("Daily scrape complete")
    log.info("=" * 50)


if __name__ == "__main__":
    run_daily_scrape()