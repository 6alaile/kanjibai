"""
scorer.py — Scout Signal Engine v2
Markets: 1X2, Double Chance, Over 1.5, BTTS, Home/Away to Score, BTTS + Over 2.5
All stats based on last 5 games only.
"""

from dataclasses import dataclass, field
from typing import Optional
import json

N = 5

@dataclass
class TeamStats:
    name: str
    form: list
    goals_scored: list
    goals_conceded: list
    league_position: Optional[int] = None
    total_teams_in_league: Optional[int] = None
    opponent_positions: Optional[list] = None

@dataclass
class H2HRecord:
    matches: list

@dataclass
class MatchOdds:
    home: float
    draw: float
    away: float
    over_15: Optional[float] = None
    over_25: Optional[float] = None
    btts_yes: Optional[float] = None

@dataclass
class FilterConfig:
    home_odds_max: float = 2.00
    away_odds_min: float = 5.00
    min_form_matches: int = 5
    league_position_weight: float = 0.5
    opp_strength_weight: float = 0.5
    markets: list = field(default_factory=lambda: ["1x2","dc","over15","btts","to_score","btts_over25","combo"])

@dataclass
class BetSignal:
    label: str
    type: str
    confidence: int

def cap5(lst):
    return lst[-N:] if len(lst) > N else lst

def safe_avg(values, fallback=0.0):
    return sum(values) / len(values) if values else fallback

def passes_odds_filter(odds, cfg):
    if odds.home < cfg.home_odds_max and odds.away >= cfg.away_odds_min:
        return True, "home"
    if odds.away < cfg.home_odds_max and odds.home >= cfg.away_odds_min:
        return True, "away"
    return False, ""

def form_confidence(stats, cfg):
    n = len(stats.form)
    if n < cfg.min_form_matches:
        return True, f"Only {n} matches available"
    return False, ""

def scoring_streak(goals_scored):
    streak = 0
    for g in reversed(cap5(goals_scored)):
        if g > 0: streak += 1
        else: break
    return streak

def conceding_streak(goals_conceded):
    streak = 0
    for g in reversed(cap5(goals_conceded)):
        if g > 0: streak += 1
        else: break
    return streak

def winning_streak(form):
    streak = 0
    for r in reversed(cap5(form)):
        if r == "W": streak += 1
        else: break
    return streak

def unbeaten_streak(form):
    streak = 0
    for r in reversed(cap5(form)):
        if r in ("W","D"): streak += 1
        else: break
    return streak

def team_tendency(stats):
    scored = cap5(stats.goals_scored)
    conceded = cap5(stats.goals_conceded)
    form = cap5(stats.form)
    n = len(scored) or 1
    return {
        "n": n,
        "games_scored_in": sum(1 for g in scored if g > 0),
        "games_conceded_in": sum(1 for g in conceded if g > 0),
        "clean_sheets": sum(1 for g in conceded if g == 0),
        "games_scored_2plus": sum(1 for g in scored if g >= 2),
        "total_scored": sum(scored),
        "total_conceded": sum(conceded),
        "scoring_streak": scoring_streak(stats.goals_scored),
        "conceding_streak": conceding_streak(stats.goals_conceded),
        "winning_streak": winning_streak(stats.form),
        "unbeaten_streak": unbeaten_streak(stats.form),
        "wins": form.count("W"),
        "draws": form.count("D"),
        "losses": form.count("L"),
        "over_15_count": sum(1 for s,c in zip(scored,conceded) if s+c > 1),
        "over_25_count": sum(1 for s,c in zip(scored,conceded) if s+c > 2),
        "btts_count": sum(1 for s,c in zip(scored,conceded) if s > 0 and c > 0),
    }

def analyse_h2h(h2h, home_team):
    if not h2h or not h2h.matches:
        return {"available":False,"home_win_rate":0.5,"over_15_rate":0.5,
                "over_25_rate":0.5,"btts_rate":0.5,"avg_total_goals":0.0,"summary":"No H2H data"}
    matches = h2h.matches[-5:]
    total = len(matches)
    home_wins = btts = 0
    goal_totals = []
    for m in matches:
        hg, ag = m["score"]
        goal_totals.append(hg+ag)
        if (m["home"]==home_team and hg>ag) or (m["away"]==home_team and ag>hg):
            home_wins += 1
        if hg > 0 and ag > 0: btts += 1
    avg = safe_avg(goal_totals)
    return {
        "available": True,
        "home_win_rate": home_wins/total,
        "over_15_rate": sum(1 for g in goal_totals if g>1)/total,
        "over_25_rate": sum(1 for g in goal_totals if g>2)/total,
        "btts_rate": btts/total,
        "avg_total_goals": round(avg,2),
        "summary": f"{home_team} W{home_wins} of last {total} H2H"
    }

def position_score(hp, ap, total, weight):
    if not hp or not ap or not total or weight==0: return 0.5
    hn = 1-((hp-1)/(total-1))
    an = 1-((ap-1)/(total-1))
    return round(0.5+((hn-an+1)/2-0.5)*weight, 3)

def market_1x2(side, home_t, away_t, h2h):
    if side=="home":
        conf = min(95,int((home_t["winning_streak"]/5*0.4+h2h["home_win_rate"]*0.3+home_t["wins"]/5*0.3)*100))
        return BetSignal("Home Win","primary",max(40,conf))
    else:
        conf = min(95,int((away_t["winning_streak"]/5*0.4+(1-h2h["home_win_rate"])*0.3+away_t["wins"]/5*0.3)*100))
        return BetSignal("Away Win","primary",max(40,conf))

def market_double_chance(side, home_t, away_t, h2h, cfg):
    if "dc" not in cfg.markets: return None
    if home_t["winning_streak"]>=2 and away_t["winning_streak"]>=2:
        conf = min(90,int((home_t["winning_streak"]+away_t["winning_streak"])/10*100))
        return BetSignal("12 (Home or Away)","secondary",max(45,conf))
    if side=="home" and home_t["unbeaten_streak"]>=3 and away_t["losses"]>=2:
        conf = min(90,int((home_t["unbeaten_streak"]/5*0.5+away_t["losses"]/5*0.5)*100))
        return BetSignal("1X (Home or Draw)","secondary",max(45,conf))
    if side=="away" and away_t["unbeaten_streak"]>=3 and home_t["losses"]>=2:
        conf = min(90,int((away_t["unbeaten_streak"]/5*0.5+home_t["losses"]/5*0.5)*100))
        return BetSignal("X2 (Away or Draw)","secondary",max(45,conf))
    return None

def market_over15(home_t, away_t, h2h, cfg):
    if "over15" not in cfg.markets: return None
    n = home_t["n"]
    if n==0: return None
    score = (
        home_t["over_15_count"]/n*0.20 +
        away_t["over_15_count"]/n*0.20 +
        home_t["games_scored_in"]/n*0.15 +
        away_t["games_scored_in"]/n*0.15 +
        home_t["games_conceded_in"]/n*0.10 +
        away_t["games_conceded_in"]/n*0.10 +
        h2h["over_15_rate"]*0.10
    )
    if score >= 0.55:
        return BetSignal("Over 1.5 Goals","secondary",min(92,int(score*100)))
    return None

def market_btts(home_t, away_t, h2h, cfg):
    if "btts" not in cfg.markets: return None
    n = home_t["n"]
    if n==0: return None
    hs = home_t["games_scored_in"]; as_ = away_t["games_scored_in"]
    hc = home_t["games_conceded_in"]; ac = away_t["games_conceded_in"]
    cs = home_t["clean_sheets"]+away_t["clean_sheets"]
    if hs>=3 and as_>=3 and hc>=3 and ac>=3:
        goals_w = min(1.0,(home_t["total_scored"]+away_t["total_scored"]+home_t["total_conceded"]+away_t["total_conceded"])/(n*4))
        base = ((hs+as_)/(n*2)*0.35+(hc+ac)/(n*2)*0.35+h2h["btts_rate"]*0.20+goals_w*0.10)
        return BetSignal("BTTS Yes","secondary",max(45,min(92,int(base*100))))
    if cs >= (hs+as_) or home_t["clean_sheets"]>=2 or away_t["clean_sheets"]>=2:
        dom = cs/((hs+as_)+1)
        return BetSignal("BTTS No","warn",max(40,min(85,int(dom*60)+30)))
    return None

def market_to_score(home_t, away_t, cfg):
    if "to_score" not in cfg.markets: return []
    n = home_t["n"]
    if n==0: return []
    signals = []
    if home_t["games_scored_in"]>=4:
        conf = min(92,int((home_t["games_scored_in"]/n*0.6+away_t["games_conceded_in"]/n*0.3+(1-away_t["clean_sheets"]/n)*0.1)*100))
        signals.append(BetSignal("Home to Score (Over 0.5)","secondary",max(50,conf)))
    if away_t["games_scored_in"]>=4:
        conf = min(92,int((away_t["games_scored_in"]/n*0.6+home_t["games_conceded_in"]/n*0.3+(1-home_t["clean_sheets"]/n)*0.1)*100))
        signals.append(BetSignal("Away to Score (Over 0.5)","secondary",max(50,conf)))
    return signals

def market_btts_over25(home_t, away_t, h2h, cfg):
    if "btts_over25" not in cfg.markets: return None
    n = home_t["n"]
    if n==0: return None
    if home_t["games_scored_in"]<3 or away_t["games_scored_in"]<3: return None
    if home_t["games_conceded_in"]<3 or away_t["games_conceded_in"]<3: return None
    total_goals = home_t["total_scored"]+away_t["total_scored"]+home_t["total_conceded"]+away_t["total_conceded"]
    gpg = total_goals/(n*2)
    score = (home_t["over_25_count"]/n*0.25+away_t["over_25_count"]/n*0.25+min(1.0,gpg/3)*0.30+h2h["over_25_rate"]*0.20)
    if score>=0.50:
        return BetSignal("BTTS + Over 2.5","combo",max(45,min(90,int(score*100))))
    return None

def market_combos(side, home_t, away_t, h2h, over15, btts, to_score, cfg):
    if "combo" not in cfg.markets: return []
    combos = []
    if side=="away" and over15 and over15.confidence>=60:
        combos.append(BetSignal("Away Win + Over 1.5","combo",min(88,int((over15.confidence+60)/2))))
    if side=="home" and over15 and over15.confidence>=60 and home_t["unbeaten_streak"]>=2:
        combos.append(BetSignal("1X + Over 1.5","combo",min(88,int((over15.confidence+65)/2))))
    if btts and btts.label=="BTTS Yes" and btts.confidence>=55:
        for ts in to_score:
            if ts.confidence>=55:
                combos.append(BetSignal(f"{ts.label} + BTTS Yes","combo",min(88,int((btts.confidence+ts.confidence)/2))))
    return combos

def generate_signals(home, away, home_t, away_t, h2h, side):
    signals = []
    def sig(text, t=""): signals.append({"text":text,"type":t})
    if home_t["winning_streak"]>=3: sig(f"{home.name} on {home_t['winning_streak']}-game winning streak")
    if away_t["winning_streak"]>=3: sig(f"{away.name} on {away_t['winning_streak']}-game winning streak")
    if home_t["scoring_streak"]>=4: sig(f"{home.name} scored in last {home_t['scoring_streak']} matches","blue")
    if away_t["scoring_streak"]>=4: sig(f"{away.name} scored in last {away_t['scoring_streak']} matches","blue")
    if away_t["conceding_streak"]>=4: sig(f"{away.name} conceded in last {away_t['conceding_streak']} matches","blue")
    if home_t["conceding_streak"]>=4: sig(f"{home.name} conceded in last {home_t['conceding_streak']} matches","blue")
    if home_t["clean_sheets"]>=3: sig(f"{home.name} kept {home_t['clean_sheets']} clean sheets in last 5","warn")
    if away_t["clean_sheets"]>=3: sig(f"{away.name} kept {away_t['clean_sheets']} clean sheets in last 5","warn")
    sig(f"{home.name} last 5: {home_t['wins']}W {home_t['draws']}D {home_t['losses']}L")
    sig(f"{away.name} last 5: {away_t['wins']}W {away_t['draws']}D {away_t['losses']}L")
    if h2h["available"]: sig(f"H2H: {h2h['summary']} — avg {h2h['avg_total_goals']} goals","blue")
    if home.league_position and away.league_position:
        gap = abs(away.league_position-home.league_position)
        sig(f"Table: #{home.league_position} vs #{away.league_position} ({gap} place gap)")
    return signals

def build_team_notes(name, t):
    notes = [f"Scored in {t['games_scored_in']} of last {t['n']} games",
             f"Conceded in {t['games_conceded_in']} of last {t['n']} games"]
    if t["clean_sheets"]>0: notes.append(f"{t['clean_sheets']} clean sheet(s) in last {t['n']}")
    if t["scoring_streak"]>=3: notes.append(f"Scoring streak: {t['scoring_streak']} games")
    if t["conceding_streak"]>=3: notes.append(f"Conceding streak: {t['conceding_streak']} games")
    return notes

def composite_confidence(side, home_t, away_t, h2h, pos, low_confidence):
    n = home_t["n"]
    if n==0: return 30
    if side=="home":
        form_sig = home_t["wins"]/n*0.6+home_t["winning_streak"]/5*0.4
        h2h_sig = h2h["home_win_rate"]
    else:
        form_sig = away_t["wins"]/n*0.6+away_t["winning_streak"]/5*0.4
        h2h_sig = 1-h2h["home_win_rate"]
    goals_sig = min(1.0,(home_t["total_scored"]+away_t["total_scored"])/(n*3))
    raw = form_sig*0.40+pos*0.20+h2h_sig*0.20+goals_sig*0.20
    if low_confidence: raw *= 0.85
    return max(30,min(95,int(raw*100)))

def score_match(match_id, home, away, odds, league, time, cfg, h2h=None, total_teams_in_league=20):
    passes, side = passes_odds_filter(odds, cfg)
    if not passes: return None
    home_low,_ = form_confidence(home, cfg)
    away_low,_ = form_confidence(away, cfg)
    low_confidence = home_low or away_low
    low_conf_team = home.name if home_low else (away.name if away_low else None)
    home_t = team_tendency(home)
    away_t = team_tendency(away)
    h2h_data = analyse_h2h(h2h, home.name)
    pos = position_score(home.league_position, away.league_position, total_teams_in_league, cfg.league_position_weight)
    bets = [market_1x2(side, home_t, away_t, h2h_data)]
    for fn, args in [
        (market_double_chance, (side, home_t, away_t, h2h_data, cfg)),
        (market_over15, (home_t, away_t, h2h_data, cfg)),
        (market_btts, (home_t, away_t, h2h_data, cfg)),
        (market_btts_over25, (home_t, away_t, h2h_data, cfg)),
    ]:
        r = fn(*args)
        if r: bets.append(r)
    bets.extend(market_to_score(home_t, away_t, cfg))
    bets.extend(market_combos(side, home_t, away_t, h2h_data,
        next((b for b in bets if b.label=="Over 1.5 Goals"),None),
        next((b for b in bets if b.label in ("BTTS Yes","BTTS No")),None),
        [b for b in bets if "to Score" in b.label], cfg))
    confidence = composite_confidence(side, home_t, away_t, h2h_data, pos, low_confidence)
    return {
        "id": match_id, "time": time, "league": league,
        "home": home.name, "away": away.name,
        "homePos": home.league_position, "awayPos": away.league_position,
        "h2hSummary": h2h_data["summary"],
        "odds": {"home": odds.home, "draw": odds.draw, "away": odds.away},
        "confidence": confidence, "lowConfidence": low_confidence, "lowConfTeam": low_conf_team,
        "recommendation": side,
        "bets": [{"label": b.label, "type": b.type} for b in bets],
        "homeStats": {"name": home.name, "form": cap5(home.form), "notes": build_team_notes(home.name, home_t)},
        "awayStats": {"name": away.name, "form": cap5(away.form), "notes": build_team_notes(away.name, away_t)},
        "signals": generate_signals(home, away, home_t, away_t, h2h_data, side)
    }

def score_all_matches(raw_matches, cfg):
    results = []
    for m in raw_matches:
        try:
            odds = MatchOdds(**m["odds"])
            home_stats = TeamStats(**m["home_stats"])
            away_stats = TeamStats(**m["away_stats"])
            h2h = H2HRecord(matches=m["h2h"]["matches"]) if m.get("h2h") and m["h2h"].get("matches") else None
            result = score_match(m["id"], home_stats, away_stats, odds, m["league"], m["time"], cfg, h2h, m.get("total_teams_in_league",20))
            if result: results.append(result)
        except Exception as e:
            print(f"[scorer] Error on {m.get('id','?')}: {e}")
    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results

if __name__ == "__main__":
    cfg = FilterConfig()
    sample = [{"id":"test_001","league":"Premier League","time":"13:30","total_teams_in_league":20,
        "odds":{"home":1.55,"draw":4.20,"away":6.50,"over_15":None,"over_25":None,"btts_yes":None},
        "home_stats":{"name":"Arsenal","form":["W","D","W","W","W"],"goals_scored":[2,1,3,2,2],
            "goals_conceded":[0,1,1,0,1],"league_position":2,"total_teams_in_league":20,"opponent_positions":None},
        "away_stats":{"name":"Wolves","form":["L","D","L","W","L"],"goals_scored":[0,1,1,2,0],
            "goals_conceded":[2,1,2,1,3],"league_position":16,"total_teams_in_league":20,"opponent_positions":None},
        "h2h":{"matches":[{"home":"Arsenal","away":"Wolves","score":[3,1]},{"home":"Wolves","away":"Arsenal","score":[0,2]},
            {"home":"Arsenal","away":"Wolves","score":[2,0]},{"home":"Wolves","away":"Arsenal","score":[1,2]},
            {"home":"Arsenal","away":"Wolves","score":[2,1]}]}}]
    results = score_all_matches(sample, cfg)
    print(json.dumps(results, indent=2))
    print(f"\n✓ {len(results)} match(es) scored")
    if results: print(f"  Markets: {[b['label'] for b in results[0]['bets']]}")
