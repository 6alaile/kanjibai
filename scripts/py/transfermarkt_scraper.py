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
    return bool(cache.get(type_ + "s", {}).get(key))


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
        page.goto(team_url, wait_until="networkidle", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(3)
    except Exception as e:
        log.warning(f"Failed to load team page {team_url}: {e}")
        return None

    data = {"url": team_url, "name": ""}

    # Team name - from header
    try:
        name_el = page.query_selector("h1.data-header__headline-wrapper, h1[itemprop='name'], .data-header__club, .data-header__headline")
        if name_el:
            data["name"] = _safe_text(name_el)
    except:
        pass

    # Market value - from data-header
    try:
        mv_el = page.query_selector(".data-header__market-value-wrapper, .data-header__market-value, [itemprop='marketValue'], .data-header__value")
        if mv_el:
            data["market_value"] = _safe_text(mv_el)
    except:
        pass

    # Squad info - from the info table
    try:
        rows = page.query_selector_all(".data-header__info-box table tr, .info-table tr, .data-header__details tr, .data-header__info tr")
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


def _extract_team_fixtures(page: Page, team_url: str) -> Dict:
    """Extract team fixtures from spielplan page - follows TypeScript patterns."""
    fixtures_url = team_url.replace("/profil/", "/spielplan/") + "?saison_id=2025"
    fixtures = []

    try:
        page.goto(fixtures_url, wait_until="networkidle", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(3)
    except Exception as e:
        log.warning(f"Failed to load fixtures page {fixtures_url}: {e}")
        return {"fixtures": [], "recent_form": {"form": [], "goals_scored": [], "goals_conceded": []}}

    try:
        html = page.content()
        
        # Find ALL tables and look for the one with "Matchday" or "Spieltag" header
        table_matches = re.findall(r'<table[^>]*>[\s\S]*?<\/table>', html)
        if not table_matches:
            log.warning(f"No tables found on fixtures page")
            return {"fixtures": [], "recent_form": {"form": [], "goals_scored": [], "goals_conceded": []}}

        fixtures_table = None
        for table in table_matches:
            if "Matchday" in table or "Spieltag" in table or "matchday" in table.lower():
                fixtures_table = table
                break
        
        if not fixtures_table:
            log.warning(f"No table with Matchday/Spieltag found")
            return {"fixtures": [], "recent_form": {"form": [], "goals_scored": [], "goals_conceded": []}}

        # Parse rows - look for <tr> with enough <td> cells (9+ columns)
        row_matches = re.findall(r'<tr[^>]*>[\s\S]*?<\/tr>', fixtures_table)
        for row in row_matches:
            cells = re.findall(r'<td[^>]*>[\s\S]*?<\/td>', row)
            if len(cells) < 9:  # need at least matchday, date, time, H/A, opponent, formation, attendance, score
                continue

            clean_cells = [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', ' ').strip() for c in cells]
            if len(clean_cells) < 9:
                continue
                
            # Column mapping (0-indexed):
            # 0: matchday, 1: date, 2: time, 3: home/away, 4: position, 5: empty, 6: opponent, 7: formation, 8: attendance, 9: score
            matchday = clean_cells[0]
            date = clean_cells[1]
            time_ = clean_cells[2]
            home_away = clean_cells[3]
            opponent = clean_cells[6] if len(clean_cells) > 6 else ""
            formation = clean_cells[7] if len(clean_cells) > 7 else ""
            attendance = clean_cells[8] if len(clean_cells) > 8 else ""
            score = clean_cells[9] if len(clean_cells) > 9 else ""

            if not matchday or not date or matchday in ("Matchday", "Spieltag"):
                continue

            # Extract opponent URL if present
            opp_link_match = re.search(r'href="([^"]+)"', opponent)
            opponent_url = urljoin(BASE_URL, opp_link_match.group(1)) if opp_link_match else None

            # Extract match report URL if present
            report_match = re.search(r'href="([^"]*spielbericht[^"]*)"', score)
            match_report_url = urljoin(BASE_URL, report_match.group(1)) if report_match else None

            # Clean opponent name (remove HTML)
            opponent_clean = re.sub(r'<[^>]+>', '', opponent).strip()

            fixtures.append({
                "matchday": matchday.replace("(", "").replace(")", "").strip(),
                "date": date.strip(),
                "time": time_.strip(),
                "homeAway": "H" if home_away.strip() == "H" else "A",
                "opponent": opponent_clean,
                "opponentUrl": opponent_url,
                "formation": formation.strip() or None,
                "attendance": attendance.strip() or None,
                "score": re.sub(r'<[^>]+>', '', score).strip() or None,
                "matchReportUrl": match_report_url
            })
    except Exception as e:
        log.warning(f"Error parsing fixtures: {e}")

    # Compute recent form from last 5 finished matches
    recent_form = _compute_recent_form(fixtures)
    
    return {
        "fixtures": fixtures,
        "recent_form": recent_form
    }


def _compute_recent_form(fixtures: List[Dict]) -> Dict[str, List]:
    """Compute form, goals_scored, goals_conceded from last 5 finished matches."""
    # Filter finished matches with scores
    finished = [f for f in fixtures if f.get("score") and ("-" in f.get("score", "") or ":" in f.get("score", ""))]
    # Sort by date descending (most recent first)
    finished.sort(key=lambda x: x.get("date", ""), reverse=True)
    finished = finished[:5]
    
    form = []
    goals_scored = []
    goals_conceded = []
    
    for fixture in finished:
        score = fixture.get("score", "")
        home_away = fixture.get("homeAway", "H")
        opponent = fixture.get("opponent", "")
        
        try:
            # Score format: "2-1" or "1:2"
            score_clean = score.replace(":", "-")
            home_goals, away_goals = map(int, score_clean.split("-"))
            
            if home_away == "H":
                scored, conceded = home_goals, away_goals
            else:
                scored, conceded = away_goals, home_goals
            
            goals_scored.append(scored)
            goals_conceded.append(conceded)
            
            if scored > conceded:
                form.append("W")
            elif scored < conceded:
                form.append("L")
            else:
                form.append("D")
        except (ValueError, AttributeError):
            continue
    
    # Reverse to chronological order (oldest first)
    form.reverse()
    goals_scored.reverse()
    goals_conceded.reverse()
    
    return {
        "form": form,
        "goals_scored": goals_scored,
        "goals_conceded": goals_conceded
    }


def _extract_h2h_from_fixtures(page: Page, team_url: str, fixtures: List[Dict]) -> Dict[str, List]:
    """Extract H2H data from fixtures - group by opponent."""
    h2h = {}
    try:
        # Group fixtures by opponent
        opponent_matches = {}
        for fixture in fixtures:
            if not fixture.get("score") or ("-" not in fixture.get("score", "") and ":" not in fixture.get("score", "")):
                continue
            opponent = fixture.get("opponent", "")
            if not opponent:
                continue
            opp_key = _cache_key(opponent)
            if opp_key not in opponent_matches:
                opponent_matches[opp_key] = []
            opponent_matches[opp_key].append(fixture)
        
        # For each opponent, get last 5 H2H matches
        for opp_key, matches in opponent_matches.items():
            matches.sort(key=lambda x: x.get("date", ""), reverse=True)
            h2h_matches = matches[:5]
            h2h[opp_key] = []
            for m in h2h_matches:
                score = m.get("score", "")
                home_away = m.get("homeAway", "H")
                try:
                    score_clean = score.replace(":", "-")
                    home_goals, away_goals = map(int, score_clean.split("-"))
                    if home_away == "H":
                        h2h[opp_key].append({
                            "home": "team",
                            "away": "opponent",
                            "score": [home_goals, away_goals],
                            "date": m.get("date", "")
                        })
                    else:
                        h2h[opp_key].append({
                            "home": "opponent",
                            "away": "team",
                            "score": [home_goals, away_goals],
                            "date": m.get("date", "")
                        })
                except:
                    continue
    except Exception as e:
        log.warning(f"Error extracting H2H: {e}")
    return h2h


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
    # Extract team slug from URL: https://www.transfermarkt.com/manchester-city/profil/verein/281
    # Team slug is the part before /profil/verein/
    if team_name:
        key = _cache_key(team_name)
    else:
        # Extract slug from URL: find the part before /profil/verein/
        parts = team_url.split("/profil/verein/")
        if len(parts) > 1:
            slug = parts[0].split("/")[-1]
        else:
            slug = team_url.split("/")[-2]  # fallback
        key = _cache_key(slug)

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
        cache = _load_cache()
        
        # Profile
        log.info(f"  Fetching profile: {team_name}")
        profile = _extract_team_profile(page, team_url)
        if profile:
            profile["name"] = team_name
            set_cached(cache, "team", key, profile)
            log.info(f"  ✓ Profile cached")

        # Extract league position
        time.sleep(REQUEST_DELAY)
        league_pos = _extract_league_position(page, team_url, team_name)
        if league_pos:
            log.info(f"  ✓ League position: {league_pos}")
            # Update team cache with league position
            team_data = cache.get("teams", {}).get(key, {}).get("data", {})
            if team_data:
                team_data["league_position"] = league_pos
                set_cached(cache, "team", key, team_data)
                log.info(f"  ✓ League position saved: {league_pos}")

        time.sleep(REQUEST_DELAY)

        # Fixtures
        log.info(f"  Fetching fixtures: {team_name}")
        fixtures_data = _extract_team_fixtures(page, team_url)
        fixtures = fixtures_data.get("fixtures", []) if fixtures_data else []
        if fixtures:
            recent_form = fixtures_data.get("recent_form", {})
            fixture_data = {
                "fixtures": fixtures, 
                "url": team_url.replace("/profil/", "/spielplan/"),
                "recent_form": recent_form
            }
            set_cached(cache, "fixture", key, fixture_data)
            log.info(f"  ✓ {len(fixtures)} fixtures cached")
            if recent_form.get("form"):
                log.info(f"  ✓ Recent form: {recent_form['form']}, GS: {recent_form['goals_scored']}, GC: {recent_form['goals_conceded']}")

        # Also extract H2H from fixtures
        h2h_data = _extract_h2h_from_fixtures(page, team_url, fixtures)
        if h2h_data:
            set_cached(cache, "h2h", key, h2h_data)
            log.info(f"  ✓ H2H data cached for {len(h2h_data)} opponents")

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
        page.goto(league_url, wait_until="networkidle", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(5)

        # DEBUG: dump page HTML to understand structure
        html = page.content()
        log.info(f"  Page HTML length: {len(html)}")
        if "challenge" in html.lower() or "cloudflare" in html.lower() or "checking your browser" in html.lower():
            log.warning(f"  Cloudflare challenge detected for {league_name}")
            # Wait longer for challenge to resolve
            time.sleep(20)
            html = page.content()
            log.info(f"  After wait HTML length: {len(html)}")

        # Save HTML for debugging
        debug_file = f"/tmp/transfermarkt_league_{_cache_key(league_name)}.html"
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(html)
        log.info(f"  Saved HTML to {debug_file}")

        # Try multiple selector strategies - Transfermarkt league tables
        # Team links in league tables use /startseite/verein/ format
        selectors = [
            'table.items tbody tr td.hauptlink a[href*="/startseite/verein/"]',  # Main team column
            'table.items tbody tr td:first-child a[href*="/startseite/verein/"]',
            'table.items tbody tr td.zentriert a[href*="/startseite/verein/"]',
            '.items tbody tr td.hauptlink a[href*="/startseite/verein/"]',
            'table.items tbody tr a.vereinprofil_tooltip',
            'a[href*="/startseite/verein/"]',
        ]

        for selector in selectors:
            team_links = page.query_selector_all(selector)
            if team_links:
                log.info(f"  Selector '{selector}' found {len(team_links)} links")
                for link in team_links:
                    href = link.get_attribute("href")
                    log.debug(f"    Found href: {href}")
                    if href and "/startseite/verein/" in href:
                        # Convert /startseite/verein/ to /profil/verein/ for profile page
                        profil_href = href.replace("/startseite/verein/", "/profil/verein/")
                        # Remove saison_id if present
                        if "/saison_id/" in profil_href:
                            profil_href = profil_href.split("/saison_id/")[0]
                        full_url = urljoin(BASE_URL, profil_href)
                        if full_url not in team_urls:
                            team_urls.append(full_url)
                if team_urls:
                    log.info(f"  ✓ Using selector: {selector}")
                    break

        if not team_urls:
            # Fallback: search HTML directly for startseite/verein/ links
            matches = re.findall(r'href="(/[^"]*/startseite/verein/\d+[^"]*)"', html)
            for href in matches:
                profil_href = href.replace("/startseite/verein/", "/profil/verein/")
                if "/saison_id/" in profil_href:
                    profil_href = profil_href.split("/saison_id/")[0]
                full_url = urljoin(BASE_URL, profil_href)
                if full_url not in team_urls:
                    team_urls.append(full_url)
            log.info(f"  Regex fallback found {len(team_urls)} teams")

        # Also try to get league position from the table page
        league_position = _extract_league_position_from_table(page, league_name, html)

        set_cached(cache, "league", key, {
            "name": league_name, 
            "url": league_url, 
            "teams": team_urls,
            "league_position_data": league_position
        })
        log.info(f"  ✓ Found {len(team_urls)} teams")
    except Exception as e:
        log.warning(f"Error scraping league {league_name}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        page.close()

    return team_urls


def _extract_league_position_from_table(page: Page, league_name: str, html: str) -> Dict[str, int]:
    """Extract team positions from league table page."""
    positions = {}
    try:
        # Try to find the league table
        tables = page.query_selector_all("table.items")
        for table in tables:
            header = table.query_selector("thead")
            if header:
                header_text = _safe_text(header).lower()
                if "platz" in header_text or "position" in header_text or "team" in header_text or "verein" in header_text:
                    rows = table.query_selector_all("tbody tr")
                    for row in rows:
                        cells = row.query_selector_all("td")
                        if len(cells) >= 2:
                            # First cell usually position, second cell team name
                            pos_text = _safe_text(cells[0])
                            team_cell = cells[1]
                            team_link = team_cell.query_selector("a[href*='/profil/verein/']")
                            if team_link and pos_text.isdigit():
                                team_name = _safe_text(team_cell)
                                pos = int(pos_text)
                                positions[team_name.lower()] = pos
                    if positions:
                        break
    except Exception as e:
        log.warning(f"Error extracting league positions: {e}")
    return positions


def _extract_league_position(page: Page, team_url: str, team_name: str) -> Optional[int]:
    """Extract league position from league table page."""
    try:
        # Navigate to league table - construct URL from team URL
        # team_url format: https://www.transfermarkt.com/team-name/profil/verein/123
        # League table: https://www.transfermarkt.com/league-name/tabelle/wettbewerb/GB1
        parts = team_url.split("/")
        verein_id = parts[-1] if parts[-1].isdigit() else None
        if not verein_id:
            return None
        
        # Try to get league ID from the team page first
        page.goto(team_url, wait_until="networkidle", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)
        
        # Look for league link in breadcrumbs or header - prioritize domestic league over cups/CL
        # Domestic leagues typically have IDs like GB1, ES1, L1, IT1, FR1, NL1, PO1, etc.
        # Champions League is CL, Europa League is EL, etc.
        league_links = page.query_selector_all("a[href*='/tabelle/wettbewerb/'], a[href*='/startseite/wettbewerb/']")
        league_id = None
        
        # First, try to find a domestic league link (not CL, EL, etc.)
        domestic_league_ids = {"GB1", "ES1", "L1", "IT1", "FR1", "NL1", "PO1", "GB2", "ES2", "L2", "IT2", "FR2", "NL2", "BE1", "PT1", "TR1", "AT1", "CH1", "DK1", "SC1", "RU1", "UA1", "PL1", "CZ1", "RO1", "HU1", "HR1", "SI1", "SK1", "CY1", "MT1", "IS1", "FI1", "NO1", "SE1", "EE1", "LV1", "LT1", "GE1", "AM1", "AZ1", "KZ1", "BY1", "MD1", "ME1", "RS1", "BA1", "MK1", "AL1", "KS1", "LI1", "AD1", "SM1", "VA1", "MC1", "IL1", "IR1", "SA1", "QA1", "AE1", "JO1", "LB1", "SY1", "IQ1", "YE1", "OM1", "BH1", "KW1", "PK1", "IN1", "CN1", "JP1", "KR1", "AU1", "NZ1", "ZA1", "EG1", "MA1", "TN1", "DZ1", "LY1", "NG1", "GH1", "CI1", "CM1", "SN1", "ML1", "BF1", "NE1", "TD1", "CF1", "CG1", "CD1", "GA1", "GQ1", "GW1", "LR1", "SL1", "GM1", "MR1", "MU1", "SC1", "ST1", "KM1", "DJ1", "SO1", "ET1", "ER1", "SS1", "UG1", "KE1", "TZ1", "RW1", "BI1", "MG1", "MZ1", "ZW1", "BW1", "NA1", "SZ1", "LS1", "MW1", "ZM1", "AO1", "TZ1", "KM1", "YT1", "RE1", "PM1", "MF1", "WF1", "PF1", "AS1", "GU1", "MP1", "VI1", "PR1", "UM1"}
        
        for link in league_links:
            href = link.get_attribute("href")
            if href and "/wettbewerb/" in href:
                candidate_id = href.split("/wettbewerb/")[-1].split("/")[0]
                # Prefer domestic league IDs over cup competitions (CL, EL, etc.)
                if candidate_id in domestic_league_ids:
                    league_id = candidate_id
                    break
                elif league_id is None and candidate_id not in {"CL", "EL", "EC", "WC", "UC"}:
                    # Fallback to first non-cup competition
                    league_id = candidate_id
        
        if not league_id:
            # Try to find from current page URL
            if "/wettbewerb/" in team_url:
                league_id = team_url.split("/wettbewerb/")[-1].split("/")[0]
        
        if not league_id:
            return None
        
        # Navigate to league table
        table_url = f"https://www.transfermarkt.com/ligatabelle/wettbewerb/{league_id}"
        log.info(f"  Fetching league table: {table_url}")
        page.goto(table_url, wait_until="networkidle", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)
        
        # Parse table for team position
        tables = page.query_selector_all("table.items")
        for table in tables:
            header = table.query_selector("thead")
            if header:
                header_text = _safe_text(header).lower()
                if "platz" in header_text or "position" in header_text or "team" in header_text or "verein" in header_text:
                    rows = table.query_selector_all("tbody tr")
                    for row in rows:
                        cells = row.query_selector_all("td")
                        if len(cells) >= 2:
                            pos_text = _safe_text(cells[0])
                            team_cell = cells[1] if len(cells) > 1 else cells[0]
                            team_link = team_cell.query_selector("a[href*='/profil/verein/']")
                            if team_link:
                                href = team_link.get_attribute("href")
                                if href and verein_id in href:
                                    if pos_text.isdigit():
                                        return int(pos_text)
                            # Also try matching by name
                            team_text = _safe_text(team_cell)
                            if team_name.lower() in team_text.lower() and pos_text.isdigit():
                                return int(pos_text)
    except Exception as e:
        log.warning(f"Error extracting league position: {e}")
    return None


def run_daily_scrape(max_teams: int = 20, betpawa_fixtures: Optional[List[Dict]] = None) -> None:
    """Main entry point for daily scraping."""
    log.info("=" * 50)
    log.info(f"Transfermarkt Daily Scrape - {datetime.now(timezone.utc).isoformat()}")
    log.info("=" * 50)

    # Seed queue with teams from BetPawa fixtures if provided
    queue = _load_queue()
    
    # Add teams from BetPawa fixtures to queue
    if betpawa_fixtures:
        log.info(f"  Seeding queue with {len(betpawa_fixtures)} BetPawa fixtures")
        for fixture in betpawa_fixtures:
            for team_key in ["home", "away"]:
                team_name = fixture.get(team_key)
                if not team_name:
                    continue
                # We need to find the Transfermarkt URL for this team
                # For now, add to queue with name only - will be resolved when processing
                tid = f"team:{_cache_key(team_name)}"
                if not any(q["id"] == tid for q in queue):
                    queue.append({
                        "id": tid, "type": "team", "name": team_name, "url": "",
                        "priority": 30, "addedAt": datetime.now(timezone.utc).isoformat(), "attempts": 0
                    })
    
    # Seed leagues if queue is empty (fallback)
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
                    # URL format: https://www.transfermarkt.com/manchester-city/profil/verein/281
                    # Team slug is at index -4 (manchester-city)
                    parts = tu.split('/')
                    team_slug = parts[-4] if len(parts) >= 4 else parts[-2]
                    tid = f"team:{team_slug}"
                    if not any(q["id"] == tid for q in queue):
                        queue.append({"id": tid, "type": "team", "name": "", "url": tu,
                                      "priority": 50, "addedAt": datetime.now(timezone.utc).isoformat(), "attempts": 0})
                item["attempts"] += 1
                item["lastAttempt"] = datetime.now(timezone.utc).isoformat()
            _save_queue(queue)

            # Process team items (prioritize BetPawa teams first)
            team_items = [q for q in queue if q["type"] == "team" and q["attempts"] < 3]
            # Sort by priority (lower = higher priority)
            team_items.sort(key=lambda x: x.get("priority", 100))
            team_items = team_items[:max_teams]
            
            for item in team_items:
                # If URL is empty, we need to search for the team on Transfermarkt
                team_url = item["url"]
                team_name = item["name"]
                
                # Derive team_name from URL if name is empty but URL exists
                if not team_name and team_url:
                    # URL format: https://www.transfermarkt.com/manchester-city/profil/verein/281
                    # Team slug is at index -4 (manchester-city)
                    parts = team_url.split("/")
                    team_name = parts[-4].replace("-", " ").title() if len(parts) >= 4 else parts[-2].replace("-", " ").title()
                    item["name"] = team_name
                
                if not team_url and team_name:
                    # Search for team on Transfermarkt
                    team_url = _search_team_url(browser, team_name)
                    if team_url:
                        item["url"] = team_url
                        # Update queue with the found URL
                        for q in queue:
                            if q["id"] == item["id"]:
                                q["url"] = team_url
                                break
                        _save_queue(queue)
                
                if team_url:
                    scrape_team(team_url, team_name, browser)
                else:
                    log.warning(f"  Could not find Transfermarkt URL for {team_name}")
                
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


def _search_team_url(browser, team_name: str) -> Optional[str]:
    """Search for a team on Transfermarkt and return its profile URL."""
    search_url = f"https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={team_name.replace(' ', '+')}"
    log.info(f"  Searching for team: {team_name}")
    
    page = browser.new_page()
    try:
        page.goto(search_url, wait_until="networkidle", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)
        
        # Find first team result link
        team_links = page.query_selector_all('a[href*="/profil/verein/"]')
        for link in team_links:
            href = link.get_attribute("href")
            if href and "/profil/verein/" in href:
                full_url = urljoin(BASE_URL, href)
                log.info(f"  Found team URL: {full_url}")
                return full_url
    except Exception as e:
        log.warning(f"Error searching for team {team_name}: {e}")
    finally:
        page.close()
    return None


if __name__ == "__main__":
    run_daily_scrape()