"""
flashscore_source.py — Flashscore as a data source for Kanjibai (PROTOTYPE)

Why this exists
---------------
Transfermarkt (via Playwright) misses data for several teams — e.g. on
2026-11-15 "Kagoshima United" resolved to no form and no league position,
even though the same club is listed on Flashscore as "Kagoshima Utd"
(J3 League, 3rd, form W-L-D-D-L).  Flashscore was evaluated as a second
and/or fallback provider.

How it works (no browser required)
----------------------------------
1. Search   GET https://s.livesport.services/api/v2/search/?q=<name>
            -> participant id, url slug, gender, country.  Plain JSON.
2. Team     GET https://www.flashscore.com/team/<slug>/<id>/
            -> the page server-renders its live data as
               `cjs.initialFeeds["results"] = { data: `<feed>` }`.
3. Table    GET https://global.flashscore.ninja/2/x/feed/tl_<table>_<season>
            -> league standings (position, played, W/D/L, goals, points).
            This one call needs the `x-fsign` header.

Feed format
-----------
Rows separated by "~", fields by "\xac", each field is "<CODE>\xf7<value>".
Codes are 2 or 3 characters and MUST be kept distinct: "ZC" is the season
id while "ZCC" is a different flag.  Collapsing them to 2 chars silently
destroys the season id, which then breaks the standings lookup.

Known field map (verified against live pages on 2026-11-15):
  event   AA=match id  AD=kickoff epoch  AE=home name  AF=away name
          AG=home score AH=away score  PX=home team id  PY=away team id
  league  ZE=table id  ZC=season id  ZL=league path  ZY=country  ZK=name
  table   TN=name  TI=team id  TIU=url  TR=position  TM=played  TW=wins
          TDR=draws  TL=losses  TP=points  TG=goals "29:19"  TPF=goal diff

Per-fixture feed (df_hh_1_{match_id}, verified 2026-11-16) — this is what
the fixture's H2H tab shows: a "Last matches" section per team plus a
"Head-to-head matches" section.
  row     KC=kickoff epoch  KP=match id  KF=league  KH=country  KI=league code
          UQ=home team id  UO=away team id  KJ=home name ("*" = section team)
          KK=away name     KL="home:away"  KU=home goals  KT=away goals
          KS=side of the section's own team ("home"/"away")
          WIS=result badge for that team ("w"/"d"/"l")
  section KB="Last matches: <team>" | "Head-to-head matches"
          KA="Overall" | "<team> - Home" | "<team> - Away"
"""

import json
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import requests

logging.basicConfig(level=logging.INFO, format="[flashscore] %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

VAL_SEP = "\xac"
KEY_SEP = "\xf7"

FS_BASE = "https://www.flashscore.com"
FS_FEED = "https://global.flashscore.ninja/2/x/feed/{}"
FS_SEARCH = ("https://s.livesport.services/api/v2/search/?q={q}&lang-id=1"
             "&type-ids=1,2,3,4&project-id=2&project-type-id=1")
FS_FSIGN = "SW9D1eZo"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

REQUEST_DELAY = 0.4

# Club-type tokens that carry no identifying information.
GENERIC = {"fc", "sc", "cf", "ac", "af", "cd", "ec", "ss", "fzc", "fcp", "bk",
           "if", "ff", "nk", "fk", "sk", "ogc", "bsc", "vfl", "vfb", "tsg",
           "sd", "ud", "rc", "rcd", "us", "as", "ca", "ssc", "sv", "bs"}

WOMEN_MARKERS = {"women", "woman", "ladies", "lady", "femenina", "femenino",
                 "feminine", "womens", "damen", "weiblich"}

# Normalisation pairs: Flashscore and bookmakers spell these differently.
ABBREV = {
    "united": "utd", "utd": "united", "city": "cty", "athletic": "ath",
    "atl": "athletic", "real": "rl", "sporting": "sp", "football": "fb",
    "futebol": "fb", "club": "cl", "futbol": "fb", "deportivo": "dep",
}

# Women's team name aliases (bookmaker name -> Flashscore name).
# Flashscore abbreviates women's clubs as "<Name> W"; some are so heavily
# renamed (Cerezo Osaka Sakai Ladies -> "C-Osaka W") that token matching
# can never connect them, so map them directly.
WOMEN_ALIASES = {
    "rapid vienna women": "SK Rapid W",
    "rapid wien women": "SK Rapid W",
    "cerezo osaka sakai ladies": "C-Osaka W",
    "cerezo osaka sakai ladies women": "C-Osaka W",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": FS_BASE + "/",
})


# ── feed decoding ────────────────────────────────────────────────────────────

def parse_feed(raw: str) -> List[Dict[str, str]]:
    """Decode a Flashscore feed into a list of field dicts (full field codes)."""
    rows: List[Dict[str, str]] = []
    for row in raw.split("~"):
        if not row.strip():
            continue
        fields: Dict[str, str] = {}
        for part in row.split(VAL_SEP):
            if KEY_SEP not in part:
                continue
            code, _, value = part.partition(KEY_SEP)
            fields[code] = value
        if fields:
            rows.append(fields)
    return rows


def grab_feed(html: str, key: str) -> Optional[str]:
    """Pull `cjs.initialFeeds["<key>"]` out of a rendered Flashscore page."""
    match = re.search(
        r'cjs\.initialFeeds\["' + re.escape(key) + r'"\]\s*=\s*\{\s*data:\s*`(.*?)`',
        html, re.S)
    return match.group(1) if match else None


# ── name handling ────────────────────────────────────────────────────────────

def _tokens(name: str) -> set:
    """Lowercase tokens, with the women's markers folded onto Flashscore's "W"."""
    out = set()
    for token in re.findall(r"[a-z0-9]+", (name or "").lower()):
        token = ABBREV.get(token, token)
        out.add("w" if token in WOMEN_MARKERS else token)
    return out


def _distinctive(name: str) -> set:
    """Tokens that actually identify a club."""
    return {t for t in _tokens(name) if t not in GENERIC}


def target_gender(name: str) -> str:
    """'Women' when the name carries a women's marker, otherwise 'Men'."""
    tokens = set(re.findall(r"[a-z0-9]+", name.lower()))
    # Only consider women markers explicitly in the name, not standalone "w" from words like "wien"
    if tokens & WOMEN_MARKERS:
        return "Women"
    return "Men"


def _query_variants(name: str) -> List[str]:
    """Search queries to try, most specific first.

    Flashscore's search ranks poorly on long names, so drop club-type tokens
    and retry with the first two words. Women's clubs are stored as
    "<Name> W", so append a " W" variant for women's teams.
    """
    variants = [name]
    stripped = " ".join(
        word for word in name.split()
        if word.lower().strip(".") not in GENERIC
    )
    if stripped and stripped != name:
        variants.append(stripped)
    words = stripped.split()
    if len(words) > 2:
        variants.append(" ".join(words[:2]))

    if target_gender(name) == "Women":
        core = " ".join(w for w in stripped.split() if w.lower() not in WOMEN_MARKERS)
        if core and core != stripped:
            variants.append(f"{core} W")
            core_words = core.split()
            if len(core_words) > 1:
                variants.append(" ".join(core_words[:2]) + " W")

    seen, out = set(), []
    for variant in variants:
        key = variant.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(variant)
    return out


# ── step 1: name -> participant ──────────────────────────────────────────────

def search_team(name: str) -> Optional[Dict]:
    """Resolve a team name to a Flashscore participant.

    Returns {id, slug, name, gender, country, score} or None when nothing
    plausible is found.  Men and women are never matched across.
    """
    # Women's clubs are stored as "<Name> W" on Flashscore and the search API
    # never indexes the "Women"/"Ladies" tokens, so swap in the canonical name
    # before querying (but keep the original gender for filtering).
    wanted_gender = target_gender(name)
    query_name = name
    name_lower = name.lower().strip()
    for alias, flashscore_name in WOMEN_ALIASES.items():
        if alias in name_lower:
            query_name = flashscore_name
            break

    target = _distinctive(query_name)
    best, best_score = None, 0.0

    for query in _query_variants(query_name):
        try:
            response = SESSION.get(
                FS_SEARCH.format(q=query.replace(" ", "+")), timeout=20)
            response.raise_for_status()
            results = response.json()
        except Exception as exc:                       # noqa: BLE001
            log.warning("  search failed for %r (%s): %s", query, name, exc)
            continue

        for rank, item in enumerate(results):
            if item.get("sport", {}).get("id") != 1:     # soccer only
                continue
            if item.get("type", {}).get("id") != 2:      # team only
                continue
            if item.get("gender", {}).get("name", "Men") != wanted_gender:
                continue

            candidate = _distinctive(item["name"])
            score = len(target & candidate) / max(1, len(target | candidate))
            if candidate and candidate < target:
                # "Kumamoto" is plausibly the same club as "Roasso Kumamoto"
                score += 0.15
            if item["name"].lower() == name.lower():
                score = 2.0
            score -= rank * 0.001
            if score > best_score:
                best_score, best = score, item

        if best and best_score >= 0.8:
            break

    if not best or best_score < 0.3:
        return None
    return {
        "id": best["id"],
        "slug": best["url"],
        "name": best["name"],
        "gender": best.get("gender", {}).get("name"),
        "country": best.get("defaultCountry", {}).get("name"),
        "score": round(best_score, 3),
    }


# ── step 2: team page -> results ────────────────────────────────────────────

def fetch_team_page(team_id: str, slug: str) -> Dict[str, List[Dict[str, str]]]:
    """Fetch a team page and return its inlined feeds."""
    url = f"{FS_BASE}/team/{slug}/{team_id}/"
    response = SESSION.get(url, timeout=30)
    response.raise_for_status()
    html = response.text

    page: Dict[str, List[Dict[str, str]]] = {"url": url}
    for key in ("results", "fixtures", "summary-results", "summary-fixtures"):
        raw = grab_feed(html, key)
        if raw:
            page[key] = parse_feed(raw)
    return page


def events_from(rows: List[Dict[str, str]]) -> List[Dict]:
    """Normalise event rows into plain dicts."""
    events = []
    for row in rows:
        if "AA" not in row or "AD" not in row:
            continue
        home_score, away_score = row.get("AG"), row.get("AH")
        events.append({
            "id": row.get("AA"),
            "ts": int(row.get("AD", 0)),
            "home": row.get("AE") or row.get("CX"),
            "away": row.get("AF") or row.get("FK"),
            "home_score": int(home_score) if home_score not in (None, "", "-1") else None,
            "away_score": int(away_score) if away_score not in (None, "", "-1") else None,
            "home_id": row.get("PX"),
            "away_id": row.get("PY"),
        })
    return events


def team_form(events: List[Dict], team_id: str, n: int = 5) -> Dict:
    """Last n finished results for a team, oldest first (matches app.js cap5)."""
    played = []
    for event in events:
        if event["home_score"] is None or event["away_score"] is None:
            continue
        if team_id not in (event["home_id"], event["away_id"]):
            continue
        is_home = event["home_id"] == team_id
        scored = event["home_score"] if is_home else event["away_score"]
        conceded = event["away_score"] if is_home else event["home_score"]
        played.append({
            "ts": event["ts"],
            "match_id": event.get("AA", ""),
            "opponent": event["away"] if is_home else event["home"],
            "opponent_id": event["away_id"] if is_home else event["home_id"],
            "home_away": "H" if is_home else "A",
            "scored": scored,
            "conceded": conceded,
            "result": "W" if scored > conceded else ("L" if scored < conceded else "D"),
        })

    played.sort(key=lambda m: m["ts"], reverse=True)
    recent = list(reversed(played[:n]))
    return {
        "form": [m["result"] for m in recent],
        "goals_scored": [m["scored"] for m in recent],
        "goals_conceded": [m["conceded"] for m in recent],
        "matches": played,
    }


def table_ref(page: Dict) -> Optional[Dict]:
    """Extract the league table reference from a team page's feeds."""
    for key in ("results", "fixtures", "summary-results", "summary-fixtures"):
        for row in page.get(key, []):
            if "ZE" not in row or "ZL" not in row:
                continue
            season = row.get("ZC")
            if season and season != "0":
                return {
                    "table_id": row["ZE"],
                    "season_id": season,
                    "league_path": row.get("ZL"),
                    "league_country": row.get("ZY"),
                    "league_name": row.get("ZK") or row.get("ZA"),
                }
    return None


def table_refs(page: Dict) -> List[Dict]:
    """Every league table reference a team's feeds mention, newest first.

    A club can sit in more than one table (groups, Apertura/Clausura phases),
    so all candidates are offered to the standings lookup.
    """
    refs: List[Dict] = []
    seen = set()
    for key in ("results", "fixtures", "summary-results", "summary-fixtures"):
        for row in page.get(key, []):
            season = row.get("ZC")
            if "ZE" not in row or not season or season == "0":
                continue
            key2 = (row["ZE"], season)
            if key2 in seen:
                continue
            seen.add(key2)
            refs.append({
                "table_id": row["ZE"],
                "season_id": season,
                "league_path": row.get("ZL"),
                "league_country": row.get("ZY"),
                "league_name": row.get("ZK") or row.get("ZA"),
            })
    return refs


# ── step 3: standings ────────────────────────────────────────────────────────

def fetch_standings(table_id: str, season_id: str) -> List[Dict]:
    """Fetch a league table: position, played, W/D/L, goals and points per team."""
    response = SESSION.get(
        FS_FEED.format(f"tl_{table_id}_{season_id}"),
        headers={"x-fsign": FS_FSIGN}, timeout=30)
    response.raise_for_status()

    table = []
    for row in parse_feed(response.text):
        if "TN" not in row or "TR" not in row:
            continue
        table.append({
            "position": int(row["TR"]),
            "name": row["TN"],
            "played": int(row.get("TM", 0)),
            "wins": int(row.get("TW", 0)),
            "draws": int(row.get("TDR", 0)),
            "losses": int(row.get("TL", 0)),
            "goals": row.get("TG"),
            "points": int(row.get("TP", 0)),
            "team_id": row.get("TI"),
            "url": row.get("TIU"),
        })
    return table


# ── orchestration ────────────────────────────────────────────────────────────

def fetch_team_data(name: str, n: int = 5) -> Optional[Dict]:
    """Full pipeline for one team: search -> page -> form + league position."""
    hit = search_team(name)
    if not hit:
        return None

    page = fetch_team_page(hit["id"], hit["slug"])
    form = team_form(events_from(page.get("results", [])), hit["id"], n=n)

    position = total = None
    ref = None
    for candidate in table_refs(page):
        try:
            table = fetch_standings(candidate["table_id"], candidate["season_id"])
        except Exception as exc:                       # noqa: BLE001
            log.warning("  standings lookup failed for %s: %s", name, exc)
            continue
        if not table:
            continue
        for row in table:
            if row["team_id"] == hit["id"]:
                position, total, ref = row["position"], len(table), candidate
                break
        if ref:
            break

    return {
        "id": hit["id"],
        "slug": hit["slug"],
        "name": hit["name"],
        "matched_name": name,
        "gender": hit["gender"],
        "country": hit["country"],
        "match_score": hit["score"],
        "form": form["form"],
        "goals_scored": form["goals_scored"],
        "goals_conceded": form["goals_conceded"],
        "recent_matches": form["matches"],
        "league_position": position,
        "total_teams": total,
        "league": ref,
    }


def fetch_h2h(home_name: str, away_name: str, limit: int = 5) -> List[Dict]:
    """Derive head-to-head history from both teams' own result lists.

    Flashscore has no H2H page for teams, but every result feed lists the
    opponent by id, so the intersection is the head-to-head record.
    """
    home = search_team(home_name)
    away = search_team(away_name)
    if not home or not away:
        return []

    home_events = events_from(
        fetch_team_page(home["id"], home["slug"]).get("results", []))
    away_id = away["id"]

    meetings = []
    for event in home_events:
        if event["home_score"] is None:
            continue
        if away_id not in (event["home_id"], event["away_id"]):
            continue
        meetings.append({
            "home": event["home"],
            "away": event["away"],
            "score": [event["home_score"], event["away_score"]],
            "ts": event["ts"],
        })

    meetings.sort(key=lambda m: m["ts"], reverse=True)
    return meetings[:limit]


def resolve_in_table(name: str, table: List[Dict]) -> Optional[Dict]:
    """Match a team name against a league table's member list.

    Safer than free-text search: both sides are in the same competition,
    so cross-league and cross-sport confusion is impossible.
    """
    wanted_gender = target_gender(name)
    target = _distinctive(name)
    best, best_score = None, 0.0

    for row in table:
        candidate = _distinctive(row["name"])
        score = len(target & candidate) / max(1, len(target | candidate))
        if candidate and candidate < target:
            score += 0.15
        if row["name"].lower() == name.lower():
            score = 2.0
        if score > best_score:
            best_score, best = score, row

    if not best or best_score < 0.3:
        return None
    return {"team_id": best["team_id"], "name": best["name"],
            "position": best["position"], "score": round(best_score, 3)}


def fetch_league_context(seed_name: str) -> Optional[Dict]:
    """Resolve a seed team, then return its league table.

    The table is the authoritative member list for the competition, so the
    other team in a fixture can be resolved without a second search.
    """
    seed = search_team(seed_name)
    if not seed:
        return None
    ref = table_ref(fetch_team_page(seed["id"], seed["slug"]))
    if not ref or not ref.get("season_id"):
        return None
    return {
        "seed": seed,
        "league": ref,
        "table": fetch_standings(ref["table_id"], ref["season_id"]),
    }


if __name__ == "__main__":
    import sys
    for team in (sys.argv[1:] or ["Kagoshima United"]):
        started = time.time()
        data = fetch_team_data(team)
        if not data:
            print(f"{team}: no match")
            continue
        print(f"{team} -> {data['name']} (id={data['id']}) "
              f"form={''.join(data['form'])} "
              f"gf={data['goals_scored']} ga={data['goals_conceded']} "
              f"pos={data['league_position']}/{data['total_teams']} "
              f"({time.time() - started:.1f}s)")
