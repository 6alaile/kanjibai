"""
flashscore_fixture_source.py — Flashscore fixtures as the source for form & results.

Why this exists
---------------
The per-team lookup (search -> team page -> "results" feed) had three defects:

1. *Stale last five.*  `flashscore_cache.get_recent_results()` reversed the whole
   stored match history and took the first five, i.e. the five **oldest** matches a
   team ever played on Flashscore (January 2026 for most teams), while the
   form/goals arrays came from the five newest — so the two were not even aligned.
2. *Unresolved teams.*  A club only appeared if the bookmaker's name matched a
   Flashscore name.  Women's clubs ("PSV Eindhoven Women" vs "PSV Eindhoven W") and
   lower-division sides ("FC Ingulets Petrove" vs "Inhulets") never resolved.
3. *No head-to-head.*  The per-fixture feed was parsed with a field convention that
   does not exist in the feed, so it always came back empty.

All three are avoided by working from the fixture instead of from a team search.
Flashscore's per-fixture feed `df_hh_1_<match_id>` carries, for both teams of a
fixture:

  * "Last matches: <team>" sections — every recent match with epoch, opponent,
    venue, score and result,
  * a "Head-to-head matches" section — the two teams' past meetings.

and the day's fixtures feed `f_1_0_3_en_1` carries every fixture with its league
path, kick-off epoch and both team ids.  Reaching a team through its fixture needs
no name search at all, and the kick-off epoch is authoritative (the bookmaker
prints 12-hour times without a meridiem marker).

Feed fields used here
---------------------
fixtures feed f_1_0_3_en_1
  AA=match id  AD=kick-off epoch  AE=home name  AF=away name  PX/PY=team ids
  ZL=league path  ZA=league name  ZY=country  ZE=table id  ZC=season id

per-fixture feed df_hh_1_<match_id>
  KB=section header ("Last matches: <team>" / "Head-to-head matches")
  KC=epoch  KP=match id  KF=competition  KH=country
  UO/UQ=the two participants of the row  KJ=UQ's name  KK=UO's name
  KL=score, always "<home goals>:<away goals>"  KS=venue of the section's team
  WIS=result of the section's team (w/d/l)

standings feed tl_<table id>_<season id>
  TN=team name  TI=team id  TR=position  TM=played  TW/TDR/TL=win/draw/loss  TP=points
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

FS_BASE = "https://www.flashscore.com"
FS_FEED = "https://global.flashscore.ninja/2/x/feed/{}"
FS_FIXTURES_FEED = FS_FEED.format("f_1_0_3_en_1")
FS_FSIGN = "SW9D1eZo"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

REQUEST_DELAY = 0.4

EAT = timezone(timedelta(hours=3))

# Club-type tokens that carry no identifying information.
GENERIC = {"fc", "sc", "cf", "ac", "af", "cd", "ec", "ss", "fzc", "fcp", "bk",
           "if", "ff", "nk", "fk", "sk", "ogc", "bsc", "vfl", "vfb", "tsg",
           "sd", "ud", "rc", "rcd", "us", "as", "ca", "ssc", "sv", "bs", "club",
           "cal", "ce", "de", "sad", "inter", "res", "rese", "reserves", "reserve",
           "b", "u19", "u20", "u21", "u23", "ii"}

# Bookmakers spell women's clubs out; Flashscore shortens them to "<Name> W".
WOMEN_TOKENS = {"women", "woman", "ladies", "lady", "femenina", "femenino",
                "feminine", "womens", "damen", "weiblich"}

# League "countries" that really mean "not a single country".
INTERNATIONAL = {"international", "world", "europe", "africa", "asia",
                 "south-america", "north-america", "north-central-america",
                 "oceania", "antarctica"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": FS_BASE + "/",
})


# ── feed decoding ────────────────────────────────────────────────────────────

def parse_feed(raw: str) -> List[Dict[str, str]]:
    """Decode a Flashscore feed into a list of field dicts."""
    rows: List[Dict[str, str]] = []
    for row in raw.split("~"):
        if not row.strip():
            continue
        fields: Dict[str, str] = {}
        for part in row.split("\xac"):
            if "\xf7" not in part:
                continue
            code, _, value = part.partition("\xf7")
            fields[code] = value
        if fields:
            rows.append(fields)
    return rows


def _get(url: str, fsign: bool = False, timeout: int = 30) -> str:
    headers = {"x-fsign": FS_FSIGN} if fsign else None
    response = SESSION.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


# ── name matching ───────────────────────────────────────────────────────────

def _tokens(name: str) -> set:
    """Lowercase alphanumeric tokens, with women's markers folded onto "w"."""
    out = set()
    for token in re.findall(r"[a-z0-9]+", (name or "").lower()):
        out.add("w" if token in WOMEN_TOKENS else token)
    return out


def _distinctive(name: str) -> set:
    """Tokens that actually identify a club."""
    return {t for t in _tokens(name) if t not in GENERIC}


def name_similarity(a: str, b: str) -> float:
    """0..1 similarity between a bookmaker name and a Flashscore name.

    Token overlap on the distinctive tokens, plus a character-level ratio so
    "Ingulets" still matches "Inhulets" (one letter apart).
    """
    if not a or not b:
        return 0.0
    ta, tb = _distinctive(a), _distinctive(b)
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb) / len(ta | tb)
    chars = SequenceMatcher(None, re.sub(r"[^a-z0-9]", "", a.lower()),
                            re.sub(r"[^a-z0-9]", "", b.lower())).ratio()
    return max(overlap, chars * 0.9)


def league_country(league: str) -> str:
    """Country of a league string ("Football / Spain / LaLiga 2" -> spain)."""
    parts = [p.strip().lower() for p in (league or "").split("/") if p.strip()]
    return parts[1] if len(parts) > 1 else ""


def book_kickoff_epoch(date_str: str, hhmm: str) -> Optional[int]:
    """Epoch for a bookmaker kick-off time on the scan date.

    The bookmaker's times are 24-hour EAT wall clock (the scraper converts the
    "1:00 pm" it reads), and Flashscore's epochs are UTC.
    """
    try:
        hour, minute = (int(x) for x in str(hhmm).strip().split(":")[:2])
        day = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    local = day.replace(hour=hour, minute=minute)
    return int(local.replace(tzinfo=EAT).timestamp())


# ── the day's fixtures ──────────────────────────────────────────────────────

def fetch_day_fixtures() -> List[Dict]:
    """Every soccer fixture Flashscore currently lists, with its league.

    Each fixture: {id, ts, home, away, home_id, away_id, league: {path, name,
    country, table_id, season_id}}.  League rows precede their matches, so the
    league of a match is the last league row seen.
    """
    rows = parse_feed(_get(FS_FIXTURES_FEED, fsign=True))

    fixtures: List[Dict] = []
    league: Optional[Dict] = None
    for row in rows:
        if row.get("ZL"):
            league = {
                "path": row["ZL"],
                "name": row.get("ZA"),
                "country": row.get("ZY"),
                "table_id": row.get("ZE"),
                "season_id": row.get("ZC"),
            }
            continue
        if not row.get("AA") or not row.get("AD") or league is None:
            continue
        fixtures.append({
            "id": row["AA"],
            "ts": int(row["AD"]),
            "home": row.get("AE"),
            "away": row.get("AF"),
            "home_id": row.get("PX"),
            "away_id": row.get("PY"),
            "league": league,
        })
    return fixtures


def match_fixtures(book_fixtures: List[Dict], fs_fixtures: List[Dict],
                   date_str: str, window_seconds: int = 4 * 3600) -> Dict[str, Dict]:
    """Match bookmaker fixtures to Flashscore fixtures, keyed by bookmaker id.

    A pair only matches when both team names are similar *and* the kick-off times
    line up, so a same-named pair in another league or on another day cannot steal
    the match.

    The first pass uses a tight window.  A second, looser pass catches fixtures
    whose kick-off moved, or a bookmaker clock that disagrees by half a day.
    """
    matches: Dict[str, Dict] = {}

    for book in book_fixtures:
        book_country = league_country(book.get("league", ""))
        book_ts = book_kickoff_epoch(date_str, book.get("time", ""))
        found = None

        for window, min_score in ((window_seconds, 0.5),
                                  (window_seconds + 12 * 3600, 0.6)):
            best, best_score = None, 0.0
            for fs in fs_fixtures:
                fs_country = league_country(fs["league"]["path"])
                if (book_country and fs_country
                        and book_country not in INTERNATIONAL
                        and fs_country not in INTERNATIONAL
                        and book_country != fs_country):
                    continue

                home_sim = name_similarity(book.get("home", ""), fs["home"])
                away_sim = name_similarity(book.get("away", ""), fs["away"])
                score = min(home_sim, away_sim)
                if score < 0.45:
                    continue

                if book_ts:
                    delta = abs(book_ts - fs["ts"])
                    if delta > window:
                        continue
                    score += 0.2 * (1 - delta / window)

                if score > best_score:
                    best_score, best = score, fs

            if best and best_score >= min_score:
                found = best
                break

        if found:
            matches[book["id"]] = found

    return matches


# ── per-fixture detail ──────────────────────────────────────────────────────

def _sections(rows: List[Dict[str, str]]) -> List[tuple]:
    """Split the feed into (title, rows) sections.

    A "KB" field opens a section ("Last matches: <team>", "Head-to-head matches");
    "KA" rows are the tab headers and carry no match data.
    """
    sections: List[tuple] = []
    title: Optional[str] = None
    buf: List[Dict[str, str]] = []
    for row in rows:
        if "KB" in row:
            if title is not None:
                sections.append((title, buf))
            title, buf = row["KB"], []
            continue
        if "KC" in row and "KL" in row:
            buf.append(row)
    if title is not None:
        sections.append((title, buf))
    return sections


def _focus_id(section_rows: List[Dict[str, str]], home_id: str,
              away_id: str) -> Optional[str]:
    """Which of the fixture's teams a "Last matches" section belongs to.

    Every row of the section involves that team, so the team id common to all
    rows is the section's team.
    """
    if not section_rows:
        return None
    common = None
    for row in section_rows:
        ids = {row.get("UO"), row.get("UQ")} - {None}
        common = ids if common is None else (common & ids)
        if not common:
            break
    for team_id in (home_id, away_id):
        if team_id and common and team_id in common:
            return team_id
    return None


def _score(row: Dict[str, str]) -> Optional[tuple]:
    """The row's score as absolute (home goals, away goals)."""
    raw = row.get("KL", "")
    if ":" not in raw:
        return None
    try:
        home_goals, away_goals = (int(x) for x in raw.split(":"))
    except ValueError:
        return None
    return home_goals, away_goals


def fetch_match_detail(match_id: str, home_id: str, home_name: str,
                       away_id: str, away_name: str) -> Dict:
    """Last matches for both teams plus their head-to-head history.

    Returns {"home": {...}, "away": {...}, "h2h": [...]} where each team entry is
    {"form": [...], "goals_scored": [...], "goals_conceded": [...],
      "results": [{ts, match_id, opponent, opponent_id, home_away, score}]}.
    The results are newest first; the caller reverses them if it wants oldest first.
    """
    empty = {"form": [], "goals_scored": [], "goals_conceded": [], "results": []}
    try:
        raw = _get(FS_FEED.format(f"df_hh_1_{match_id}"), fsign=True, timeout=15)
    except Exception as exc:                            # noqa: BLE001
        log.warning("  fixture feed failed for %s: %s", match_id, exc)
        return {"home": dict(empty), "away": dict(empty), "h2h": []}

    sides = {home_id: "home", away_id: "away"}
    results: Dict[str, Dict[str, Dict]] = {"home": {}, "away": {}}
    venues: Dict[str, Dict[str, str]] = {}     # match id -> {team id: home/away}
    meetings: Dict[str, Dict[str, str]] = {}   # match id -> feed row

    for title, section_rows in _sections(parse_feed(raw)):
        if title.startswith("Head-to-head"):
            for row in section_rows:
                if {row.get("UO"), row.get("UQ")} != {home_id, away_id}:
                    continue
                mid = row.get("KP") or ""
                meetings.setdefault(mid, row)
            continue

        focus_id = _focus_id(section_rows, home_id, away_id)
        if focus_id not in sides:
            continue
        side = sides[focus_id]

        for row in section_rows:
            uo, uq = row.get("UO"), row.get("UQ")
            if focus_id not in (uo, uq):
                continue
            venue = row.get("KS")
            if venue not in ("home", "away"):
                continue
            goals = _score(row)
            if not goals:
                continue
            home_goals, away_goals = goals

            opponent_id = uq if focus_id == uo else uo
            # KJ names UQ, KK names UO.
            opponent_name = (row.get("KJ") if focus_id == uo else row.get("KK")) or ""
            opponent_name = opponent_name.lstrip("*")

            mid = row.get("KP") or ""
            if mid in results[side]:
                continue
            if venue == "home":
                scored, conceded = home_goals, away_goals
            else:
                scored, conceded = away_goals, home_goals

            results[side][mid] = {
                "ts": int(row.get("KC", 0) or 0),
                "match_id": mid,
                "opponent": opponent_name,
                "opponent_id": opponent_id,
                "home_away": "H" if venue == "home" else "A",
                "score": [scored, conceded],
            }
            venues.setdefault(mid, {})[focus_id] = venue
            venues[mid][opponent_id] = "away" if venue == "home" else "home"

    detail: Dict = {"h2h": []}
    for side in ("home", "away"):
        ordered = sorted(results[side].values(), key=lambda r: r["ts"], reverse=True)[:5]
        detail[side] = {
            "form": [_result(r) for r in ordered],
            "goals_scored": [r["score"][0] for r in ordered],
            "goals_conceded": [r["score"][1] for r in ordered],
            "results": ordered,
        }

    for mid, row in meetings.items():
        goals = _score(row)
        if not goals:
            continue
        home_goals, away_goals = goals
        # The meeting rows carry no venue of their own; it comes from the same
        # match's row in the teams' "Last matches" sections.
        hosts = [tid for tid, venue in venues.get(mid, {}).items() if venue == "home"]
        if len(hosts) != 1:
            continue
        host = hosts[0]
        # The row's score is host-first (match home = host), so it stays as-is;
        # only the displayed sides change with the host.
        if host == home_id:
            meeting = {"home": home_name, "away": away_name,
                       "score": [home_goals, away_goals], "ts": int(row.get("KC", 0) or 0)}
        else:
            meeting = {"home": away_name, "away": home_name,
                       "score": [home_goals, away_goals], "ts": int(row.get("KC", 0) or 0)}
        detail["h2h"].append(meeting)

    detail["h2h"].sort(key=lambda m: m["ts"], reverse=True)
    detail["h2h"] = detail["h2h"][:5]
    return detail


def _result(result: Dict) -> str:
    scored, conceded = result["score"]
    return "W" if scored > conceded else ("L" if scored < conceded else "D")


# ── standings ───────────────────────────────────────────────────────────────

def fetch_table(table_id: str, season_id: str) -> Dict[str, Dict]:
    """League table keyed by team id: position, played, wins, draws, losses, points."""
    if not table_id or not season_id:
        return {}
    rows = parse_feed(_get(FS_FEED.format(f"tl_{table_id}_{season_id}"), fsign=True))
    table: Dict[str, Dict] = {}
    for row in rows:
        if not row.get("TN") or not row.get("TI") or not row.get("TR"):
            continue
        table[row["TI"]] = {
            "name": row["TN"],
            "position": int(row["TR"]),
            "played": int(row.get("TM", 0) or 0),
            "wins": int(row.get("TW", 0) or 0),
            "draws": int(row.get("TDR", 0) or 0),
            "losses": int(row.get("TL", 0) or 0),
            "points": int(row.get("TP", 0) or 0),
        }
    return table


if __name__ == "__main__":
    import json
    import sys
    import time

    fixtures = fetch_day_fixtures()
    print(f"fixtures: {len(fixtures)}")
    for fs in sorted(fixtures, key=lambda f: f["ts"])[:10]:
        when = time.strftime("%m-%d %H:%M", time.gmtime(fs["ts"]))
        print(f"  {when}Z  {fs['home']} vs {fs['away']}  [{fs['league']['name']}]")

    target = sys.argv[1] if len(sys.argv) > 1 else None
    for fs in fixtures:
        if target and target.lower() not in (fs["home"] + fs["away"]).lower():
            continue
        detail = fetch_match_detail(fs["id"], fs["home_id"], fs["home"],
                                    fs["away_id"], fs["away"])
        print(f"\n{fs['home']} vs {fs['away']} ({fs['id']})")
        for side in ("home", "away"):
            print(f"  {side}: {''.join(detail[side]['form'])} "
                  f"gf={detail[side]['goals_scored']} ga={detail[side]['goals_conceded']}")
            for r in detail[side]["results"]:
                print(f"    {time.strftime('%Y-%m-%d', time.gmtime(r['ts']))} "
                      f"{r['home_away']} {r['opponent']} {r['score'][0]}:{r['score'][1]}")
        for m in detail["h2h"]:
            print(f"  h2h {time.strftime('%Y-%m-%d', time.gmtime(m['ts']))} "
                  f"{m['home']} {m['score'][0]}:{m['score'][1]} {m['away']}")
        break
