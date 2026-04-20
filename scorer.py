"""
scorer.py — Scout Signal Engine
Applies filters, analyses stats, scores matches, generates bet recommendations.
No external dependencies. Pure logic. Fully testable in isolation.
"""

from dataclasses import dataclass, field
from typing import Optional
import json


# ─── DATA STRUCTURES ────────────────────────────────────────────────────────

@dataclass
class TeamStats:
    name: str
    form: list[str]           # e.g. ["W","W","D","L","W"] — most recent last
    goals_scored: list[int]   # goals scored in each of last N matches
    goals_conceded: list[int] # goals conceded in each of last N matches
    league_position: Optional[int] = None
    total_teams_in_league: Optional[int] = None
    opponent_positions: Optional[list[int]] = None  # position of each recent opponent


@dataclass
class H2HRecord:
    matches: list[dict]  # each: { "home": str, "away": str, "score": [int,int] }
    # e.g. [{"home":"Arsenal","away":"Wolves","score":[3,1]}, ...]


@dataclass
class MatchOdds:
    home: float
    draw: float
    away: float
    over_15: Optional[float] = None
    over_25: Optional[float] = None
    over_35: Optional[float] = None
    btts_yes: Optional[float] = None


@dataclass
class FilterConfig:
    home_odds_max: float = 2.00
    away_odds_min: float = 5.00
    min_form_matches: int = 5
    league_position_weight: float = 0.5
    opp_strength_weight: float = 0.5
    markets: list[str] = field(default_factory=lambda: ["1x2", "ou", "btts", "combo"])


@dataclass
class BetSignal:
    label: str       # e.g. "Home Win", "Over 1.5", "1 + Over 1.5"
    type: str        # "primary" | "secondary" | "combo" | "warn"
    confidence: int  # 0–100


@dataclass
class MatchResult:
    match_id: str
    home: str
    away: str
    league: str
    time: str
    odds: MatchOdds
    bets: list[BetSignal]
    confidence: int
    low_confidence: bool
    low_conf_team: Optional[str]
    home_stats: TeamStats
    away_stats: TeamStats
    h2h: Optional[H2HRecord]
    signals: list[dict]   # [{"text": str, "type": str}]
    home_pos: Optional[int]
    away_pos: Optional[int]
    h2h_summary: str
    recommendation: str   # "1" | "X" | "2"
    passes_filter: bool


# ─── FILTER ─────────────────────────────────────────────────────────────────

def passes_odds_filter(odds: MatchOdds, cfg: FilterConfig) -> bool:
    """Entry gate: home must be short, away must be long."""
    return odds.home <= cfg.home_odds_max and odds.away >= cfg.away_odds_min


# ─── CONFIDENCE HELPERS ─────────────────────────────────────────────────────

def form_confidence(stats: TeamStats, cfg: FilterConfig) -> tuple[bool, str]:
    """
    Returns (is_low_confidence, reason).
    Low confidence if fewer matches than threshold.
    """
    n = len(stats.form)
    if n < cfg.min_form_matches:
        return True, f"Only {n} form matches available (min {cfg.min_form_matches})"
    return False, ""


def safe_avg(values: list[int | float], fallback: float = 0.0) -> float:
    if not values:
        return fallback
    return sum(values) / len(values)


# ─── FORM ANALYSIS ──────────────────────────────────────────────────────────

def form_score(form: list[str]) -> float:
    """
    Convert form list to a 0–1 score.
    W=1, D=0.4, L=0
    Recent matches weighted more heavily.
    """
    if not form:
        return 0.0
    weights = [1 + (i * 0.2) for i in range(len(form))]  # more recent = higher weight
    total_weight = sum(weights)
    score_map = {"W": 1.0, "D": 0.4, "L": 0.0}
    weighted = sum(score_map.get(r, 0) * w for r, w in zip(form, weights))
    return weighted / total_weight


def goals_tendency(scored: list[int], conceded: list[int]) -> dict:
    """Analyse goals patterns."""
    n = len(scored)
    avg_scored = safe_avg(scored)
    avg_conceded = safe_avg(conceded)
    scored_2plus = sum(1 for g in scored if g >= 2) / n if n else 0
    conceded_1plus = sum(1 for g in conceded if g >= 1) / n if n else 0
    clean_sheets = sum(1 for g in conceded if g == 0) / n if n else 0
    over_15_rate = sum(1 for s, c in zip(scored, conceded) if s + c > 1) / n if n else 0
    over_25_rate = sum(1 for s, c in zip(scored, conceded) if s + c > 2) / n if n else 0
    btts_rate = sum(1 for s, c in zip(scored, conceded) if s > 0 and c > 0) / n if n else 0

    return {
        "avg_scored": round(avg_scored, 2),
        "avg_conceded": round(avg_conceded, 2),
        "scored_2plus_rate": round(scored_2plus, 2),
        "conceded_1plus_rate": round(conceded_1plus, 2),
        "clean_sheet_rate": round(clean_sheets, 2),
        "over_15_rate": round(over_15_rate, 2),
        "over_25_rate": round(over_25_rate, 2),
        "btts_rate": round(btts_rate, 2),
    }


# ─── H2H ANALYSIS ───────────────────────────────────────────────────────────

def analyse_h2h(h2h: Optional[H2HRecord], home_team: str) -> dict:
    """
    Returns h2h stats from home team perspective.
    """
    if not h2h or not h2h.matches:
        return {
            "available": False,
            "home_win_rate": None,
            "avg_total_goals": None,
            "over_15_rate": None,
            "over_25_rate": None,
            "btts_rate": None,
            "summary": "No H2H data"
        }

    total = len(h2h.matches)
    home_wins = 0
    total_goals_list = []
    btts_count = 0

    for m in h2h.matches:
        hg, ag = m["score"]
        total_g = hg + ag
        total_goals_list.append(total_g)
        if m["home"] == home_team and hg > ag:
            home_wins += 1
        elif m["away"] == home_team and ag > hg:
            home_wins += 1
        if hg > 0 and ag > 0:
            btts_count += 1

    avg_goals = safe_avg(total_goals_list)
    over_15_rate = sum(1 for g in total_goals_list if g > 1) / total
    over_25_rate = sum(1 for g in total_goals_list if g > 2) / total
    home_win_rate = home_wins / total
    btts_rate = btts_count / total

    # Summary string
    wins = home_wins
    summary = f"{home_team} W{wins} of last {total}"

    return {
        "available": True,
        "home_win_rate": round(home_win_rate, 2),
        "avg_total_goals": round(avg_goals, 2),
        "over_15_rate": round(over_15_rate, 2),
        "over_25_rate": round(over_25_rate, 2),
        "btts_rate": round(btts_rate, 2),
        "summary": summary
    }


# ─── LEAGUE POSITION WEIGHTING ──────────────────────────────────────────────

def position_advantage_score(
    home_pos: Optional[int],
    away_pos: Optional[int],
    total_teams: Optional[int],
    weight: float
) -> float:
    """
    Returns 0–1 score for positional advantage.
    Higher = home team has a bigger table gap advantage.
    """
    if not home_pos or not away_pos or not total_teams or weight == 0:
        return 0.5  # neutral if no data

    # Normalise: lower position number = better
    home_norm = 1 - ((home_pos - 1) / (total_teams - 1))
    away_norm = 1 - ((away_pos - 1) / (total_teams - 1))
    gap = home_norm - away_norm  # positive = home team higher

    # Scale gap to 0–1 range and apply weight
    raw = (gap + 1) / 2  # shift from [-1,1] to [0,1]
    return round(0.5 + (raw - 0.5) * weight, 3)


# ─── OPPONENT STRENGTH ADJUSTMENT ───────────────────────────────────────────

def opp_strength_adjustment(
    team_stats: TeamStats,
    weight: float
) -> float:
    """
    Adjust form score based on quality of recent opponents.
    If a team's recent wins came against weak sides, discount.
    Returns multiplier 0.7–1.3.
    """
    if not team_stats.opponent_positions or weight == 0:
        return 1.0

    total = team_stats.total_teams_in_league or 20
    avg_opp_pos = safe_avg(team_stats.opponent_positions)
    # Normalise: avg_opp_pos closer to 1 = played strong opponents
    opp_strength = 1 - ((avg_opp_pos - 1) / (total - 1))  # 0=weak, 1=strong

    # Adjustment: playing strong opponents inflates value of wins
    # Playing weak opponents deflates value
    adjustment = 1.0 + (opp_strength - 0.5) * weight * 0.6
    return round(max(0.7, min(1.3, adjustment)), 3)


# ─── SIGNAL GENERATION ──────────────────────────────────────────────────────

def generate_signals(
    home: TeamStats,
    away: TeamStats,
    home_goals: dict,
    away_goals: dict,
    h2h_data: dict,
    pos_score: float,
    cfg: FilterConfig
) -> list[dict]:
    """Generate human-readable signals for the reasoning panel."""
    signals = []

    def sig(text, type_=""):
        signals.append({"text": text, "type": type_})

    # Position gap
    if home.league_position and away.league_position:
        gap = away.league_position - home.league_position
        if gap >= 10:
            sig(f"Large table gap: #{home.league_position} vs #{away.league_position} ({gap} places)")
        elif gap >= 5:
            sig(f"Table gap: #{home.league_position} vs #{away.league_position} ({gap} places)")

    # Home form
    recent_form = home.form[-5:] if len(home.form) >= 5 else home.form
    wins = recent_form.count("W")
    if wins >= 4:
        sig(f"{home.name} in excellent form — {wins} wins in last {len(recent_form)}")
    elif wins >= 3:
        sig(f"{home.name} solid form — {wins} wins in last {len(recent_form)}")

    # Away form
    away_recent = away.form[-5:] if len(away.form) >= 5 else away.form
    away_wins = away_recent.count("W")
    away_losses = away_recent.count("L")
    if away_losses >= 3:
        sig(f"{away.name} struggling — {away_losses} losses in last {len(away_recent)}", "warn")

    # Goals — Over/Under signals
    if home_goals["over_15_rate"] >= 0.8:
        sig(f"{home.name}: Over 1.5 in {int(home_goals['over_15_rate']*100)}% of recent matches", "blue")
    if home_goals["over_25_rate"] >= 0.6:
        sig(f"{home.name}: Over 2.5 in {int(home_goals['over_25_rate']*100)}% of recent matches", "blue")
    if away_goals["conceded_1plus_rate"] >= 0.8:
        sig(f"{away.name} conceded in {int(away_goals['conceded_1plus_rate']*100)}% of away games", "blue")

    # BTTS
    combined_btts = (home_goals["btts_rate"] + away_goals["btts_rate"]) / 2
    if combined_btts >= 0.65:
        sig(f"BTTS likely — combined BTTS rate {int(combined_btts*100)}%", "blue")
    elif combined_btts <= 0.35:
        sig(f"BTTS unlikely — combined BTTS rate only {int(combined_btts*100)}%", "warn")

    # H2H
    if h2h_data["available"]:
        if h2h_data["home_win_rate"] >= 0.7:
            sig(f"H2H: {h2h_data['summary']}", "blue")
        elif h2h_data["home_win_rate"] <= 0.3:
            sig(f"H2H: Home team historically weak in this fixture", "warn")
        if h2h_data["over_25_rate"] >= 0.7:
            sig(f"H2H: Over 2.5 goals in {int(h2h_data['over_25_rate']*100)}% of meetings", "blue")

    # Opponent strength note
    if home.opponent_positions and cfg.opp_strength_weight > 0:
        total = home.total_teams_in_league or 20
        avg = safe_avg(home.opponent_positions)
        if avg <= total * 0.35:
            sig(f"{home.name}'s recent form came vs top-half opposition — quality confirmed")
        elif avg >= total * 0.65:
            sig(f"{home.name}'s recent wins came vs lower-half sides — adjust expectations", "warn")

    return signals


# ─── BET RECOMMENDATION ─────────────────────────────────────────────────────

def recommend_bets(
    home_goals: dict,
    away_goals: dict,
    h2h_data: dict,
    home_form_score: float,
    pos_score: float,
    cfg: FilterConfig
) -> list[BetSignal]:
    """Determine which bets to recommend based on signal strength."""
    bets = []

    # 1X2 — always recommend home win (passed odds filter)
    bets.append(BetSignal(label="Home Win", type="primary", confidence=int(home_form_score * 100)))

    # Over/Under
    # Combine home scoring rate, away conceding rate, H2H
    ou15_signals = [
        home_goals["over_15_rate"],
        away_goals["conceded_1plus_rate"],
        h2h_data.get("over_15_rate") or 0.5
    ]
    ou15_score = safe_avg(ou15_signals)

    ou25_signals = [
        home_goals["over_25_rate"],
        (home_goals["avg_scored"] + away_goals["avg_conceded"]) / 4,
        h2h_data.get("over_25_rate") or 0.5
    ]
    ou25_score = safe_avg(ou25_signals)

    if "ou" in cfg.markets:
        if ou25_score >= 0.60:
            bets.append(BetSignal(label="Over 2.5", type="secondary", confidence=int(ou25_score * 100)))
        elif ou15_score >= 0.70:
            bets.append(BetSignal(label="Over 1.5", type="secondary", confidence=int(ou15_score * 100)))

    # BTTS
    btts_score = (
        home_goals["btts_rate"] * 0.4 +
        away_goals["btts_rate"] * 0.4 +
        (h2h_data.get("btts_rate") or 0.5) * 0.2
    )
    if "btts" in cfg.markets and btts_score >= 0.60:
        bets.append(BetSignal(label="BTTS Yes", type="secondary", confidence=int(btts_score * 100)))

    # Combos — only suggest when both legs are strong independently
    if "combo" in cfg.markets:
        # 1 + Over 1.5
        if ou15_score >= 0.72 and home_form_score >= 0.65:
            combo_conf = int((ou15_score + home_form_score) / 2 * 100)
            bets.append(BetSignal(label="1 + Over 1.5", type="combo", confidence=combo_conf))
        # 1 + Over 2.5
        if ou25_score >= 0.65 and home_form_score >= 0.70:
            combo_conf = int((ou25_score + home_form_score) / 2 * 100)
            bets.append(BetSignal(label="1 + Over 2.5", type="combo", confidence=combo_conf))
        # 1 + BTTS
        if btts_score >= 0.65 and home_form_score >= 0.65:
            combo_conf = int((btts_score + home_form_score) / 2 * 100)
            bets.append(BetSignal(label="1 + BTTS", type="combo", confidence=combo_conf))

    return bets


# ─── TEAM NOTES (for UI reasoning panel) ────────────────────────────────────

def build_team_notes(name: str, goals: dict) -> list[str]:
    notes = []
    if goals["avg_scored"] >= 2.0:
        notes.append(f"Averaging {goals['avg_scored']} goals scored per game")
    elif goals["avg_scored"] >= 1.0:
        notes.append(f"Scoring {goals['avg_scored']} goals per game on average")
    else:
        notes.append(f"Low scoring — avg {goals['avg_scored']} per game")

    if goals["clean_sheet_rate"] >= 0.4:
        notes.append(f"Clean sheet in {int(goals['clean_sheet_rate']*100)}% of recent games")
    if goals["conceded_1plus_rate"] >= 0.8:
        notes.append(f"Conceded in {int(goals['conceded_1plus_rate']*100)}% of recent games")
    if goals["scored_2plus_rate"] >= 0.6:
        notes.append(f"Scored 2+ in {int(goals['scored_2plus_rate']*100)}% of recent games")

    return notes


# ─── MAIN SCORER ────────────────────────────────────────────────────────────

def score_match(
    match_id: str,
    home: TeamStats,
    away: TeamStats,
    odds: MatchOdds,
    league: str,
    time: str,
    cfg: FilterConfig,
    h2h: Optional[H2HRecord] = None,
    total_teams_in_league: int = 20
) -> MatchResult:
    """
    Full pipeline for a single match.
    Returns a MatchResult with all signals, bets, and confidence.
    """

    # 1. Odds filter
    passes = passes_odds_filter(odds, cfg)

    # 2. Confidence check
    home_low_conf, home_conf_reason = form_confidence(home, cfg)
    away_low_conf, away_conf_reason = form_confidence(away, cfg)
    low_confidence = home_low_conf or away_low_conf
    low_conf_team = None
    if home_low_conf:
        low_conf_team = home.name
    elif away_low_conf:
        low_conf_team = away.name

    # 3. Goals tendency
    home_goals = goals_tendency(home.goals_scored, home.goals_conceded)
    away_goals = goals_tendency(away.goals_scored, away.goals_conceded)

    # 4. H2H
    h2h_data = analyse_h2h(h2h, home.name)

    # 5. Form scores
    home_fs = form_score(home.form)
    away_fs = form_score(away.form)

    # 6. Opponent strength adjustment
    home_opp_adj = opp_strength_adjustment(home, cfg.opp_strength_weight)
    away_opp_adj = opp_strength_adjustment(away, cfg.opp_strength_weight)
    home_fs_adj = min(1.0, home_fs * home_opp_adj)
    away_fs_adj = min(1.0, away_fs * away_opp_adj)

    # 7. Position advantage
    pos_score = position_advantage_score(
        home.league_position,
        away.league_position,
        total_teams_in_league,
        cfg.league_position_weight
    )

    # 8. Composite confidence
    # Weights: form 40%, position 20%, h2h 20%, goals tendency 20%
    h2h_score = h2h_data["home_win_rate"] if h2h_data["available"] else 0.55
    goals_signal = min(1.0, (home_goals["avg_scored"] / 3 + (1 - away_goals["clean_sheet_rate"])) / 2)

    raw_conf = (
        home_fs_adj * 0.40 +
        pos_score * 0.20 +
        h2h_score * 0.20 +
        goals_signal * 0.20
    )

    # Penalise low confidence
    if low_confidence:
        raw_conf *= 0.85

    confidence = max(30, min(95, int(raw_conf * 100)))

    # 9. Bet recommendations
    bets = recommend_bets(home_goals, away_goals, h2h_data, home_fs_adj, pos_score, cfg)

    # 10. Signals
    signals = generate_signals(home, away, home_goals, away_goals, h2h_data, pos_score, cfg)

    # 11. Team notes for UI
    home.goals_scored = home.goals_scored  # already set
    home_notes = build_team_notes(home.name, home_goals)
    away_notes = build_team_notes(away.name, away_goals)

    # Attach notes to stats for output
    home_with_notes = {
        "name": home.name,
        "form": home.form[-5:] if len(home.form) >= 5 else home.form,
        "notes": home_notes
    }
    away_with_notes = {
        "name": away.name,
        "form": away.form[-5:] if len(away.form) >= 5 else away.form,
        "notes": away_notes
    }

    return MatchResult(
        match_id=match_id,
        home=home.name,
        away=away.name,
        league=league,
        time=time,
        odds=odds,
        bets=bets,
        confidence=confidence,
        low_confidence=low_confidence,
        low_conf_team=low_conf_team,
        home_stats=home_with_notes,
        away_stats=away_with_notes,
        h2h=h2h,
        signals=signals,
        home_pos=home.league_position,
        away_pos=away.league_position,
        h2h_summary=h2h_data["summary"],
        recommendation="1",
        passes_filter=passes
    )


def score_all_matches(raw_matches: list[dict], cfg: FilterConfig) -> list[dict]:
    """
    Entry point. Takes list of raw match dicts (from scraper),
    returns list of scored match dicts ready for results.json.
    Excludes matches that don't pass the odds filter.
    """
    results = []

    for m in raw_matches:
        try:
            odds = MatchOdds(**m["odds"])

            if not passes_odds_filter(odds, cfg):
                continue

            home_stats = TeamStats(**m["home_stats"])
            away_stats = TeamStats(**m["away_stats"])

            h2h = None
            if m.get("h2h"):
                h2h = H2HRecord(matches=m["h2h"].get("matches", []))

            result = score_match(
                match_id=m["id"],
                home=home_stats,
                away=away_stats,
                odds=odds,
                league=m["league"],
                time=m["time"],
                cfg=cfg,
                h2h=h2h,
                total_teams_in_league=m.get("total_teams_in_league", 20)
            )

            results.append({
                "id": result.match_id,
                "time": result.time,
                "league": result.league,
                "home": result.home,
                "away": result.away,
                "homePos": result.home_pos,
                "awayPos": result.away_pos,
                "h2hSummary": result.h2h_summary,
                "odds": {
                    "home": result.odds.home,
                    "draw": result.odds.draw,
                    "away": result.odds.away
                },
                "confidence": result.confidence,
                "lowConfidence": result.low_confidence,
                "lowConfTeam": result.low_conf_team,
                "recommendation": result.recommendation,
                "bets": [{"label": b.label, "type": b.type} for b in result.bets],
                "homeStats": result.home_stats,
                "awayStats": result.away_stats,
                "signals": result.signals
            })

        except Exception as e:
            print(f"[scorer] Error processing match {m.get('id','?')}: {e}")
            continue

    # Sort by confidence descending
    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results


# ─── CLI TEST ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = FilterConfig(
        home_odds_max=2.00,
        away_odds_min=5.00,
        min_form_matches=5,
        league_position_weight=0.5,
        opp_strength_weight=0.5,
        markets=["1x2", "ou", "btts", "combo"]
    )

    # Sample match — Arsenal vs Wolves
    sample = [{
        "id": "test_001",
        "league": "Premier League",
        "time": "13:30",
        "total_teams_in_league": 20,
        "odds": {
            "home": 1.55, "draw": 4.20, "away": 6.50,
            "over_15": 1.25, "over_25": 1.70, "btts_yes": 1.85
        },
        "home_stats": {
            "name": "Arsenal",
            "form": ["W","D","W","W","W"],
            "goals_scored": [2,1,3,2,2],
            "goals_conceded": [0,1,1,0,1],
            "league_position": 2,
            "total_teams_in_league": 20,
            "opponent_positions": [5,8,3,12,6]
        },
        "away_stats": {
            "name": "Wolves",
            "form": ["L","D","L","W","L"],
            "goals_scored": [0,1,1,2,0],
            "goals_conceded": [2,1,2,1,3],
            "league_position": 16,
            "total_teams_in_league": 20,
            "opponent_positions": [4,7,9,2,14]
        },
        "h2h": {
            "matches": [
                {"home":"Arsenal","away":"Wolves","score":[3,1]},
                {"home":"Wolves","away":"Arsenal","score":[0,2]},
                {"home":"Arsenal","away":"Wolves","score":[2,0]},
                {"home":"Wolves","away":"Arsenal","score":[1,2]},
                {"home":"Arsenal","away":"Wolves","score":[2,1]},
            ]
        }
    }]

    results = score_all_matches(sample, cfg)
    print(json.dumps(results, indent=2))
    print(f"\n✓ {len(results)} match(es) passed filter and scored")
