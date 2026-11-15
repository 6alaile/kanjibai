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
import difflib  # fuzzy string matching for team name correction

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


def _slugify(name: str) -> str:
    """Convert a team name to a Transfermarkt-style URL slug (hyphenated)."""
    slug = name.lower().strip()
    slug = re.sub(r"[.,'\"&]", "", slug)
    slug = re.sub(r"[^a-z0-9À-ÿ]+", "-", slug).strip("-")
    return slug


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

def _normalize_name(name: str) -> str:
    """Normalize team name for matching: strip prefixes/suffixes/filler words."""
    name = name.lower().strip()
    name = re.sub(r'[.,\'"&/]', '', name)
    # Remove common club prefixes (longest first so "club atletico" beats "atletico")
    prefixes = [
        "club social y deportivo ", "club atletico ", "club deportivo ",
        "clube atletico ", "foot ball club ", "foot-ball club ", "football club ",
        "sporting club ", "sport club ", "sociedad deportiva ", "sociedad ",
        "atletico ", "deportivo ", "club social ", "club ", "sporting ", "sport ",
        "asociacion ", "associacao ", "associacao esportiva ", "esporte clube ",
        "futebol clube ", "foot ball club ",
    ]
    for prefix in sorted(prefixes, key=len, reverse=True):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    # Remove common suffixes
    suffixes = [" fc", " cf", " sc", " cd", " ec", " ac", " af", " cs", " ss"]
    for suffix in sorted(suffixes, key=len, reverse=True):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    # Remove filler words that carry no identity: de/del/la/el/los/las/y/the
    filler = {"de", "del", "la", "el", "los", "las", "y", "the", "van", "von", "da", "do"}
    tokens = [t for t in name.split() if t not in filler]
    name = " ".join(tokens)
    # Remove extra spaces
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def _detect_gender(name: str) -> Optional[str]:
    """Detect whether a team name refers to a women's side.

    Returns 'w' for women, None when there is no explicit marker
    (an unmarked name is treated as ambiguous/men's-default).
    """
    lower = name.lower()
    # Word-boundary tokens so 'w' inside other words isn't matched
    tokens = set(re.findall(r"[a-zà-ÿ]+", lower))
    if tokens & {"women", "woman", "femenino", "femenina", "femenil",
                 "damen", "weiblich", "wfc", "ladies"}:
        return "w"
    return None


def _strip_gender_marker(name: str) -> str:
    """Remove women's-side markers so the core club identity can be compared."""
    lower = name.lower().strip()
    lower = re.sub(r"\b(women|woman|femenino|femenina|femenil|damen|weiblich|wfc|ladies)\b",
                   "", lower)
    return re.sub(r"\s+", " ", lower).strip()


def _distinctive_tokens(name: str) -> set:
    """Return the set of distinctive (non-filler) words in a normalized name."""
    filler = {"de", "del", "la", "el", "los", "las", "y", "the", "van", "von", "da", "do",
              "atletico", "deportivo", "club", "sporting", "sport", "united", "city",
              "women", "woman", "femenino", "femenina", "femenil", "damen", "weiblich",
              "wfc", "ladies"}
    return {t for t in name.split() if t not in filler}


def _search_team_url(browser, team_name: str) -> Optional[Dict]:
    """Search Transfermarkt for a team. Returns {"id": int, "url": str, "name": str} or None."""
    # If the name marks a women's side, keep the marker in the query so
    # Transfermarkt returns the women's team rather than the men's default.
    query = team_name.replace(' ', '+')
    if _detect_gender(team_name) == "w":
        query = query + "+women"
    search_url = f"{BASE_URL}/schnellsuche/ergebnis/schnellsuche?query={query}"
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

        # ── Name similarity scoring ─────────────────────────────────────────────
        target_raw = team_name.lower().strip()
        target_norm = _normalize_name(team_name)
        target_tokens = _distinctive_tokens(target_norm)
        target_gender = _detect_gender(team_name)

        best_match = None
        best_score = 0.0

        for r in results:
            dn_raw = r["name"].lower()
            dn_norm = _normalize_name(r["name"])
            dn_tokens = _distinctive_tokens(dn_norm)
            dn_gender = _detect_gender(r["name"])

            # Gender guard: never cross-match men's and women's sides
            if target_gender == "w" and dn_gender != "w":
                continue
            if target_gender is None and dn_gender == "w":
                # Unmarked target is ambiguous; skip explicit women's entries
                # unless the name also carries the marker, which it doesn't here.
                continue

            # Exact match on raw or normalized form → immediate return
            if target_raw == dn_raw or target_norm == dn_norm:
                return r

            # Token overlap ratio (Jaccard-like, weighted by distinctiveness)
            if target_tokens and dn_tokens:
                inter = len(target_tokens & dn_tokens)
                union = len(target_tokens | dn_tokens)
                token_ratio = inter / union if union else 0
            else:
                token_ratio = 0

            # Substring containment bonus
            contains = 0.0
            if target_norm in dn_norm or dn_norm in target_norm:
                contains = 0.5
            elif target_raw in dn_raw or dn_raw in target_raw:
                contains = 0.3

            # Prefix bonus (first distinctive word matches)
            prefix_bonus = 0.0
            t_first = target_norm.split()[0] if target_norm.split() else ""
            d_first = dn_norm.split()[0] if dn_norm.split() else ""
            if t_first and d_first and (t_first == d_first or t_first.startswith(d_first) or d_first.startswith(t_first)):
                prefix_bonus = 0.2

            # Slug similarity (URL slug often encodes the team name)
            slug_bonus = 0.0
            slug_tokens = set(re.split(r'[-_]', r["slug"].lower()))
            slug_overlap = len(target_tokens & slug_tokens)
            if slug_overlap:
                slug_bonus = min(0.3, slug_overlap * 0.15)

            # Combined score
            score = token_ratio + contains + prefix_bonus + slug_bonus

            if score > best_score:
                best_score = score
                best_match = r

        # Decision thresholds
        if best_match and best_score >= 0.6:
            log.info(f"  Found: {best_match['name']} (id={best_match['id']}, score={best_score:.2f})")
            return best_match

        if best_match and best_score >= 0.3:
            log.info(f"  Close match: {best_match['name']} (id={best_match['id']}, score={best_score:.2f})")
            return best_match

        # Fuzzy string matching fallback — still respect the gender guard
        eligible = [r for r in results if _detect_gender(r["name"]) != "w" or target_gender == "w"]
        if not eligible:
            log.warning(f"  All {len(results)} results excluded by gender guard for '{team_name}'")
            return None
        fuzzy_pool = [r["name"].lower() for r in eligible]
        fuzzy = difflib.get_close_matches(target_raw, fuzzy_pool, n=1, cutoff=0.5)
        if fuzzy:
            for r in eligible:
                if r["name"].lower() == fuzzy[0]:
                    log.info(f"  Fuzzy match: {r['name']} (id={r['id']})")
                    return r

        # Last resort: first eligible result
        log.warning(f"  No confident match for '{team_name}' (best={best_score:.2f}), using first eligible: {eligible[0]['name']}")
        return eligible[0]

    except Exception as e:
        log.warning(f"  Search failed for {team_name}: {e}")
    finally:
        page.close()
    return None


# ── Spielplandatum parsing (fixtures / form) ──────────────────────────────────

def _fixture_url(team_slug: str, team_id: int, season: str) -> str:
    """Build a Transfermarkt fixture URL in the canonical /plus/0 form.

    The bare ?saison_id= variant returns an unfiltered shell for many clubs;
    the /plus/0 path with explicit filter params is what actually renders
    the matchday table.
    """
    return (
        f"{BASE_URL}/{team_slug}/spielplandatum/verein/{team_id}/plus/0"
        f"?saison_id={season}&wettbewerb_id=&day=&heim_gast=&punkte="
        f"&datum_von=&datum_bis="
    )


def _candidate_seasons() -> List[str]:
    """Current season only — prior seasons are irrelevant for form.

    The one-season fallback that previously tried 2024/2025 is removed:
    old fixtures distort recent form and slow the scrape significantly.
    H2H lookups (spielbericht pages) may still reference older match IDs
    but those are fetched lazily and are not subject to this limit.
    """
    return [_current_season_id()]


def _extract_league_from_page(page: Page) -> tuple:
    """Extract (league_id, league_name) from a loaded fixture page."""
    content = page.content()
    league_id = None
    league_name = None

    # Try eVar8 analytics — various formats
    league_match = re.search(r"eVar8:\s*'([^']+?)\s*\(([A-Z0-9]+)\)'", content)
    if not league_match:
        league_match = re.search(r"eVar8[=:]\s*[\"']([^\"']+?)\s*\(([A-Z0-9]+)\)[\"']", content)
    if league_match:
        league_name = league_match.group(1).strip()
        league_id = league_match.group(2)

    if not league_id:
        comp_links = page.query_selector_all('a[href*="/wettbewerb/"]')
        for link in comp_links:
            href = link.get_attribute("href") or ""
            m = re.search(r'/wettbewerb/([A-Z0-9]+)', href)
            if m:
                candidate = m.group(1)
                if candidate not in {"CL", "EL", "EC", "WC", "UC", "CWC"}:
                    league_id = candidate
                    league_name = (_safe_text(link) or link.get_attribute("title")
                                   or link.get_attribute("aria-label") or candidate)
                    break

    if not league_name or league_name == league_id:
        title_el = page.query_selector("h1")
        if title_el:
            title_text = _safe_text(title_el)
            tn_match = re.search(
                r"(.+?)\s*(?:Table|Tabelle|Fixture|Schedule|table|fixture|schedule|\(([A-Z0-9]+)\))",
                title_text,
            )
            if tn_match:
                league_name = tn_match.group(1).strip()
                if not league_id:
                    league_id = tn_match.group(2) or league_id

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

    return league_id, league_name


def _extract_fixtures_from_page(page: Page, league_id, league_name) -> List[Dict]:
    """Parse fixture rows out of the currently loaded page."""
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

            if not matchday or matchday in ("Matchday", "Spieltag"):
                continue
            if not date_str:
                continue

            score = None
            score_match = re.match(r'(\d+)\s*:\s*(\d+)', score_raw)
            if score_match:
                try:
                    score = [int(score_match.group(1)), int(score_match.group(2))]
                except ValueError:
                    log.warning(f"    Could not parse score: '{score_raw}'")
                    score = None

            opponent = re.sub(r'\s*\(\d+\.\)\s*', '', opponent_raw).strip()
            if not opponent:
                continue

            opp_link = cells[6].query_selector('a[href*="/startseite/verein/"]')
            opp_id = None
            if opp_link:
                opp_href = opp_link.get_attribute("href") or ""
                opp_match = re.search(r'/verein/(\d+)', opp_href)
                if opp_match:
                    opp_id = int(opp_match.group(1))

            match_id = None
            report_link = cells[9].query_selector('a[href*="/spielbericht/"]')
            if report_link:
                report_href = report_link.get_attribute("href") or ""
                id_match = re.search(r'/spielbericht/(\d+)', report_href)
                if id_match:
                    match_id = id_match.group(1)

            fixtures.append({
                "date": date_str,
                "time": time_str,
                "home_away": home_away,
                "opponent": opponent,
                "opponent_id": opp_id,
                "score": score,
                "matchday": matchday,
                "competition": league_name or "",
                "league_id": league_id or "",
                "match_id": match_id,
                "ranking": ranking,
            })

        break  # Only process the first Matchday table

    return fixtures


def _parse_spielplandatum(page: Page, team_id: int, team_slug: str) -> Optional[Dict]:
    """Fetch spielplandatum page and extract fixtures using Playwright selectors.
    Returns {"league_id": str, "league_name": str, "fixtures": [...]} or None.

    Tries the current season first, then walks backwards up to two seasons
    so leagues whose data lags still resolve.
    """
    for season in _candidate_seasons():
        url = _fixture_url(team_slug, team_id, season)
        log.info(f"  Fetching: {url}")

        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(RENDER_WAIT)
        except Exception as e:
            log.warning(f"  Failed to load spielplandatum ({season}): {e}")
            continue

        # Check for Cloudflare
        html = page.content()
        if "challenge" in html.lower() or "cloudflare" in html.lower():
            log.warning("  Cloudflare challenge detected, waiting...")
            time.sleep(20)

        league_id, league_name = _extract_league_from_page(page)
        fixtures = _extract_fixtures_from_page(page, league_id, league_name)

        if fixtures:
            break  # Got data for this season, stop trying older ones
        log.info(f"  No fixtures for season {season}, trying previous...")

    if not fixtures:
        log.warning(f"  No fixtures found for {team_slug} across seasons {_candidate_seasons()}")
        return None

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


def _h2h_tokens(name: str) -> set:
    """Identity tokens of a team name, for aligning Transfermarkt and odds names."""
    toks = set()
    for token in re.findall(r"[a-z0-9]+", name.lower()):
        if token in {"fc", "sc", "cf", "ac", "af", "cd", "ec", "ss",
                     "club", "sociedad", "city", "town"}:
            continue
        toks.add("united" if token in ("utd", "united") else token)
    return toks


def _rename_h2h(matches: List[Dict], home_name: str, away_name: str) -> List[Dict]:
    """Rewrite Transfermarkt team names in H2H records to the odds-feed names.

    app.js compares the H2H names against the fixture's team names to work
    out who won, so the two have to be the same strings.
    """
    home_tokens = _h2h_tokens(home_name)
    away_tokens = _h2h_tokens(away_name)
    renamed = []

    for match in matches:
        entry = dict(match)
        stored_home = _h2h_tokens(match.get("home", ""))
        stored_away = _h2h_tokens(match.get("away", ""))

        if stored_home and away_tokens and (stored_home & away_tokens) \
                and not (stored_home & home_tokens):
            # The stored "home" is really our away team — swap names and score
            entry["home"] = away_name
            entry["away"] = home_name
            score = match.get("score")
            if isinstance(score, list) and len(score) == 2:
                entry["score"] = [score[1], score[0]]
        else:
            entry["home"] = home_name
            entry["away"] = away_name
        renamed.append(entry)

    return renamed


# ── League table parsing ─────────────────────────────────────────────────────

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

    # The head-to-head table is the one headed "Competition | Matchday | Date |
    # Home team | Away team | Attendance | Result".
    tables = page.query_selector_all("table")
    for table in tables:
        header = table.query_selector("thead")
        if not header:
            continue
        header_text = _safe_text(header)
        if "Matchday" not in header_text and "Spieltag" not in header_text:
            continue

        for row in table.query_selector_all("tbody tr"):
            cells = row.query_selector_all("td")
            if len(cells) < 4:
                continue

            cell_texts = [_safe_text(c) for c in cells]

            # Date: "Sat 22/11/2025" (Transfermarkt dropped the dotted format)
            date_match = None
            for ct in cell_texts:
                dm = re.search(r'(\d{2}[./]\d{2}[./]\d{4})', ct)
                if dm:
                    date_match = dm.group(1)
                    break
            if not date_match:
                continue

            # Team names: the club links point at /spielplan/verein/<id>,
            # and each side has both a crest link and a name link, so only
            # the links with text are the team names.
            team_links = [
                a for a in row.query_selector_all('a[href*="/verein/"]')
                if _safe_text(a)
            ]
            if len(team_links) < 2:
                continue
            home = _strip_ranking(_safe_text(team_links[0]))
            away = _strip_ranking(_safe_text(team_links[1]))

            score = None
            for ct in cell_texts:
                sm = re.match(r'(\d+)\s*:\s*(\d+)', ct.strip())
                if sm:
                    try:
                        score = [int(sm.group(1)), int(sm.group(2))]
                        break
                    except ValueError:
                        continue

            if home and away and score:
                h2h.append({
                    "date": _iso_date(date_match),
                    "home": home,
                    "away": away,
                    "score": score,
                })

        break  # only the first H2H table

    # Sort by date descending, take last 5
    h2h.sort(key=lambda x: x["date"], reverse=True)
    result = h2h[:5]
    log.info(f"  Parsed {len(result)} H2H matches")
    return result


def _strip_ranking(name: str) -> str:
    """Remove the trailing position marker Transfermarkt appends ("Vissel Kobe  (4.)")."""
    return re.sub(r'\s*\(\d+\.\)\s*$', '', name).strip()


def _iso_date(date_str: str) -> str:
    """Normalise "22/11/2025" to "2025-11-22" so the list sorts correctly."""
    parts = date_str.replace(".", "/").split("/")
    if len(parts) != 3:
        return date_str
    day, month, year = parts
    return f"{year}-{month}-{day}"


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


def _strip_fixtures_for_cache(fixtures: List[Dict]) -> List[Dict]:
    """Return fixtures stripped to only fields needed for form computation (score, home_away)."""
    stripped = []
    for f in fixtures:
        if f.get("score") is not None:
            stripped.append({
                "score": f["score"],
                "home_away": f.get("home_away", "H"),
            })
    return stripped


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

            # In-memory index: league_id -> {home_key -> {away_key -> match_id}}
            # Built from full fixtures during scraping; used for H2H lookup (no match_id in stripped recent_matches)
            match_id_index: Dict[str, Dict[str, Dict[str, str]]] = {}

            # Maps the odds-feed team name to the Transfermarkt team name, so
            # the H2H index (which is keyed by Transfermarkt names) can be
            # looked up with the names from the match list.
            odds_to_tm: Dict[str, str] = {}

            for item in team_items:
                team_name = item["name"]
                team_url = item.get("url", "")

                log.info(f"\n  [{item['attempts']+1}] {team_name}")

                # The name as it appears in the odds feed.  The H2H index is
                # keyed by these names because that is what the match list
                # looks up, so keep it even after the Transfermarkt name
                # replaces team_name below.
                odds_name = team_name

                # If no URL, search for team
                if not team_url and team_name:
                    result = _search_team_url(browser, team_name)
                    if result:
                        item["url"] = result["url"]
                        item["name"] = result["name"]
                        item["slug"] = result.get("slug") or _slugify(result["name"])
                        team_name = result["name"]
                        item["team_id"] = result["id"]
                        odds_to_tm[_cache_key(odds_name)] = team_name
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

                # Slug: prefer the one resolved during search, then URL, then name-derived
                if item.get("slug"):
                    team_slug = item["slug"]
                else:
                    slug_match = re.search(r'transfermarkt\.com/([^/]+)/', team_url)
                    team_slug = slug_match.group(1) if slug_match else _slugify(team_name)
                    item["slug"] = team_slug
                    _save_queue(queue)

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

                    # Build match_id_index from full fixtures (all_fixtures) for H2H lookup
                    all_fixtures = data.get("all_fixtures", [])
                    if all_fixtures and league_id != "unknown":
                        idx = match_id_index.setdefault(league_id, {})
                        team_key = _cache_key(team_name)
                        for f in all_fixtures:
                            match_id = f.get("match_id")
                            if not match_id:
                                continue
                            opp_key = _cache_key(f.get("opponent", ""))
                            if f.get("home_away") == "H":
                                # This team was home, opponent was away
                                home_key, away_key = team_key, opp_key
                            else:
                                # This team was away, opponent was home
                                home_key, away_key = opp_key, team_key
                            idx.setdefault(home_key, {})[away_key] = match_id

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

                    # Persist stripped fixtures: complete form (>=5) -> empty list; partial -> stripped {score, home_away}
                    if len(fixtures) >= 5:
                        stored_fixtures = []
                    else:
                        stored_fixtures = _strip_fixtures_for_cache(fixtures)

                    # Update team in cache — find by ID first (more reliable)
                    if league_id in cache.get("leagues", {}):
                        teams = cache["leagues"][league_id].get("teams", {})
                        found = False
                        for tk, td in teams.items():
                            if td.get("id") == team_id:
                                td["last_scraped"] = datetime.now(timezone.utc).isoformat()
                                td["recent_matches"] = stored_fixtures
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
                                "recent_matches": stored_fixtures,
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
                                    "recent_matches": stored_fixtures,
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

                    # Find match ID from in-memory index (built from full fixtures during scraping)
                    match_id = None
                    # The index is keyed by Transfermarkt names, so translate
                    # the names from the match list before looking it up.
                    tm_home_key = _cache_key(odds_to_tm.get(home_key, home_name))
                    tm_away_key = _cache_key(odds_to_tm.get(away_key, away_name))
                    for lid, idx in match_id_index.items():
                        if tm_home_key in idx and tm_away_key in idx[tm_home_key]:
                            match_id = idx[tm_home_key][tm_away_key]
                            break
                        if tm_away_key in idx and tm_home_key in idx[tm_away_key]:
                            match_id = idx[tm_away_key][tm_home_key]
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
                        # Align the team names with the ones used by the odds feed
                        h2h_data = _rename_h2h(h2h_data, home_name, away_name)
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
