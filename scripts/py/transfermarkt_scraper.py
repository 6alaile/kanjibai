"""
transfermarkt_scraper.py — Transfermarkt Scraper (v2)

Two data source URLs only:
  1. {team}/spielplandatum/verein/{id}?saison_id={year}  → fixtures (form)
  2. {league}/tabelle/wettbewerb/{league_id}              → league table
  3. spielbericht/index/spielbericht/{matchId}            → H2H (lazy)

Cache structure (v2, league-centric):
{
  "version": 2,
  "lastUpdated": "ISO8601",
  "leagues": {
    "GB1": {
      "name": "Premier League",
      "table_fetched_at": "ISO8601",
      "teams": {
        "manchester_united": {
          "id": 985, "name": "Manchester United",
          "league_position": 8, "total_teams": 20, "last_scraped": "...",
          "recent_matches": [...],
          "h2h": { "liverpool": [...] }
        }
      }
    }
  }
}
"""

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from urllib.parse import urljoin

from playwright.sync_api import Page, sync_playwright

logging.basicConfig(level=logging.INFO, format="[transfermarkt] %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent.parent / "data" / "transfermarkt_cache.json"
QUEUE_FILE = Path(__file__).parent.parent / "ts" / "transfermarkt_queue.json"

RENDER_WAIT = 5
REQUEST_DELAY = 3
BASE_URL = "https://www.transfermarkt.com"


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _load_cache() -> Dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {"version": 2, "lastUpdated": None, "leagues": {}}


def _save_cache(cache: Dict) -> None:
    cache["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _cache_key(name: str) -> str:
    return name.lower().replace(" ", "_").replace(".", "").replace("'", "").replace("&", "").replace("-", "_")


def _safe_text(el) -> str:
    try:
        return el.inner_text().strip() if el else ""
    except Exception:
        return ""


def _current_season_id() -> str:
    """Dynamic season: Jul–Dec → current year, Jan–Jun → previous year."""
    now = datetime.now()
    return str(now.year if now.month >= 7 else now.year - 1)


# ── Queue helpers ──────────────────────────────────────────────────────────────

def _load_queue() -> List[Dict]:
    if QUEUE_FILE.exists():
        with open(QUEUE_FILE, "r") as f:
            data = json.load(f)
            return data.get("queue", [])
    return []


def _save_queue(queue: List[Dict]) -> None:
    with open(QUEUE_FILE, "w") as f:
        json.dump({
            "queue": queue,
            "processedToday": 0,
            "lastReset": datetime.now().strftime("%Y-%m-%d")
        }, f, indent=2)


# ── Team search ────────────────────────────────────────────────────────────────

def _search_team_url(browser, team_name: str) -> Optional[Dict]:
    """Search Transfermarkt for a team. Returns {"id": int, "url": str, "name": str} or None."""
    search_url = f"{BASE_URL}/schnellsuche/ergebnis/schnellsuche?query={team_name.replace(' ', '+')}"
    log.info(f"  Searching for: {team_name}")

    page = browser.new_page()
    try:
        page.goto(search_url, wait_until="networkidle", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)

        # Find all team results
        links = page.query_selector_all('a[href*="/startseite/verein/"]')
        results = []
        for link in links:
            href = link.get_attribute("href") or ""
            match = re.search(r'/([^/]+)/startseite/verein/(\d+)', href)
            if match:
                slug, team_id = match.group(1), int(match.group(2))
                display_name = _safe_text(link) or slug.replace("-", " ").title()
                results.append({
                    "id": team_id,
                    "url": f"{BASE_URL}/{slug}/startseite/verein/{team_id}",
                    "name": display_name,
                    "slug": slug,
                })

        if not results:
            log.warning(f"  No results found for {team_name}")
            return None

        # Match by name similarity
        target = team_name.lower().strip()
        best_match = None
        best_score = 0

        # Common abbreviations
        ABBREVIATIONS = {
            "man city": "manchester city",
            "man utd": "manchester united",
            "man united": "manchester united",
            "spurs": "tottenham",
            "barca": "barcelona",
            "real": "real madrid",
            "bayern": "bayern munich",
            "inter": "inter milan",
            "juve": "juventus",
            "ac milan": "milan",
            "psg": "paris saint-germain",
        }
        expanded = ABBREVIATIONS.get(target, target)

        for r in results:
            dn_lower = r["name"].lower()
            # Exact match
            if expanded == dn_lower or target == dn_lower:
                return r
            # Score based on word overlap
            expanded_words = set(expanded.split())
            name_words = set(dn_lower.split())
            score = len(expanded_words & name_words)
            # Bonus for matching start of name
            if dn_lower.startswith(expanded.split()[0]):
                score += 1
            if score > best_score:
                best_score = score
                best_match = r

        if best_match and best_score >= 2:
            log.info(f"  Found: {best_match['name']} (id={best_match['id']})")
            return best_match

        # Fallback: first result
        log.warning(f"  No good match for '{team_name}', using first result: {results[0]['name']}")
        return results[0]

    except Exception as e:
        log.warning(f"  Search failed for {team_name}: {e}")
    finally:
        page.close()
    return None


# ── Spielplandatum parsing (fixtures / form) ──────────────────────────────────

def _parse_spielplandatum(page: Page, team_id: int, team_slug: str) -> Optional[Dict]:
    """Fetch spielplandatum page and extract fixtures using Playwright selectors.
    Returns {"league_id": str, "league_name": str, "fixtures": [...]} or None.
    """
    season = _current_season_id()
    url = f"{BASE_URL}/{team_slug}/spielplandatum/verein/{team_id}?saison_id={season}"
    log.info(f"  Fetching: {url}")

    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(RENDER_WAIT)
    except Exception as e:
        log.warning(f"  Failed to load spielplandatum: {e}")
        return None

    # Check for Cloudflare
    html = page.content()
    if "challenge" in html.lower() or "cloudflare" in html.lower():
        log.warning("  Cloudflare challenge detected, waiting...")
        time.sleep(20)

    # Extract league ID from page analytics or competition links
    league_id = None
    league_name = None
    content = page.content()

    # Try eVar8 analytics — various formats
    league_match = re.search(r"eVar8:\s*'([^']+?)\s*\(([A-Z0-9]+)\)'", content)
    if not league_match:
        # Alternate: "eVar8='Name (ID)'" or double quotes
        league_match = re.search(r"eVar8[=:]\s*[\"']([^\"']+?)\s*\(([A-Z0-9]+)\)[\"']", content)
    if league_match:
        league_name = league_match.group(1).strip()
        league_id = league_match.group(2)

    if not league_id:
        # Fallback: find from competition links
        comp_links = page.query_selector_all('a[href*="/wettbewerb/"]')
        for link in comp_links:
            href = link.get_attribute("href") or ""
            m = re.search(r'/wettbewerb/([A-Z0-9]+)', href)
            if m:
                candidate = m.group(1)
                if candidate not in {"CL", "EL", "EC", "WC", "UC", "CWC"}:
                    league_id = candidate
                    # Try link text, title, aria-label
                    league_name = _safe_text(link) or link.get_attribute("title") or link.get_attribute("aria-label") or candidate
                    break

    # Last resort: look for league name in page title or heading
    if not league_name or league_name == league_id:
        title_el = page.query_selector("h1")
        if title_el:
            title_text = _safe_text(title_el)
            # Extract name and ID from "Premier League Table" or "Premier League (GB1)"
            tn_match = re.search(r"(.+?)\s*(?:Table|Tabelle|Fixture|Schedule|table|fixture|schedule|\(([A-Z0-9]+)\))", title_text)
            if tn_match:
                league_name = tn_match.group(1).strip()
                if not league_id:
                    league_id = tn_match.group(2) or league_id

    # Fallback: known league name map
    if not league_name or league_name == league_id:
        LEAGUE_NAMES = {
            "GB1": "Premier League", "ES1": "LaLiga", "L1": "Bundesliga",
            "IT1": "Serie A", "FR1": "Ligue 1", "NL1": "Eredivisie",
            "PO1": "Primeira Liga", "GB2": "Championship",
            "BE1": "Jupiler Pro League", "TR1": "Süper Lig",
            "RU1": "Premier-Liga", "PL1": "Ekstraklasa",
        }
        if league_id in LEAGUE_NAMES:
            league_name = LEAGUE_NAMES[league_id]

    # Find the Matchday table using Playwright
    fixtures = []
    tables = page.query_selector_all("table")
    for table in tables:
        header = table.query_selector("thead")
        if not header:
            continue
        header_text = _safe_text(header)
        if "Matchday" not in header_text and "Spieltag" not in header_text:
            continue

        rows = table.query_selector_all("tbody tr")
        log.info(f"  Found {len(rows)} fixture rows")

        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) < 10:
                continue

            cell_texts = [_safe_text(c) for c in cells]
            matchday = cell_texts[0]
            date_str = cell_texts[1]
            time_str = cell_texts[2]
            home_away = cell_texts[3]
            ranking = cell_texts[4]
            opponent_raw = cell_texts[6]
            score_raw = cell_texts[9]

            # Skip header-like rows or empty rows
            if not matchday or matchday in ("Matchday", "Spieltag"):
                continue
            if not date_str:
                continue

            # Parse score (format: "3:0", "-:-", "6:5 on pens", "2:1 AET", "6 on pens")
            score = None
            score_match = re.match(r'(\d+)\s*:\s*(\d+)', score_raw)
            if score_match:
                try:
                    score = [int(score_match.group(1)), int(score_match.group(2))]
                except ValueError:
                    log.warning(f"    Could not parse score: '{score_raw}'")
                    score = None
            elif score_raw and score_raw != "-:-":
                # Handle non-standard formats like "6 on pens", "AET", "pen"
                log.debug(f"    Non-standard score format (skipped): '{score_raw}'")

            # Parse opponent name (remove ranking in parentheses)
            opponent = re.sub(r'\s*\(\d+\.\)\s*', '', opponent_raw).strip()
            if not opponent:
                continue

            # Extract opponent link for team ID
            opp_link = cells[6].query_selector('a[href*="/startseite/verein/"]')
            opp_id = None
            if opp_link:
                opp_href = opp_link.get_attribute("href") or ""
                opp_match = re.search(r'/verein/(\d+)', opp_href)
                if opp_match:
                    opp_id = int(opp_match.group(1))

            # Extract match report link for H2H
            match_id = None
            report_link = cells[9].query_selector('a[href*="/spielbericht/"]')
            if report_link:
                report_href = report_link.get_attribute("href") or ""
                id_match = re.search(r'/spielbericht/(\d+)', report_href)
                if id_match:
                    match_id = id_match.group(1)

            # Parse competition from row context
            competition = league_name or ""

            fixtures.append({
                "date": date_str,
                "time": time_str,
                "home_away": home_away,
                "opponent": opponent,
                "opponent_id": opp_id,
                "score": score,
                "matchday": matchday,
                "competition": competition,
                "league_id": league_id or "",
                "match_id": match_id,
                "ranking": ranking,
            })

        break  # Only process the first Matchday table

    # Sort by date descending (most recent first), take last 5 finished
    def parse_date(d):
        # Format: "Sun 16/08/26" or "16/08/2026"
        m = re.search(r'(\d{2}/\d{2}/\d{2,4})', d)
        if m:
            parts = m.group(1).split("/")
            return f"20{parts[2]}" if len(parts[2]) == 2 else parts[2]
        return "0"

    fixtures.sort(key=lambda x: x["date"], reverse=True)
    finished = [f for f in fixtures if f["score"] is not None]
    recent = finished[:5]

    log.info(f"  {len(fixtures)} total fixtures, {len(finished)} finished, {len(recent)} recent")

    return {
        "league_id": league_id,
        "league_name": league_name,
        "fixtures": recent,
        "all_fixtures": fixtures,
    }


# ── League table parsing ───────────────────────────────────────────────────────

def _parse_league_table(page: Page, league_id: str) -> Dict[str, Dict]:
    """Fetch league table from tabelle URL. Returns {team_key: {id, name, position}}."""
    # Map league IDs to URL slugs
    LEAGUE_SLUGS = {
        "GB1": "premier-league", "ES1": "laliga", "L1": "bundesliga",
        "IT1": "serie-a", "FR1": "ligue-1", "NL1": "eredivisie",
        "PO1": "primeira-liga", "GB2": "championship",
        "BE1": "jupiler-pro-league", "TR1": "super-lig",
        "RU1": "premier-liga", "PL1": "ekstraklasa",
    }
    slug = LEAGUE_SLUGS.get(league_id, league_id.lower())
    url = f"{BASE_URL}/{slug}/tabelle/wettbewerb/{league_id}"
    log.info(f"  Fetching table: {url}")

    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(RENDER_WAIT)
    except Exception as e:
        log.warning(f"  Failed to load table: {e}")
        return {}

    html = page.content()
    if "challenge" in html.lower() or "cloudflare" in html.lower():
        log.warning("  Cloudflare on table page, waiting...")
        time.sleep(20)

    teams = {}

    # Find table with rows containing position numbers
    tables = page.query_selector_all("table")
    for table in tables:
        rows = table.query_selector_all("tr")
        if len(rows) < 10:
            continue

        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) < 3:
                continue

            cell_texts = [_safe_text(c) for c in cells]
            # Check if first cell is a position number
            if not cell_texts[0].isdigit():
                continue

            pos = int(cell_texts[0])

            # Find team link — try both startseite andspielplan patterns
            team_link = row.query_selector('a[href*="/startseite/verein/"]') or \
                        row.query_selector('a[href*="/spielplan/verein/"]')
            if not team_link:
                continue

            href = team_link.get_attribute("href") or ""
            id_match = re.search(r'/verein/(\d+)', href)
            if not id_match:
                continue

            team_id = int(id_match.group(1))

            # Get team name — prefer the cell with text, not empty cell
            team_name = ""
            for cell in cells:
                link = cell.query_selector('a[href*="/startseite/verein/"]') or \
                       cell.query_selector('a[href*="/spielplan/verein/"]')
                if link:
                    name = _safe_text(link)
                    if name:
                        team_name = name
                        break

            key = _cache_key(team_name)
            teams[key] = {"id": team_id, "name": team_name, "position": pos}

        if teams:
            break

    log.info(f"  Parsed {len(teams)} teams from table")
    return teams


# ── H2H parsing (spielbericht) ────────────────────────────────────────────────

def _parse_h2h(page: Page, match_id: str) -> List[Dict]:
    """Fetch spielbericht page and extract H2H data."""
    url = f"{BASE_URL}/spielbericht/index/spielbericht/{match_id}"
    log.info(f"  Fetching H2H: {url}")

    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(RENDER_WAIT)
    except Exception as e:
        log.warning(f"  Failed to load spielbericht: {e}")
        return []

    html = page.content()
    if "challenge" in html.lower() or "cloudflare" in html.lower():
        log.warning("  Cloudflare on H2H page, waiting...")
        time.sleep(20)

    h2h = []

    # Look for H2H table — typically under "Direktvergleich" or "Head to Head"
    # Find tables and look for one with past match data
    tables = page.query_selector_all("table")
    for table in tables:
        rows = table.query_selector_all("tr")
        if len(rows) < 3:
            continue

        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) < 4:
                continue

            cell_texts = [_safe_text(c) for c in cells]
            # Look for date pattern (DD.MM.YYYY)
            date_match = None
            for ct in cell_texts:
                dm = re.search(r'(\d{2}\.\d{2}\.\d{4})', ct)
                if dm:
                    date_match = dm.group(1)
                    break

            if not date_match:
                continue

            # Find team names and score
            home = away = ""
            score = None

            team_links = row.query_selector_all('a[href*="/startseite/verein/"]')
            if len(team_links) >= 2:
                home = _safe_text(team_links[0])
                away = _safe_text(team_links[1])

            # Find score (X:Y pattern)
            for ct in cell_texts:
                sm = re.match(r'(\d+:\d+)', ct.strip())
                if sm:
                    try:
                        parts = sm.group(1).split(":")
                        score = [int(parts[0]), int(parts[1])]
                        break
                    except ValueError:
                        log.debug(f"    Could not parse H2H score: '{sm.group(1)}'")
                        continue

            if home and away and score:
                h2h.append({
                    "date": date_match,
                    "home": home,
                    "away": away,
                    "score": score,
                })

    # Sort by date descending, take last 5
    h2h.sort(key=lambda x: x["date"], reverse=True)
    result = h2h[:5]
    log.info(f"  Parsed {len(result)} H2H matches")
    return result


# ── Form computation ───────────────────────────────────────────────────────────

def _compute_team_form(team_name: str, fixtures: List[Dict]) -> Dict[str, List]:
    """Compute W/D/L form for a team from fixture list."""
    team_key = _cache_key(team_name)
    form = []
    gs = []
    gc = []

    for f in fixtures:
        if f["score"] is None:
            continue
        hs, aws = f["score"]
        is_home = f["home_away"] == "H"

        if is_home:
            scored, conceded = hs, aws
        else:
            scored, conceded = aws, hs

        form.append("W" if scored > conceded else ("L" if scored < conceded else "D"))
        gs.append(scored)
        gc.append(conceded)

    return {"form": form, "goals_scored": gs, "goals_conceded": gc}


# ── Cache update helpers ──────────────────────────────────────────────────────

def _upsert_team_in_cache(cache: Dict, league_id: str, league_name: str,
                          team_key: str, team_data: Dict) -> None:
    """Insert or update a team in the league-centric cache."""
    if league_id not in cache["leagues"]:
        cache["leagues"][league_id] = {
            "name": league_name or league_id,
            "table_fetched_at": datetime.now(timezone.utc).isoformat(),
            "teams": {}
        }
    cache["leagues"][league_id]["teams"][team_key] = team_data


def _seed_queue_from_fixtures(queue: List[Dict], fixtures: List[Dict]) -> List[Dict]:
    """Add teams from BetPawa fixtures to the queue (priority 30)."""
    seen = {q["id"] for q in queue}
    for fixture in fixtures:
        for team_name in [fixture.get("home"), fixture.get("away")]:
            if not team_name:
                continue
            tid = f"team:{_cache_key(team_name)}"
            if tid not in seen:
                queue.append({
                    "id": tid,
                    "type": "team",
                    "name": team_name,
                    "url": "",
                    "priority": 30,
                    "addedAt": datetime.now(timezone.utc).isoformat(),
                    "attempts": 0,
                })
                seen.add(tid)
    return queue


def _seed_queue_from_leagues(queue: List[Dict]) -> List[Dict]:
    """Seed queue with top teams from major leagues (standalone mode)."""
    # Top teams from each major league (Transfermarkt team IDs)
    LEAGUE_TEAMS = {
        "GB1": [
            (11, "Arsenal"), (985, "Man United"), (631, "Chelsea"),
            (281, "Man City"), (1068, "Tottenham"), (14, "Liverpool"),
            (1148, "Newcastle"), (1237, "Brighton"), (1178, "Brentford"),
            (873, "Crystal Palace"), (523, "Bournemouth"), (405, "Aston Villa"),
            (703, "Nottm Forest"), (289, "Everton"), (771, "Leeds"),
            (543, "Fulham"), (379, "Ipswich"), (164, "Coventry"),
            (168, "Hull"), (1224, "Sunderland"),
        ],
        "ES1": [
            (418, "Real Madrid"), (131, "Barcelona"), (13, "Atletico Madrid"),
            (1108, "Villarreal"), (331, "Real Sociedad"), (150, "Betis"),
            (621, "Athletic Bilbao"), (940, "Celta Vigo"), (368, "Sevilla"),
            (104, "Valencia"), (693, "Espanyol"), (859, "Deportivo"),
            (366, "Racing"), (364, "Getafe"), (1021, "Levante"),
            (367, "Rayo Vallecano"), (332, "Osasuna"), (596, "Elche"),
            (420, "Alaves"), (1153, "Malaga"),
        ],
        "L1": [
            (27, "Bayern Munich"), (16, "Dortmund"), (173, "RB Leipzig"),
            (18, "Leverkusen"), (1133, "Stuttgart"), (44, "Frankfurt"),
            (396, "Hoffenheim"), (524, "Freiburg"), (111, "Augsburg"),
            (39, "Mainz"), (3, "Koln"), (43, "Hamburg"),
            (18, "Gladbach"), (34, "Bremen"), (41, "Union Berlin"),
            (33, "Schalke"), (2024, "Elversberg"), (301, "Paderborn"),
        ],
        "IT1": [
            (46, "Inter Milan"), (45, "Juventus"), (624, "Como"),
            (12, "Roma"), (5, "AC Milan"), (39, "Atalanta"),
            (6197, "Napoli"), (430, "Fiorentina"), (398, "Lazio"),
            (102, "Bologna"), (431, "Sassuolo"), (563, "Torino"),
            (164, "Genoa"), (556, "Udinese"), (457, "Parma"),
            (444, "Cagliari"), (318, "Venezia"), (1211, "Monza"),
            (1210, "Frosinone"), (1212, "Lecce"),
        ],
        "FR1": [
            (583, "PSG"), (454, "Monaco"), (967, "Strasbourg"),
            (482, "Lyon"), (497, "Lille"), (536, "Rennes"),
            (1063, "Lens"), (481, "Marseille"), (4268, "Paris FC"),
            (521, "Nice"), (621, "Toulouse"), (545, "Auxerre"),
            (1219, "Lorient"), (544, "Brest"), (1145, "Angers"),
            (1214, "Le Havre"), (1218, "Troyes"), (1223, "Le Mans"),
        ],
        "UZB1": [
            (0, "Fardu Ferghana"), (0, "FK Aral Samali"),
            (0, "PFK Metallurg Bekabad"), (0, "FC Pakhtakor Tashkent II"),
        ],
        "ARE23": [
            (0, "Ajman U23"), (0, "AL Nasr U23"),
            (0, "AL Wahda FC U23"), (0, "AL Dhafra U23"),
        ],
        "URU_RES": [
            (0, "Club Atletico Penarol Reserves"), (0, "CA Boston River Reserves"),
            (0, "Montevideo City Torque Reserves"), (0, "Liverpool Montevideo Reserves"),
        ],
        "QAT2": [
            (0, "Umm-Salal SC"), (0, "Al-Khor SC"),
        ],
        "BUL2": [
            (0, "PFK Sportist Svoge"), (0, "Gorna Oryahovitsa"),
            (0, "FC Fratria Varna"), (0, "PFC Chernomorets Burgas"),
        ],
        "TR2": [
            (0, "Istanbulspor AS"), (0, "Igdir FK"),
            (0, "Esenler Erokspor"), (0, "Kayserispor"),
        ],
        "RO1": [
            (0, "SC FC Voluntari"), (0, "ACS Champions FC Arges"),
        ],
        "SVK2": [
            (0, "MSK Zilina B"), (0, "Slovan Bratislava B U21"),
        ],
        "SAU1": [
            (0, "Al-Khaleej Club"), (0, "Al-Riyadh SC"),
        ],
    }

    seen = {q["id"] for q in queue}
    for league_id, teams in LEAGUE_TEAMS.items():
        for team_id, team_name in teams:
            tid = f"team:{_cache_key(team_name)}"
            if tid not in seen:
                queue.append({
                    "id": tid,
                    "type": "team",
                    "name": team_name,
                    "url": "",
                    "priority": 50,
                    "addedAt": datetime.now(timezone.utc).isoformat(),
                    "attempts": 0,
                })
                seen.add(tid)

    log.info(f"  Seeded queue with {len(seen)} teams from {len(LEAGUE_TEAMS)} leagues")
    return queue


# ── Main entry point ──────────────────────────────────────────────────────────

def run_daily_scrape(max_teams: int = 20, betpawa_fixtures: Optional[List[Dict]] = None) -> None:
    """Main entry point for daily scraping."""
    log.info("=" * 50)
    log.info(f"Transfermarkt Daily Scrape (v2) — {datetime.now(timezone.utc).isoformat()}")
    log.info(f"Season: {_current_season_id()}")
    log.info("=" * 50)

    cache = _load_cache()
    queue = _load_queue()

    # Seed queue with BetPawa teams
    if betpawa_fixtures:
        log.info(f"  Seeding queue from {len(betpawa_fixtures)} BetPawa fixtures")
        queue = _seed_queue_from_fixtures(queue, betpawa_fixtures)

    # If queue is empty (standalone run), seed with top teams from major leagues
    if not queue:
        log.info("  No fixtures provided — seeding from major leagues")
        queue = _seed_queue_from_leagues(queue)

    if not queue:
        log.warning("  Queue is empty — nothing to scrape")
        return

    _save_queue(queue)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )

        try:
            # Step 1: Fetch league tables for all leagues we need
            # First, collect league IDs from BetPawa fixtures
            needed_leagues = set()
            if betpawa_fixtures:
                # We'll discover league IDs as we process teams
                pass

            # Step 2: Process team items — fetch spielplandatum for each team
            team_items = [q for q in queue if q["type"] == "team" and q["attempts"] < 3]
            team_items.sort(key=lambda x: x.get("priority", 100))
            team_items = team_items[:max_teams]

            # Filter out teams already cached with recent data (< 48h)
            fresh_cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            filtered_items = []
            for item in team_items:
                team_key = _cache_key(item["name"])
                cached = False
                for lid, ldata in cache.get("leagues", {}).items():
                    td = ldata.get("teams", {}).get(team_key)
                    if td and td.get("last_scraped"):
                        try:
                            last = datetime.fromisoformat(td["last_scraped"].replace("Z", "+00:00"))
                            if last > fresh_cutoff:
                                cached = True
                                break
                        except Exception:
                            pass
                if cached:
                    log.info(f"  Skipping {item['name']} — cached < 48h ago")
                    queue.remove(item)
                else:
                    filtered_items.append(item)

            team_items = filtered_items
            log.info(f"\n  Processing {len(team_items)} teams...")

            # Track which leagues we've already fetched table for
            fetched_tables = set()

            for item in team_items:
                team_name = item["name"]
                team_url = item.get("url", "")

                log.info(f"\n  [{item['attempts']+1}] {team_name}")

                # If no URL, search for team
                if not team_url and team_name:
                    result = _search_team_url(browser, team_name)
                    if result:
                        item["url"] = result["url"]
                        item["name"] = result["name"]
                        team_name = result["name"]
                        item["team_id"] = result["id"]
                        _save_queue(queue)
                    else:
                        log.warning(f"    Could not find URL for {team_name}")
                        item["attempts"] += 1
                        _save_queue(queue)
                        continue

                # Extract team_id from URL if not set
                if "team_id" not in item and team_url:
                    id_match = re.search(r'/verein/(\d+)', team_url)
                    if id_match:
                        item["team_id"] = int(id_match.group(1))
                        _save_queue(queue)

                team_id = item.get("team_id")
                if not team_id:
                    log.warning(f"    No team ID for {team_name}")
                    item["attempts"] += 1
                    _save_queue(queue)
                    continue

                # Extract slug from URL
                slug_match = re.search(r'transfermarkt\.com/([^/]+)/', team_url)
                team_slug = slug_match.group(1) if slug_match else _cache_key(team_name)

                # Create page for this team
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                page = context.new_page()

                try:
                    # Fetch spielplandatum (fixtures)
                    data = _parse_spielplandatum(page, team_id, team_slug)

                    if not data:
                        log.warning(f"    No data returned for {team_name}")
                        item["attempts"] += 1
                        _save_queue(queue)
                        continue

                    league_id = data.get("league_id") or "unknown"
                    league_name = data.get("league_name") or league_id

                    # Fetch league table if not already done
                    if league_id not in fetched_tables and league_id != "unknown":
                        fetched_tables.add(league_id)
                        league_teams = _parse_league_table(page, league_id)
                        if league_teams:
                            log.info(f"    League {league_id}: {len(league_teams)} teams from table")
                            for tk, tdata in league_teams.items():
                                # Preserve existing form data if team already in cache
                                existing = cache.get("leagues", {}).get(league_id, {}).get("teams", {}).get(tk)
                                if existing:
                                    existing["league_position"] = tdata["position"]
                                    existing["total_teams"] = len(league_teams)
                                    existing["last_scraped"] = datetime.now(timezone.utc).isoformat()
                                else:
                                    team_entry = {
                                        "id": tdata["id"],
                                        "name": tdata["name"],
                                        "league_position": tdata["position"],
                                        "total_teams": len(league_teams),
                                        "last_scraped": datetime.now(timezone.utc).isoformat(),
                                        "recent_matches": [],
                                        "form_summary": {"form": [], "goals_scored": [], "goals_conceded": []},
                                        "h2h": {},
                                    }
                                    _upsert_team_in_cache(cache, league_id, league_name, tk, team_entry)
                            _save_cache(cache)

                    # Store our team's form data
                    team_key = _cache_key(team_name)
                    fixtures = data.get("fixtures", [])
                    form = _compute_team_form(team_name, fixtures)

                    # Update team in cache — find by ID first (more reliable)
                    if league_id in cache.get("leagues", {}):
                        teams = cache["leagues"][league_id].get("teams", {})
                        found = False
                        for tk, td in teams.items():
                            if td.get("id") == team_id:
                                td["last_scraped"] = datetime.now(timezone.utc).isoformat()
                                td["recent_matches"] = fixtures
                                td["form_summary"] = form
                                found = True
                                break
                        if not found:
                            # Team not in table — create entry
                            teams[team_key] = {
                                "id": team_id,
                                "name": team_name,
                                "league_position": None,
                                "total_teams": len(teams),
                                "last_scraped": datetime.now(timezone.utc).isoformat(),
                                "recent_matches": fixtures,
                                "form_summary": form,
                                "h2h": {},
                            }
                    else:
                        # League not yet in cache — create it
                        cache["leagues"][league_id] = {
                            "name": league_name,
                            "table_fetched_at": datetime.now(timezone.utc).isoformat(),
                            "teams": {
                                team_key: {
                                    "id": team_id,
                                    "name": team_name,
                                    "league_position": None,
                                    "total_teams": 0,
                                    "last_scraped": datetime.now(timezone.utc).isoformat(),
                                    "recent_matches": fixtures,
                                    "form_summary": form,
                                    "h2h": {},
                                }
                            }
                        }

                    _save_cache(cache)
                    log.info(f"    Form: {form.get('form', [])}")

                finally:
                    context.close()

                # Success — remove from queue instead of incrementing attempts
                queue.remove(item)
                _save_queue(queue)

                time.sleep(REQUEST_DELAY)

            # Step 3: Fetch H2H for today's matches (lazy — only for BetPawa fixtures)
            if betpawa_fixtures:
                log.info(f"\n  Fetching H2H for {len(betpawa_fixtures)} BetPawa matches...")
                h2h_fetched = 0

                for fixture in betpawa_fixtures:
                    home_name = fixture.get("home")
                    away_name = fixture.get("away")
                    if not home_name or not away_name:
                        continue

                    home_key = _cache_key(home_name)
                    away_key = _cache_key(away_name)

                    # Check if H2H already cached
                    h2h_exists = False
                    for lid, ldata in cache.get("leagues", {}).items():
                        t1 = ldata.get("teams", {}).get(home_key, {})
                        if t1.get("h2h", {}).get(away_key):
                            h2h_exists = True
                            break

                    if h2h_exists:
                        continue

                    # Find match ID from cached fixtures
                    match_id = None
                    for lid, ldata in cache.get("leagues", {}).items():
                        t1 = ldata.get("teams", {}).get(home_key, {})
                        for rm in t1.get("recent_matches", []):
                            if (_cache_key(rm.get("opponent", "")) == away_key and
                                rm.get("match_id")):
                                match_id = rm["match_id"]
                                break
                        if match_id:
                            break

                    if not match_id:
                        log.info(f"    No match ID for {home_name} vs {away_name} — skipping H2H")
                        continue

                    # Fetch H2H from spielbericht
                    context = browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    )
                    page = context.new_page()

                    try:
                        h2h_data = _parse_h2h(page, match_id)
                    finally:
                        context.close()

                    if h2h_data:
                        # Store H2H for both teams
                        for lid, ldata in cache.get("leagues", {}).items():
                            teams = ldata.get("teams", {})
                            if home_key in teams:
                                teams[home_key].setdefault("h2h", {})[away_key] = h2h_data
                            if away_key in teams:
                                teams[away_key].setdefault("h2h", {})[home_key] = h2h_data

                        _save_cache(cache)
                        h2h_fetched += 1
                        log.info(f"    H2H cached: {home_name} vs {away_name} ({len(h2h_data)} matches)")

                    time.sleep(REQUEST_DELAY)

                log.info(f"  {h2h_fetched} H2H records fetched")

        finally:
            browser.close()

    log.info("=" * 50)
    log.info("Daily scrape complete (v2)")
    log.info(f"  Leagues: {len(cache.get('leagues', {}))}")
    total_teams = sum(len(l.get("teams", {})) for l in cache.get("leagues", {}).values())
    log.info(f"  Teams: {total_teams}")
    log.info("=" * 50)


if __name__ == "__main__":
    run_daily_scrape()
