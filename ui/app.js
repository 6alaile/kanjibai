document.addEventListener('DOMContentLoaded', function () {
const { createApp, ref, computed, reactive } = Vue;

// ─── FETCH ENRICHED DATA ─────────────────────────────────────────────────────
async function loadEnriched() {
  try {
    const resp = await fetch('/data/enriched.json?t=' + Date.now());
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } catch (e) {
    console.warn('enriched.json fetch failed:', e.message);
    return { meta: null, matches: [] };
  }
}

// ─── POSITION ──────────────────────────────────────────────────────────────────

// League position as an ordinal: 1st, 2nd, 3rd, 4th … 11th, 21st, 22nd, 23rd.
function ordinal(n) {
  if (!n) return '';
  const suffix = ['th', 'st', 'nd', 'rd'];
  const v = n % 100;
  return `${n}${suffix[(v - 20) % 10] || suffix[v] || suffix[0]}`;
}

// "3rd/16" — ordinal position with the league size when it is known.
function posLabel(pos, total) {
  if (!pos) return '';
  return total ? `${ordinal(pos)}/${total}` : ordinal(pos);
}

// ─── TIME ──────────────────────────────────────────────────────────────────────
// Kick-off times arrive as HH:MM (BetPawa renders EAT). Normalise defensively:
// 12h values ("7:30 PM"), ISO timestamps and malformed values are handled so
// the card always shows a 24h clock.
function parseTime(timeStr) {
  if (!timeStr) return null;
  const s = String(timeStr).trim();
  const m = /(\d{1,2}):(\d{2})/.exec(s);
  if (!m) return null;
  let hours = parseInt(m[1], 10);
  const minutes = parseInt(m[2], 10);
  if (hours > 23 || minutes > 59) return null;
  const ampm = /\s?(AM|PM)\b/i.exec(s);
  if (ampm) {
    const period = ampm[1].toUpperCase();
    if (period === 'PM' && hours < 12) hours += 12;
    if (period === 'AM' && hours === 12) hours = 0;
  }
  return { hours, minutes };
}

// Minutes since midnight — numeric sort key for kick-off order.
function timeMinutes(timeStr) {
  const t = parseTime(timeStr);
  return t ? t.hours * 60 + t.minutes : 24 * 60;
}

// ─── SCORER ────────────────────────────────────────────────────────────────────

const N = 5;

// Form arrays arrive newest first (index 0 is the most recent match), so the
// last five matches are the first five entries.
function cap5(arr) {
  return arr ? arr.slice(0, N) : [];
}

function safeAvg(arr, fallback = 0) {
  return arr && arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : fallback;
}

// Slider values arrive from v-model as strings ("0.5"), so numeric filter
// values are coerced before they are used in arithmetic.
function num(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

// A probability for display: whole percent, never 100 — there is always
// some doubt left, and "100%" on a chip reads as a guarantee.
function pct100(p) {
  return Math.max(1, Math.min(99, Math.round(p * 100)));
}

// Pull a goal probability back toward the coin flip — see GOAL_CALIBRATION.
function calibrateGoal(p) {
  return Math.max(0.01, Math.min(0.99, 0.5 + (p - 0.5) * GOAL_CALIBRATION));
}

// The odds gate only decides *whether* a fixture qualifies for a card — the
// home/draw/away choice is made by the probability model (see below). Kept
// deliberately: both sliders still describe the same price band, one side has
// to be inside it and the other side has to be worth taking.
function passesOddsFilter(odds, cfg) {
  if (!odds) return false;
  const h = odds.home, a = odds.away;
  if (typeof h === 'number' && typeof a === 'number') {
    const max = num(cfg.homeOddsMax, 10);
    const min = num(cfg.awayOddsMin, 1.2);
    if (h < max && a >= min) return true;
    if (a < max && h >= min) return true;
  }
  return false;
}

function formConfidence(stats, cfg) {
  const n = (stats.form || []).length;
  return n < num(cfg.minFormMatches, 5);
}

// Streaks count back from the most recent match, which sits at index 0.
function scoringStreak(goalsScored) {
  let streak = 0;
  const arr = cap5(goalsScored || []);
  for (let i = 0; i < arr.length; i++) {
    if (arr[i] > 0) streak++; else break;
  }
  return streak;
}

function concedingStreak(goalsConceded) {
  let streak = 0;
  const arr = cap5(goalsConceded || []);
  for (let i = 0; i < arr.length; i++) {
    if (arr[i] > 0) streak++; else break;
  }
  return streak;
}

function winningStreak(form) {
  let streak = 0;
  const arr = cap5(form || []);
  for (let i = 0; i < arr.length; i++) {
    if (arr[i] === 'W') streak++; else break;
  }
  return streak;
}

function unbeatenStreak(form) {
  let streak = 0;
  const arr = cap5(form || []);
  for (let i = 0; i < arr.length; i++) {
    if (arr[i] === 'W' || arr[i] === 'D') streak++; else break;
  }
  return streak;
}

function teamTendency(stats) {
  const scored = cap5(stats.goals_scored || []);
  const conceded = cap5(stats.goals_conceded || []);
  const form = cap5(stats.form || []);
  const n = scored.length || 1;
  return {
    n,
    games_scored_in: scored.filter(g => g > 0).length,
    games_conceded_in: conceded.filter(g => g > 0).length,
    clean_sheets: conceded.filter(g => g === 0).length,
    games_scored_2plus: scored.filter(g => g >= 2).length,
    total_scored: scored.reduce((a, b) => a + b, 0),
    total_conceded: conceded.reduce((a, b) => a + b, 0),
    scoring_streak: scoringStreak(stats.goals_scored),
    conceding_streak: concedingStreak(stats.goals_conceded),
    winning_streak: winningStreak(stats.form),
    unbeaten_streak: unbeatenStreak(stats.form),
    wins: form.filter(r => r === 'W').length,
    draws: form.filter(r => r === 'D').length,
    losses: form.filter(r => r === 'L').length,
    over_15_count: scored.filter((g, i) => g + (conceded[i] || 0) > 1).length,
    over_25_count: scored.filter((g, i) => g + (conceded[i] || 0) > 2).length,
    btts_count: scored.filter((g, i) => g > 0 && (conceded[i] || 0) > 0).length,
  };
}

function analyseH2H(h2h, homeTeam) {
  if (!h2h || !h2h.matches || !h2h.matches.length) {
    return { available: false, home_win_rate: 0.5, away_win_rate: 0.25, draw_rate: 0.25, over_15_rate: 0.5, over_25_rate: 0.5, btts_rate: 0.5, avg_total_goals: 0, summary: 'No H2H data' };
  }
  // H2H arrives newest first; the five most recent meetings are the ones shown.
  const matches = h2h.matches.slice(0, 5);
  const total = matches.length;
  let homeWins = 0, btts = 0, draws = 0;
  const goalTotals = matches.map(m => {
    const [hg, ag] = m.score;
    if ((m.home === homeTeam && hg > ag) || (m.away === homeTeam && ag > hg)) homeWins++;
    if (hg === ag) draws++;
    if (hg > 0 && ag > 0) btts++;
    return hg + ag;
  });
  const avg = safeAvg(goalTotals);
  const awayWins = total - homeWins - draws;
  return {
    available: true,
    matches: matches,
    home_win_rate: homeWins / total,
    away_win_rate: awayWins / total,
    draw_rate: draws / total,
    over_15_rate: goalTotals.filter(g => g > 1).length / total,
    over_25_rate: goalTotals.filter(g => g > 2).length / total,
    btts_rate: btts / total,
    avg_total_goals: Math.round(avg * 100) / 100,
    summary: `${homeTeam} W${homeWins} of last ${total} H2H`
  };
}

// League position as a signed gap: +1 when the home side tops the table and
// the away side sits at the bottom, -1 the other way round, 0 when even.
function positionGap(hp, ap, total) {
  if (!hp || !ap || !total || total < 2) return 0;
  const hn = 1 - ((hp - 1) / (total - 1));
  const an = 1 - ((ap - 1) / (total - 1));
  return (hn - an) / 2;
}

// The position term of the 1X2 stats signal: 0.5 is neutral and the slider
// decides how far a table gap can move it.
function positionScore(hp, ap, total, weight) {
  const w = num(weight, 0);
  if (!hp || !ap || !total || w === 0) return 0.5;
  return Math.round((0.5 + positionGap(hp, ap, total) * w) * 1000) / 1000;
}

// ─── 1X2 PROBABILITY MODEL ────────────────────────────────────────────────────
// Every weight the model uses is named here, so the whole thing is tuned in
// one place.
//
// The model blends two views of the same fixture:
//   • market — the de-vigged 1X2 prices (the sharpest prior available)
//   • stats  — recent form, head-to-head and league position (our "signal")
// The blend is driven by the Opponent Strength slider: at its default (0.5)
// the model reads 70/30 market/stats, at 0 it is pure market and at 1 the
// stats carry 60% of the probability.
const OUTCOMES = ['home', 'draw', 'away'];
const STATS_WEIGHT_SCALE = 0.60;     // stats share = STATS_WEIGHT_SCALE × slider
const H2H_WEIGHT = 0.25;             // inside the stats signal: head-to-head
const POSITION_WEIGHT = 0.30;        // inside the stats signal: league position
const DRAW_POSITION_PENALTY = 0.20;  // draws are rarer when one side dominates
// A pick must be at least this likely, otherwise the fixture is a no-bet.
// 36% is clear of the 33% three-way coin flip: below it the fixture is
// genuinely even and there is nothing to take.
const MIN_PICK_PROB = 0.36;

// League-average priors, used when the form feed has nothing for a team.
// They keep the distribution whole (win + draw + loss = 1) instead of
// silently dropping a side out of the maths.
const FORM_PRIOR = { win: 0.45, draw: 0.25, loss: 0.30 };

// ─── POINTS SYSTEM ────────────────────────────────────────────────────────────
// Points are the currency of the form markets: every stat the feed gives us
// is worth a fixed number of points, so a team's recent form reads as one
// score and every market leans on the same evidence.
//
//   win 3 · draw 1 · loss 0        → form points
//   goal 1 · conceded −0.5         → attack / defence points
//   winning streak 0.5 a match     → momentum
//   scoring streak 0.25 a match    → attack (feeds BTTS and to score)
//   conceding streak −0.25 a match → defence (feeds BTTS)
//   home 0.5                       → venue
//   league position                → the table gap, scaled by its slider
const PTS_WIN = 3.0;
const PTS_DRAW = 1.0;
const PTS_LOSS = 0.0;
const PTS_GOAL = 1.0;
const PTS_CONCEDED = -0.5;
const PTS_WIN_STREAK = 0.5;
const PTS_SCORING_STREAK = 0.25;
const PTS_CONCEDING_STREAK = -0.25;
const PTS_HOME = 0.5;
const PTS_POSITION = 4.0;      // the table gap, scaled by the position slider

// The points system speaks in points, the goal model below in goals. These
// convert between them and set how hard each signal is allowed to push.
const GOAL_FORM_WEIGHT = 0.35;      // how much a good run lifts expected goals
const GOAL_FORM_BASE = 0.90;        // form points per game that count as normal
const GOAL_STREAK_WEIGHT = 0.10;    // a match of scoring streak
const GOAL_CONCEDING_WEIGHT = 0.08; // a match of conceding streak
const GOAL_POSITION_WEIGHT = 0.18;  // the table gap, in goals
const GOAL_H2H_WEIGHT = 0.25;       // the head-to-head scoring pattern
const GOAL_EDGE_SCALE = 8.0;        // goal points that count as a full edge
const GOAL_EDGE_TILT = 0.20;        // how hard the goal difference tilts 1X2
const OVER15_H2H_WEIGHT = 0.25;     // head-to-head over-1.5 pattern
const BTTS_H2H_WEIGHT = 0.25;       // head-to-head both-teams-scoring pattern

// League averages, used when a team has no form at all.
const AVG_GOALS_SCORED = 1.30;
const AVG_GOALS_CONCEDED = 1.25;
const AVG_FORM_PER_GAME = 0.92;

// Calibration for the goal markets. The 2026-11-18 backtest (330 decided
// picks) showed the goal probabilities running about 12 points hot: a
// team's last-five window is a noisy, hot-biased estimate of its true
// scoring rate, and the model then picks the fixtures where it is most
// confident — the winner's curse on top of a biased input. Shrinking the
// goal probabilities toward the coin flip by this much puts them back on
// the observed hit rates (Over 1.5: stated 77% vs 76% actual; to score:
// stated 78% vs 74% actual; BTTS Yes: stated 58% vs 47% actual).
//
// The 1X2 model is deliberately left alone — it came out slightly
// conservative in the same backtest (stated 50.5% vs 58.1% actual).
const GOAL_CALIBRATION = 0.75;

// A combo is two picks that both have to land, so its confidence is the
// product of the legs — not their average. Two 60% legs are a 36% double,
// and the backtest says the joints land at about 33%. The legs are pulled
// in opposite directions (a 1-0 home win kills Over 1.5, a 3-2 kills
// BTTS Yes), so the correlations roughly cancel and the plain product is
// the right joint.
const COMBO_MIN_JOINT = 0.30;

// Confidence floors for the form-based markets. Each one is a real
// probability now, so the floors mean what they say. The slider in the
// controls panel drives them; these are the defaults.
//
// Over 1.5 and "team to score" are naturally high-probability selections
// (a fixture expected to produce three goals or more), while a both-teams-
// to-score call is a coin-flip market, so its floor sits lower.
const DEFAULT_MIN_PICK_CONFIDENCE = 75;   // percent, the slider's default
const BTTS_FLOOR_OFFSET = 18;              // points below the slider
const MIN_OVER15_PROB = 0.80;
const MIN_BTTS_PROB = 0.62;
const MIN_TOSCORE_PROB = 0.80;
// The points edge that says the form really favours one side — the reason
// to take the double chance instead of the side itself. At 4.5 the arm's
// confidence never drops below ~70%.
const MIN_DC_EDGE = 4.5;

const OUTCOME_LABELS = { home: 'Home Win', draw: 'Draw', away: 'Away Win' };

// De-vigged 1X2 probabilities from the fixture's prices. A suspended leg is
// absent from the data (never zero), so it drops out of the normalisation and
// carries no probability mass.
function marketProbabilities(odds) {
  const raw = {};
  let sum = 0;
  for (const leg of OUTCOMES) {
    const o = odds ? odds[leg] : null;
    if (typeof o === 'number' && isFinite(o) && o > 0) {
      raw[leg] = 1 / o;
      sum += raw[leg];
    }
  }
  if (!sum) return null;
  const p = {};
  for (const leg of OUTCOMES) p[leg] = raw[leg] ? raw[leg] / sum : 0;
  return p;
}

// A team's win/draw/loss split over its last five, with league-average
// priors when there is no form data at all. The prior is used when the
// feed has no games for the team — an empty form is not a 0-0-0 record.
function formRates(t) {
  const games = t ? t.wins + t.draws + t.losses : 0;
  if (!t || !t.n || !games) {
    return { win: FORM_PRIOR.win, draw: FORM_PRIOR.draw, loss: FORM_PRIOR.loss };
  }
  return { win: t.wins / t.n, draw: t.draws / t.n, loss: t.losses / t.n };
}

// The stats view: a full 1X2 distribution built from form, H2H and league
// position. Form supplies the base rates, H2H and position tilt them.
function statsProbabilities(ht, at, h2h, pos) {
  const hf = formRates(ht);
  const af = formRates(at);

  // Base: a side's expected share pairs its own win rate with the
  // opponent's loss rate; the draw is seeded from both teams' draw rates.
  const base = {
    home: (hf.win + af.loss) / 2,
    draw: (hf.draw + af.draw) / 2,
    away: (af.win + hf.loss) / 2,
  };

  // H2H tilt — only applied when there is a head-to-head record to tilt with.
  // Each rate is centred on its league prior rather than on 50%: the draw
  // happens ~25% of the time, so centring it on 50% would penalise every
  // draw in the sample. The deviation is scaled by the prior's complement
  // (the share of results the outcome does not hold), so a handful of
  // H2H games moves the model gently and a lopsided record moves it a lot.
  const tilt = { home: 1, draw: 1, away: 1 };
  if (h2h.available) {
    tilt.home = 1 + H2H_WEIGHT * (h2h.home_win_rate - FORM_PRIOR.win) / (1 - FORM_PRIOR.win);
    tilt.away = 1 + H2H_WEIGHT * (h2h.away_win_rate - FORM_PRIOR.loss) / (1 - FORM_PRIOR.loss);
    tilt.draw = 1 + H2H_WEIGHT * (h2h.draw_rate - FORM_PRIOR.draw) / (1 - FORM_PRIOR.draw);
  }

  // Goal difference as points — the one piece of evidence the win/draw/loss
  // rates do not carry. It tilts the stats signal towards the side that
  // scores more and concedes less.
  const netGoalPoints = t =>
    t.total_scored * PTS_GOAL + t.total_conceded * PTS_CONCEDED +
    t.scoring_streak * PTS_SCORING_STREAK + t.conceding_streak * PTS_CONCEDING_STREAK;
  const goalEdge = (netGoalPoints(ht) - netGoalPoints(at)) / GOAL_EDGE_SCALE;
  tilt.home *= 1 + GOAL_EDGE_TILT * goalEdge;
  tilt.away *= 1 - GOAL_EDGE_TILT * goalEdge;

  // League position tilt — the better-placed side is favoured, and the wider
  // the gap the less a draw is worth.
  const gap = 2 * pos - 1;  // −1 … +1, home advantage in league position
  const posTilt = {
    home: 1 + POSITION_WEIGHT * gap,
    away: 1 + POSITION_WEIGHT * (-gap),
    draw: 1 - DRAW_POSITION_PENALTY * gap * gap,
  };

  const tilted = {};
  let sum = 0;
  for (const leg of OUTCOMES) {
    tilted[leg] = Math.max(0, base[leg] * tilt[leg] * posTilt[leg]);
    sum += tilted[leg];
  }
  if (!sum) return { home: 1 / 3, draw: 1 / 3, away: 1 / 3 };

  const p = {};
  for (const leg of OUTCOMES) p[leg] = tilted[leg] / sum;
  return p;
}

// The blended model plus the 1X2 selection: the argmax of the three blended
// probabilities, restricted to legs that are actually priced (a suspended leg
// cannot be staked) and inside the max-odds band.
function probabilityModel(match, ht, at, h2h, pos, cfg) {
  const market = marketProbabilities(match.odds);
  const stats = statsProbabilities(ht, at, h2h, pos);
  const slider = num(cfg.oppStrengthWeight, 0);
  const statsWeight = Math.max(0, Math.min(1, STATS_WEIGHT_SCALE * slider));

  const p = {};
  for (const leg of OUTCOMES) {
    const m = market ? market[leg] : 0;
    p[leg] = (1 - statsWeight) * m + statsWeight * stats[leg];
  }

  // Only a leg with a price inside the band can be picked.
  const maxOdds = num(cfg.homeOddsMax, 10);
  const priced = OUTCOMES.filter(leg => {
    const o = match.odds ? match.odds[leg] : null;
    return typeof o === 'number' && o > 0 && o <= maxOdds;
  });
  const pool = priced.length ? priced : OUTCOMES.filter(leg => p[leg] > 0);

  let pick = pool[0] || 'home';
  let best = -1;
  for (const leg of pool) {
    if (p[leg] > best) { best = p[leg]; pick = leg; }
  }

  return { p, market, stats, statsWeight, pick, prob: p[pick] };
}

function market1x2(model) {
  return {
    label: OUTCOME_LABELS[model.pick],
    type: 'primary',
    // The primary pick's confidence *is* the model's probability for the
    // chosen outcome — the old form/streak heuristic is gone.
    confidence: pct100(model.prob),
    prob: pct100(model.prob),
    market: ['1x2', model.pick],
  };
}

// ─── POINTS ENGINE ────────────────────────────────────────────────────────────
// The points a team has collected over its last five results, split so each
// market can use the part it cares about: form, attack, defence, momentum,
// venue and table position.
function teamPoints(t, side, gap, weight) {
  const n = t.n || 0;
  const hasForm = (t.wins + t.draws + t.losses) > 0;

  const form = t.wins * PTS_WIN + t.draws * PTS_DRAW + t.losses * PTS_LOSS;
  const attack = t.total_scored * PTS_GOAL + t.scoring_streak * PTS_SCORING_STREAK;
  const defence = t.total_conceded * PTS_CONCEDED + t.conceding_streak * PTS_CONCEDING_STREAK;
  const momentum = t.winning_streak * PTS_WIN_STREAK;
  const venue = side === 'home' ? PTS_HOME : 0;
  const position = gap * PTS_POSITION * num(weight, 0.5);

  return {
    n,
    hasForm,
    form,
    attack,
    defence,
    momentum,
    venue,
    position,
    total: form + momentum + venue + position,
    // Per-game rates for the goal model. A team with no form at all falls
    // back to the league average rather than to a zero.
    attackRate: hasForm ? t.total_scored / n : AVG_GOALS_SCORED,
    defenceRate: hasForm ? t.total_conceded / n : AVG_GOALS_CONCEDED,
    formPerGame: hasForm ? form / n : AVG_FORM_PER_GAME,
    scoringStreak: t.scoring_streak,
    concedingStreak: t.conceding_streak,
    winningStreak: t.winning_streak,
    draws: t.draws,
  };
}

// ─── GOAL MODEL ───────────────────────────────────────────────────────────────
// Expected goals for the fixture. Each side's expectation blends its own
// attacking output with the opponent's leaking, then takes a nudge from form,
// momentum and the table gap. The head-to-head scoring pattern pulls the
// total towards what these two have produced against each other.
//
// Everything comes out as a probability, so the markets below can be judged
// against a single confidence scale.
function goalModel(hp, ap, h2h, gap, weight) {
  const w = num(weight, 0.5);

  let lambdaHome = (hp.attackRate + ap.defenceRate) / 2;
  let lambdaAway = (ap.attackRate + hp.defenceRate) / 2;

  // Form: a team in a good run scores more than its goal average suggests.
  lambdaHome += GOAL_FORM_WEIGHT * (hp.formPerGame - GOAL_FORM_BASE);
  lambdaAway += GOAL_FORM_WEIGHT * (ap.formPerGame - GOAL_FORM_BASE);

  // Momentum: a scoring streak lifts an attack, a conceding streak lifts
  // what the opponent can expect.
  lambdaHome += GOAL_STREAK_WEIGHT * hp.scoringStreak
              - GOAL_CONCEDING_WEIGHT * ap.concedingStreak;
  lambdaAway += GOAL_STREAK_WEIGHT * ap.scoringStreak
              - GOAL_CONCEDING_WEIGHT * hp.concedingStreak;

  // Table gap: the better-placed side scores a little more and concedes a
  // little less.
  lambdaHome += GOAL_POSITION_WEIGHT * gap * w;
  lambdaAway -= GOAL_POSITION_WEIGHT * gap * w;

  // The head-to-head scoring pattern pulls the total towards the average
  // these two have produced against each other.
  if (h2h.available && h2h.avg_total_goals > 0) {
    const total = Math.max(0.3, lambdaHome + lambdaAway);
    const blended = (1 - GOAL_H2H_WEIGHT) * total + GOAL_H2H_WEIGHT * h2h.avg_total_goals;
    const scale = blended / total;
    lambdaHome *= scale;
    lambdaAway *= scale;
  }

  lambdaHome = Math.max(0.15, lambdaHome);
  lambdaAway = Math.max(0.15, lambdaAway);

  const total = lambdaHome + lambdaAway;
  const homeScores = 1 - Math.exp(-lambdaHome);
  const awayScores = 1 - Math.exp(-lambdaAway);

  return {
    lambdaHome,
    lambdaAway,
    total,
    // Poisson: the chance the match sees more than one goal.
    over15: 1 - Math.exp(-total) * (1 + total),
    homeScores,
    awayScores,
    // Both teams score.
    btts: homeScores * awayScores,
  };
}

// Double chance is the safer twin of the 1X2 pick: it covers the pick plus
// one other outcome, so its confidence is the model's own probability for
// that pair. The arm follows the model's pick and the points system — a Home
// pick pairs with 1X, an Away pick with X2, and a Draw pick pairs with
// whichever arm covers the draw and the likelier of the two sides.
function marketDC(model, hp, ap, cfg) {
  if (!cfg.markets.includes('dc')) return null;
  const p = model.p;
  const edge = hp.total - ap.total;

  // 12 — both teams are in winning form, so the match is too close to call.
  if (hp.winningStreak >= 2 && ap.winningStreak >= 2) {
    const conf = pct100(p.home + p.away);
    return { label: '12 (Home or Away)', type: 'secondary', confidence: conf, prob: conf, market: ['dc', 'home_away'] };
  }
  // 1X — the home side's points are clearly ahead and the model picked home.
  if (model.pick === 'home' && edge >= MIN_DC_EDGE) {
    const conf = pct100(p.home + p.draw);
    return { label: '1X (Home or Draw)', type: 'secondary', confidence: conf, prob: conf, market: ['dc', 'home_draw'] };
  }
  // X2 — the away side's points are clearly ahead and the model picked away.
  if (model.pick === 'away' && edge <= -MIN_DC_EDGE) {
    const conf = pct100(p.away + p.draw);
    return { label: 'X2 (Away or Draw)', type: 'secondary', confidence: conf, prob: conf, market: ['dc', 'draw_away'] };
  }
  // Draw pick — both teams keep drawing, so there is something behind it.
  if (model.pick === 'draw' && hp.draws >= 2 && ap.draws >= 2) {
    const arm = p.home >= p.away ? 'home_draw' : 'draw_away';
    const label = arm === 'home_draw' ? '1X (Home or Draw)' : 'X2 (Away or Draw)';
    const conf = pct100(arm === 'home_draw' ? p.home + p.draw : p.away + p.draw);
    return { label, type: 'secondary', confidence: conf, prob: conf, market: ['dc', arm] };
  }
  return null;
}

// Over 1.5 goals — the expected goals decide it, and the head-to-head
// scoring pattern says how much to trust them. The old version added a
// weighted score of "how often both teams hit 1.5" against a 0.55 cut-off
// on a scale that averaged 0.77, so it fired on 92% of fixtures; this one
// is a probability with a floor that means something.
function marketOver15(goals, h2h, cfg) {
  if (!cfg.markets.includes('over15')) return null;
  const raw = h2h.available
    ? (1 - OVER15_H2H_WEIGHT) * goals.over15 + OVER15_H2H_WEIGHT * h2h.over_15_rate
    : goals.over15;
  const prob = calibrateGoal(raw);
  if (prob < num(cfg.over15Floor, MIN_OVER15_PROB)) return null;
  const conf = pct100(prob);
  return { label: 'Over 1.5 Goals', type: 'secondary', confidence: conf, prob: conf, market: ['total_goals', '1.5', 'over'] };
}

// Both teams to score — the two expected goals decide it, with the scoring
// and conceding streaks already inside them and the head-to-head pattern
// pulled in on top. The old version counted "scored in 3 of the last 5"
// and fired on more than half the fixtures.
function marketBTTS(goals, h2h, cfg) {
  if (!cfg.markets.includes('btts')) return null;
  const raw = h2h.available
    ? (1 - BTTS_H2H_WEIGHT) * goals.btts + BTTS_H2H_WEIGHT * h2h.btts_rate
    : goals.btts;
  const pYes = calibrateGoal(raw);
  // "Both teams score" is the weaker side of this market: it landed 17 of 36
  // times while stating 72%, so it has to clear a much higher bar than the
  // "no goals" side to be worth showing.
  if (pYes >= num(cfg.bttsYesFloor, MIN_BTTS_PROB)) {
    const conf = pct100(pYes);
    return { label: 'BTTS Yes', type: 'secondary', confidence: conf, prob: conf, market: ['btts', 'yes'] };
  }
  const pNo = 1 - pYes;
  if (pNo >= num(cfg.bttsFloor, MIN_BTTS_PROB)) {
    const conf = pct100(pNo);
    return { label: 'BTTS No', type: 'warn', confidence: conf, prob: conf, market: ['btts', 'no'] };
  }
  return null;
}

// To score — the team's expected goals against the opponent's conceding.
// The old version asked for "scored in 4 of the last 5", which is true for
// most teams and produced a to-score leg on nearly every card.
function marketToScore(goals, cfg) {
  if (!cfg.markets.includes('to_score')) return [];
  const signals = [];
  const floor = num(cfg.toScoreFloor, MIN_TOSCORE_PROB);
  const homeProb = calibrateGoal(goals.homeScores);
  const awayProb = calibrateGoal(goals.awayScores);
  if (homeProb >= floor) {
    const conf = pct100(homeProb);
    signals.push({ label: 'Home to Score (Over 0.5)', type: 'secondary', confidence: conf, prob: conf, market: ['home_goals', '0.5', 'over'] });
  }
  if (awayProb >= floor) {
    const conf = pct100(awayProb);
    signals.push({ label: 'Away to Score (Over 0.5)', type: 'secondary', confidence: conf, prob: conf, market: ['away_goals', '0.5', 'over'] });
  }
  return signals;
}

// The three combo picks: both legs must qualify (confidence ≥ 55 each),
// each priced from BetPawa's combined market.
function marketCombos(mainBet, dc, over15, btts, cfg) {
  if (!cfg.markets.includes('combo')) return [];
  const combos = [];
  // Both legs have to land, so the joint is the product of the two
  // probabilities. The old version averaged them, which stated 72% for a
  // pair of legs that actually land together about a third of the time.
  const joint = (a, b) => Math.round((a.prob / 100) * (b.prob / 100) * 100);
  const both = (a, b) => a && b && a.confidence >= 55 && b.confidence >= 55
    && joint(a, b) >= COMBO_MIN_JOINT * 100;
  const side = mainBet.market[1]; // home | draw | away — the model's pick

  if (both(mainBet, btts)) {
    combos.push({
      label: `${mainBet.label} + ${btts.label}`,
      type: 'combo', confidence: joint(mainBet, btts), prob: joint(mainBet, btts),
      market: ['x1x2_btts', `${side}_${btts.market[1]}`]
    });
  }
  if (both(dc, btts)) {
    combos.push({
      label: `${dc.label.slice(0, 2)} + ${btts.label}`,
      type: 'combo', confidence: joint(dc, btts), prob: joint(dc, btts),
      market: ['dc_btts', `${dc.market[1]}_${btts.market[1]}`]
    });
  }
  if (both(mainBet, over15)) {
    combos.push({
      label: `${mainBet.label} + ${over15.label}`,
      type: 'combo', confidence: joint(mainBet, over15), prob: joint(mainBet, over15),
      market: ['result_total', '1.5', `${side}_over`]
    });
  }
  return combos;
}

// A pick's price inside the fixture's BetPawa markets: path like
// ['total_goals', '1.5', 'over'].  Suspended/absent markets → no price.
function betOdds(markets, path) {
  let v = markets;
  for (const key of path || []) {
    if (v == null) return null;
    v = v[key];
  }
  return typeof v === 'number' ? v : null;
}

// "2026-11-17T14:05:00+03:00" → "14:05": the string's own EAT wall time,
// no timezone maths on the client.
function capturedTime(iso) {
  const m = /T(\d{2}:\d{2})/.exec(iso || '');
  return m ? m[1] : '';
}

// Signals carry a team tag so the UI can split them: home | neutral | away.
// `side` was unused and has been dropped with the home/away team names —
// the columns are labelled with the team names instead.
function generateSignals(ht, at, h2h) {
  const signals = [];
  const sig = (text, type = '', team = 'neutral') => signals.push({ text, type, team });
  if (ht.winning_streak >= 3) sig(`${ht.winning_streak}-game winning streak`, '', 'home');
  if (at.winning_streak >= 3) sig(`${at.winning_streak}-game winning streak`, '', 'away');
  if (ht.scoring_streak >= 4) sig(`Scored in last ${ht.scoring_streak} matches`, 'blue', 'home');
  if (at.scoring_streak >= 4) sig(`Scored in last ${at.scoring_streak} matches`, 'blue', 'away');
  if (at.conceding_streak >= 4) sig(`Conceded in last ${at.conceding_streak} matches`, 'blue', 'away');
  if (ht.conceding_streak >= 4) sig(`Conceded in last ${ht.conceding_streak} matches`, 'blue', 'home');
  if (ht.clean_sheets >= 3) sig(`${ht.clean_sheets} clean sheets in last 5`, 'warn', 'home');
  if (at.clean_sheets >= 3) sig(`${at.clean_sheets} clean sheets in last 5`, 'warn', 'away');
  sig(`Last 5: ${ht.wins}W ${ht.draws}D ${ht.losses}L`, '', 'home');
  sig(`Last 5: ${at.wins}W ${at.draws}D ${at.losses}L`, '', 'away');
  if (h2h.available) sig(`H2H: ${h2h.summary} — avg ${h2h.avg_total_goals} goals`, 'blue', 'neutral');
  return signals;
}

function buildTeamNotes(name, t) {
  const notes = [
    `Scored in ${t.games_scored_in} of last ${t.n} games`,
    `Conceded in ${t.games_conceded_in} of last ${t.n} games`,
  ];
  if (t.clean_sheets > 0) notes.push(`${t.clean_sheets} clean sheet(s) in last ${t.n}`);
  if (t.scoring_streak >= 3) notes.push(`Scoring streak: ${t.scoring_streak} games`);
  if (t.conceding_streak >= 3) notes.push(`Conceding streak: ${t.conceding_streak} games`);
  return notes;
}

// Form row helpers — recent_results aligned with form array
function venueFor(team, idx) {
  const r = team.recent_results && team.recent_results[idx];
  if (!r) return '';
  return r.home_away === 'H' ? 'H' : '@';  // @ = away
}

function opponentFor(team, idx) {
  const r = team.recent_results && team.recent_results[idx];
  if (!r || !r.opponent) return '';
  // Abbreviate opponent name
  const name = r.opponent;
  return name.length > 12 ? name.slice(0, 10) + '..' : name;
}

function scorelineFor(team, idx) {
  const gs = team.goals_scored && team.goals_scored[idx];
  const gc = team.goals_conceded && team.goals_conceded[idx];
  if (gs === undefined || gc === undefined) return '';
  return `${gs}:${gc}`;
}

// Match-level score for the card header. It stays a match score (not a pick
// probability) but follows the model's pick, so a draw pick is scored on the
// teams' drawing record rather than on one side's win rate.
function compositeConfidence(side, ht, at, h2h, pos, lowConfidence) {
  const n = ht.n;
  if (!n) return 30;
  let formSig, h2hSig, posSig = pos;
  if (side === 'draw') {
    formSig = (ht.draws/n + at.draws/n) / 2;
    h2hSig = h2h.draw_rate;
    posSig = 1 - Math.abs(2 * pos - 1); // an even table favours the draw
  } else {
    formSig = side === 'home'
      ? ht.wins/n*0.6 + ht.winning_streak/5*0.4
      : at.wins/n*0.6 + at.winning_streak/5*0.4;
    h2hSig = side === 'home' ? h2h.home_win_rate : 1 - h2h.home_win_rate;
  }
  const goalsSig = Math.min(1, (ht.total_scored + at.total_scored) / (n*3));
  let raw = formSig*0.40 + posSig*0.20 + h2hSig*0.20 + goalsSig*0.20;
  if (lowConfidence) raw *= 0.85;
  return Math.max(30, Math.min(95, Math.round(raw*100)));
}

// The shared "no bet" card shape: a fixture that fails the odds gate and a
// fixture the model has no confident enough outcome for look the same.
function noBetMatch(match, reason, note) {
  const odds = match.odds || {};
  return {
    id: match.id,
    time: match.time,
    league: match.league,
    home: match.home,
    away: match.away,
    homePos: null,
    awayPos: null,
    h2hSummary: 'No H2H data',
    odds: { home: odds.home, draw: odds.draw, away: odds.away },
    markets: match.markets || null,
    oddsCapturedAt: match.odds_captured_at || null,
    confidence: 0,
    lowConfidence: true,
    lowConfTeam: '',
    recommendation: '',
    reason,
    bets: [],
    homeStats: {
      name: match.home,
      form: [],
      notes: [note]
    },
    awayStats: {
      name: match.away,
      form: [],
      notes: [note]
    },
    signals: []
  };
}

function scoreMatch(match, cfg) {
  const odds = match.odds || {};

  // The range inputs hand v-model strings, so every numeric filter value is
  // coerced once here instead of relying on JavaScript's string comparisons.
  const cfgN = {
    homeOddsMax: num(cfg.homeOddsMax, 10),
    awayOddsMin: num(cfg.awayOddsMin, 1.2),
    minFormMatches: num(cfg.minFormMatches, 5),
    leaguePositionWeight: num(cfg.leaguePositionWeight, 0.5),
    oppStrengthWeight: num(cfg.oppStrengthWeight, 0.5),
    minPickConfidence: num(cfg.minPickConfidence, DEFAULT_MIN_PICK_CONFIDENCE),
    markets: cfg.markets || [],
  };

  // The confidence floors for the secondary markets follow the slider: a
  // higher floor means the model has to be more certain before the market
  // is offered, which is the dial behind "too many picks".
  const pickFloor = cfgN.minPickConfidence / 100;
  cfgN.over15Floor = pickFloor;
  cfgN.toScoreFloor = pickFloor;
  // "Both teams score" has to clear the full slider, "no goals" the lower bar.
  cfgN.bttsYesFloor = pickFloor;
  cfgN.bttsFloor = Math.max(0.5, pickFloor - BTTS_FLOOR_OFFSET / 100);

  // The odds gate decides whether the fixture qualifies at all; the model
  // below decides which of home/draw/away is taken.
  if (!passesOddsFilter(odds, cfgN)) {
    return noBetMatch(
      match,
      'Odds do not meet filter criteria (need one side ≤ ' + cfgN.homeOddsMax + ' with the other ≥ ' + cfgN.awayOddsMin + ')',
      'Odds below filtering threshold'
    );
  }

  const homeStats = match.home_stats || match.homeStats || {};
  const awayStats = match.away_stats || match.awayStats || {};
  const h2hRaw = match.h2h || null;

  const homeLowConf = formConfidence(homeStats, cfgN);
  const awayLowConf = formConfidence(awayStats, cfgN);
  const lowConfidence = homeLowConf || awayLowConf;
  const lowConfTeam = homeLowConf ? match.home : (awayLowConf ? match.away : null);

  const ht = teamTendency(homeStats);
  const at = teamTendency(awayStats);
  const h2h = analyseH2H(h2hRaw, match.home);
  const totalTeams = match.total_teams_in_league || 20;
  const gap = positionGap(homeStats.league_position, awayStats.league_position, totalTeams);
  const pos = positionScore(
    homeStats.league_position, awayStats.league_position,
    totalTeams, cfgN.leaguePositionWeight
  );

  const model = probabilityModel(match, ht, at, h2h, pos, cfgN);

  // No outcome is strong enough to take — return the no-bet card with a
  // reason that names the best outcome instead of the old home/away logic.
  // The floor is compared against the rounded percentage, so the reason and
  // the chip always read the same number.
  const pickPct = Math.round(model.prob * 100);
  if (pickPct < MIN_PICK_PROB * 100) {
    return noBetMatch(
      match,
      'No confident enough outcome — best is ' + OUTCOME_LABELS[model.pick] + ' at ' +
        pickPct + '%, below the ' + Math.round(MIN_PICK_PROB * 100) + '% floor',
      'No outcome above the confidence floor'
    );
  }

  // The points system and the goal model: one set of evidence, read by every
  // market below.
  const hp = teamPoints(ht, 'home', gap, cfgN.leaguePositionWeight);
  const ap = teamPoints(at, 'away', -gap, cfgN.leaguePositionWeight);
  const goals = goalModel(hp, ap, h2h, gap, cfgN.leaguePositionWeight);

  const bets = [];
  bets.push(market1x2(model));

  const dc = marketDC(model, hp, ap, cfgN);
  if (dc) bets.push(dc);

  const over15 = marketOver15(goals, h2h, cfgN);
  if (over15) bets.push(over15);

  const btts = marketBTTS(goals, h2h, cfgN);
  if (btts) bets.push(btts);

  const toScore = marketToScore(goals, cfgN);
  toScore.forEach(b => bets.push(b));

  const combos = marketCombos(bets[0], dc, over15, btts, cfgN);
  combos.forEach(b => bets.push(b));

  const confidence = compositeConfidence(model.pick, ht, at, h2h, pos, lowConfidence);

  return {
    id: match.id,
    time: match.time,
    league: match.league,
    home: match.home,
    away: match.away,
    homePos: homeStats.league_position || null,
    awayPos: awayStats.league_position || null,
    homeTotal: homeStats.total_teams_in_league || match.total_teams_in_league || null,
    awayTotal: awayStats.total_teams_in_league || match.total_teams_in_league || null,
    h2hSummary: h2h.summary,
    h2hMatches: h2h.available ? h2h.matches.slice(0, 5) : null,
    odds: { home: odds.home, draw: odds.draw, away: odds.away },
    markets: match.markets || null,
    oddsCapturedAt: match.odds_captured_at || null,
    confidence,
    lowConfidence,
    lowConfTeam,
    recommendation: model.pick,
    reason: '',
    // The picks contract: label, type, market path, price and the probability
    // behind the pick. `prob` is the model's probability for the 1X2 pick and
    // the market's own confidence score for every other pick (both 0–100).
    bets: bets.map(b => ({
      label: b.label, type: b.type, market: b.market || null,
      price: betOdds(match.markets, b.market),
      prob: typeof b.prob === 'number' ? b.prob
        : (typeof b.confidence === 'number' ? b.confidence : null)
    })),
    homeStats: {
      name: match.home,
      form: cap5(homeStats.form || []),
      goals_scored: cap5(homeStats.goals_scored || []),
      goals_conceded: cap5(homeStats.goals_conceded || []),
      recent_results: cap5(homeStats.recent_results || []),
      notes: buildTeamNotes(match.home, ht),
      winningStreak: ht.winning_streak,
      unbeatenStreak: ht.unbeaten_streak,
      cleanSheets: ht.clean_sheets
    },
    awayStats: {
      name: match.away,
      form: cap5(awayStats.form || []),
      goals_scored: cap5(awayStats.goals_scored || []),
      goals_conceded: cap5(awayStats.goals_conceded || []),
      recent_results: cap5(awayStats.recent_results || []),
      notes: buildTeamNotes(match.away, at),
      winningStreak: at.winning_streak,
      unbeatenStreak: at.unbeaten_streak,
      cleanSheets: at.clean_sheets
    },
    signals: generateSignals(ht, at, h2h)
  };
}

function scoreAll(matches, cfg) {
  const results = matches
    .map(m => { try { return scoreMatch(m, cfg); } catch(e) { console.warn('Score error:', e); return null; } })
    .filter(Boolean)
    .sort((a, b) => b.confidence - a.confidence);
  return results;
}

// ─── BET SLIP ──────────────────────────────────────────────────────────────────
// A pick can only go on the slip when BetPawa priced it — a suspended market
// has no price and cannot be staked.
function isPriced(bet) {
  return typeof bet.price === 'number' && isFinite(bet.price) && bet.price > 0;
}

// localStorage is date-scoped: one slip key per scan date,
// `kanjibai.slip.<YYYY-MM-DD>` — the scan's own `meta.date`, falling back to
// today's date in EAT before the scan loads. Keys for any other date belong to
// an older scan and are dropped on restore. Returns null when storage is
// unavailable (private mode, blocked cookies) — the slip then lives in memory
// only, with no console noise.
const SLIP_KEY_PREFIX = 'kanjibai.slip.';

function slipStorage() {
  try {
    const probe = SLIP_KEY_PREFIX + 'probe';
    window.localStorage.setItem(probe, '1');
    window.localStorage.removeItem(probe);
    return window.localStorage;
  } catch (e) {
    return null;
  }
}

// Today as YYYY-MM-DD in the site's timezone (EAT).
function eatDate(d) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Africa/Dar_es_Salaam',
    year: 'numeric', month: '2-digit', day: '2-digit'
  }).format(d || new Date());
}

// ─── VUE APP ─────────────────────────────────────────────────────────────────

createApp({
  setup() {
    const today = new Date().toLocaleDateString('en-GB', { weekday:'short', day:'numeric', month:'short', year:'numeric' });
    const loading = ref(false);
    const hasRun = ref(false);
    const openCards = ref([]);
    const controlsOpen = ref(true);
    const activeFilter = ref('all');
    const loadingStep = ref('');
    const loadingDetail = ref('');
    const scanMeta = ref(null);
    const rawMatches = ref([]); // enriched data from GitHub

    // Live clock
    const clockTime = ref('');
    function updateClock() {
      clockTime.value = new Date().toLocaleTimeString('en-GB', {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        timeZone: 'Africa/Dar_es_Salaam'
      }) + ' EAT';
    }
    updateClock();
    setInterval(updateClock, 1000);

    // 24h time formatting helper
    function formatTime24(timeStr) {
      if (!timeStr) return '';
      const t = parseTime(timeStr);
      if (!t) return String(timeStr).trim();
      return `${String(t.hours).padStart(2, '0')}:${String(t.minutes).padStart(2, '0')}`;
    }

    const filters = reactive({
      homeOddsMax: 10.00,
      awayOddsMin: 1.20,
      minFormMatches: 5,
      leaguePositionWeight: 0.5,
      oppStrengthWeight: 0.5,
      minPickConfidence: DEFAULT_MIN_PICK_CONFIDENCE,
      markets: ['1x2', 'dc', 'over15', 'btts', 'to_score', 'combo']
    });

    const marketOptions = [
      { key: '1x2', label: '1X2 (H/D/A)' },
      { key: 'dc', label: 'Double Chance' },
      { key: 'over15', label: 'Over 1.5' },
      { key: 'btts', label: 'BTTS' },
      { key: 'to_score', label: 'To Score' },
      { key: 'combo', label: 'Combos' }
    ];

    const filterOptions = [
      { key: 'all', label: 'All' },
      { key: 'high', label: 'High Conf (75+)' },
      { key: 'combo', label: 'Has Combo' },
      { key: 'warn', label: 'Low Conf Flag' }
    ];

    // Card ordering
    const sortBy = ref('time');

    const sortOptions = [
      { key: 'time', label: 'Kick-off' },
      { key: 'score', label: 'Score' },
      { key: 'league', label: 'League' }
    ];

    // Competition filter — shows only the picked competition (was bound to an
    // "exclude" field, so picking a league hid it instead of showing it).
    const selectedCompetition = ref('');

    // League string is "Football / <Country> / <Competition>" → label "Country · Competition"
    function competitionLabel(league) {
      const parts = (league || '').split('/').map(p => p.trim()).filter(Boolean);
      if (!parts.length) return '';
      const comp = parts[parts.length - 1];
      const country = parts.length > 1 ? parts[parts.length - 2] : '';
      return country ? `${country} · ${comp}` : comp;
    }

    const competitionOptions = computed(() => {
      const seen = new Map();
      for (const m of rawMatches.value || []) {
        const full = (m.league || '').trim();
        if (!full || seen.has(full)) continue;
        seen.set(full, { value: full, label: competitionLabel(full) });
      }
      return Array.from(seen.values()).sort((a, b) => a.label.localeCompare(b.label));
    });

    // Scored matches — recomputed whenever filters change
    const matches = computed(() => {
      const selected = selectedCompetition.value;
      const filtered = selected
        ? rawMatches.value.filter(m => (m.league || '').trim() === selected)
        : rawMatches.value;

      const scored = scoreAll(filtered, filters);
      const byTime = (a, b) => timeMinutes(a.time) - timeMinutes(b.time);
      const sorters = {
        time: byTime,
        score: (a, b) => b.confidence - a.confidence || byTime(a, b),
        league: (a, b) => competitionLabel(a.league).localeCompare(competitionLabel(b.league)) || byTime(a, b)
      };
      return scored.sort(sorters[sortBy.value] || byTime);
    });

    const qualifiedMatches = computed(() => matches.value);

    const filteredMatches = computed(() => {
      if (activeFilter.value === 'high') return matches.value.filter(m => m.confidence >= 75);
      if (activeFilter.value === 'combo') return matches.value.filter(m => m.bets.some(b => b.type === 'combo'));
      if (activeFilter.value === 'warn') return matches.value.filter(m => m.lowConfidence);
      return matches.value;
    });

    const stats = computed(() => ({
      scanned: scanMeta.value?.scanned ?? (hasRun.value ? rawMatches.value.length : 0),
      combos: matches.value.filter(m => m.bets.some(b => b.type === 'combo')).length,
      lowConf: matches.value.filter(m => m.lowConfidence).length
    }));

    // ─── BET SLIP ─────────────────────────────────────────────────────────
    // Selections persist under a date-scoped localStorage key. The date is
    // the scan's own `meta.date` once the scan loads (adoptSlipKey in
    // runScan); until then today's EAT date is read provisionally. Restore
    // deletes every other `kanjibai.slip.*` key — a selection from a
    // previous scan date is stale by definition. Storage failures fall back
    // to this in-memory list silently.
    const storage = slipStorage();
    const slipItems = ref([]);
    const slipOpen = ref(false);
    const slipHint = ref('');
    const stake = ref('');
    let slipKeyDate = eatDate();
    let hintTimer = null;

    function readSlip(date) {
      if (!storage) return [];
      try {
        const raw = storage.getItem(SLIP_KEY_PREFIX + date);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        return (Array.isArray(parsed.items) ? parsed.items : []).filter(s =>
          s && s.id !== undefined && typeof s.label === 'string' && isPriced(s));
      } catch (e) {
        return [];  // corrupt entry — start clean, no console noise
      }
    }

    function persistSlip() {
      if (!storage) return;
      try {
        storage.setItem(SLIP_KEY_PREFIX + slipKeyDate,
          JSON.stringify({ date: slipKeyDate, items: slipItems.value }));
      } catch (e) { /* quota/blocked — the in-memory slip still works */ }
    }

    // Move to `date`'s key, restore its slip and delete every other
    // `kanjibai.slip.*` key: selections from a previous scan date are stale.
    function adoptSlipKey(date) {
      slipKeyDate = date;
      slipItems.value = readSlip(date);
      if (!storage) return;
      try {
        for (let i = storage.length - 1; i >= 0; i--) {
          const k = storage.key(i);
          if (k && k.indexOf(SLIP_KEY_PREFIX) === 0 && k !== SLIP_KEY_PREFIX + date) {
            storage.removeItem(k);
          }
        }
      } catch (e) { /* ignore */ }
    }

    // Provisional restore at load, before the scan's meta.date is known.
    // Read-only: cleaning up here could delete a still-valid slip from the
    // previous day while the current scan date is unknown.
    slipItems.value = readSlip(slipKeyDate);

    const slipCount = computed(() => slipItems.value.length);

    function inSlip(matchId, label) {
      return slipItems.value.some(s => s.id === matchId && s.label === label);
    }

    // Brief hint for the same-fixture guard and disabled picks.
    function showSlipHint(msg) {
      slipHint.value = msg;
      if (hintTimer) clearTimeout(hintTimer);
      hintTimer = setTimeout(() => { slipHint.value = ''; }, 2600);
    }

    function toggleSlipPick(match, bet) {
      if (!isPriced(bet)) {
        showSlipHint('No price for this pick — the market is suspended');
        return;
      }
      const existing = slipItems.value.find(s => s.id === match.id);
      if (existing) {
        // Same chip again → toggle off; a different pick on the same
        // fixture is rejected (sportsbooks decline same-match accumulators).
        if (existing.label === bet.label) {
          slipItems.value = slipItems.value.filter(s => s.id !== match.id);
          persistSlip();
        } else {
          showSlipHint(`One pick per fixture — "${existing.label}" is already on the slip`);
        }
        return;
      }
      slipItems.value = slipItems.value.concat([{
        id: match.id, time: match.time, league: match.league,
        home: match.home, away: match.away,
        label: bet.label, type: bet.type, prob: bet.prob, price: bet.price
      }]);
      persistSlip();
    }

    function removeSlipItem(id) {
      slipItems.value = slipItems.value.filter(s => s.id !== id);
      persistSlip();
    }

    function clearSlip() {
      slipItems.value = [];
      persistSlip();
    }

    // ─── STAKE & RETURN (accumulator) ─────────────────────────────────────
    // Total odds = product of the leg prices. An empty or invalid stake
    // clears the return rows rather than rendering NaN.
    const totalOdds = computed(() =>
      slipItems.value.length
        ? slipItems.value.reduce((acc, s) => acc * s.price, 1)
        : null);

    const stakeValue = computed(() => {
      const raw = String(stake.value).trim();
      if (!raw) return null;
      const n = Number(raw);
      return Number.isFinite(n) && n > 0 ? n : null;
    });

    const potentialReturn = computed(() =>
      stakeValue.value !== null && totalOdds.value !== null
        ? stakeValue.value * totalOdds.value
        : null);

    const netProfit = computed(() =>
      potentialReturn.value !== null ? potentialReturn.value - stakeValue.value : null);

    // ─── GENERATE SLIP ────────────────────────────────────────────────────
    // Best priced pick per qualified fixture (highest model probability),
    // top GENERATE_SLIP_MAX_LEGS of them, kick-off order. Merges into the
    // slip: fixtures already covered keep the user's pick.
    const GENERATE_SLIP_MAX_LEGS = 8;

    function generateSlip() {
      const covered = new Set(slipItems.value.map(s => s.id));
      const legs = [];
      for (const m of matches.value) {
        if (covered.has(m.id)) continue;
        const priced = m.bets.filter(isPriced);
        if (!priced.length) continue;
        const best = priced.reduce((a, b) => ((b.prob || 0) > (a.prob || 0)) ? b : a);
        legs.push({
          id: m.id, time: m.time, league: m.league,
          home: m.home, away: m.away,
          label: best.label, type: best.type, prob: best.prob, price: best.price
        });
      }
      const top = legs
        .sort((a, b) => (b.prob || 0) - (a.prob || 0))
        .slice(0, GENERATE_SLIP_MAX_LEGS)
        .sort((a, b) => timeMinutes(a.time) - timeMinutes(b.time));
      if (!top.length) {
        showSlipHint(slipItems.value.length
          ? 'Every fixture on the slip already has a pick'
          : 'No priced picks available today');
        return;
      }
      const kept = slipItems.value.length;
      slipItems.value = slipItems.value.concat(top);
      persistSlip();
      slipOpen.value = true;
      showSlipHint(`Added ${top.length} best pick${top.length === 1 ? '' : 's'}` +
        (kept ? ` · kept ${kept} existing` : ''));
    }

    const steps = [
      ['Loading fixtures...', 'Fetching enriched match data from GitHub'],
      ['Applying odds filter...', `Home ≤ ${filters.homeOddsMax} · Away ≥ ${filters.awayOddsMin}`],
      ['Scoring matches...', 'Running 3-way probability model (Home / Draw / Away)'],
      ['Generating picks...', `Building market recommendations (min confidence ${filters.minPickConfidence}%)`],
    ];

    async function runScan() {
      loading.value = true;

      for (const [step, detail] of steps) {
        loadingStep.value = step;
        loadingDetail.value = detail;
        await new Promise(r => setTimeout(r, 400));
      }

      const data = await loadEnriched();
      rawMatches.value = data.matches || [];
      if (data.meta) scanMeta.value = data.meta;
      // Adopt the scan's date-scoped slip key (restores that date's slip,
      // drops keys from previous scan dates).
      if (data.meta && data.meta.date) adoptSlipKey(data.meta.date);

      loading.value = false;
      hasRun.value = true;
      controlsOpen.value = false;
    }

    function toggleMarket(key) {
      const idx = filters.markets.indexOf(key);
      if (idx > -1) filters.markets.splice(idx, 1);
      else filters.markets.push(key);
    }

    function toggleReasoning(id) {
      const idx = openCards.value.indexOf(id);
      if (idx > -1) openCards.value.splice(idx, 1);
      else openCards.value.push(id);
    }

    function getCompetition(league) {
      if (!league) return '';
      const parts = league.split('/');
      return parts[parts.length - 1].trim();
    }

    // Band for a pick's probability — the same 75 / 55 thresholds the card
    // header's conf-score uses, so both read on one scale.
    function probBand(prob) {
      if (typeof prob !== 'number') return '';
      return prob >= 75 ? 'high' : prob >= 55 ? 'mid' : 'low';
    }

    // Signals split into three columns: home team | match-level | away team
    function signalGroups(match) {
      const groups = [
        { team: 'home', label: match.home, items: [] },
        { team: 'neutral', label: 'Match', items: [] },
        { team: 'away', label: match.away, items: [] }
      ];
      const byTeam = { home: groups[0], neutral: groups[1], away: groups[2] };
      for (const sig of match.signals || []) {
        (byTeam[sig.team] || byTeam.neutral).items.push(sig);
      }
      return groups;
    }

    // League string is "Football / <Country> / <Competition>" → ISO code for flagcdn.com
    const FLAGS = {
      'Albania':'al','Algeria':'dz','Andorra':'ad','Angola':'ao','Argentina':'ar','Armenia':'am','Australia':'au',
      'Austria':'at','Azerbaijan':'az','Bahrain':'bh','Bangladesh':'bd','Belarus':'by','Belgium':'be','Bolivia':'bo',
      'Bosnia and Herzegovina':'ba','Bosnia & Herzegovina':'ba','Botswana':'bw','Brazil':'br','Bulgaria':'bg',
      'Burkina Faso':'bf','Cameroon':'cm','Canada':'ca','Cape Verde':'cv','Chile':'cl','China':'cn','Colombia':'co',
      'Costa Rica':'cr','Croatia':'hr','Cyprus':'cy','Czech Republic':'cz','Czechia':'cz','Denmark':'dk',
      'DR Congo':'cd','Ecuador':'ec','Egypt':'eg','El Salvador':'sv','England':'gb-eng','Estonia':'ee',
      'Ethiopia':'et','Faroe Islands':'fo','Finland':'fi','France':'fr','Georgia':'ge','Germany':'de','Ghana':'gh',
      'Greece':'gr','Guatemala':'gt','Honduras':'hn','Hong Kong':'hk','Hungary':'hu','Iceland':'is','India':'in',
      'Indonesia':'id','Iran':'ir','Iraq':'iq','Ireland':'ie','Israel':'il','Italy':'it','Ivory Coast':'ci',
      'Jamaica':'jm','Japan':'jp','Jordan':'jo','Kazakhstan':'kz','Kenya':'ke','Kosovo':'xk','Kuwait':'kw',
      'Latvia':'lv','Lebanon':'lb','Libya':'ly','Lithuania':'lt','Luxembourg':'lu','Malawi':'mw','Malaysia':'my',
      'Malta':'mt','Mexico':'mx','Moldova':'md','Montenegro':'me','Morocco':'ma','Mozambique':'mz','Namibia':'na',
      'Netherlands':'nl','New Zealand':'nz','Nigeria':'ng','North Macedonia':'mk','Northern Ireland':'gb-nir',
      'Norway':'no','Oman':'om','Panama':'pa','Paraguay':'py','Peru':'pe','Philippines':'ph','Poland':'pl',
      'Portugal':'pt','Qatar':'qa','Romania':'ro','Russia':'ru','Rwanda':'rw','Saudi Arabia':'sa','Scotland':'gb-sct',
      'Senegal':'sn','Serbia':'rs','Singapore':'sg','Slovakia':'sk','Slovenia':'si','South Africa':'za',
      'South Korea':'kr','Spain':'es','Sudan':'sd','Sweden':'se','Switzerland':'ch','Syria':'sy','Tanzania':'tz',
      'Thailand':'th','Tunisia':'tn','Turkey':'tr','Turkiye':'tr','Uganda':'ug','Ukraine':'ua',
      'United Arab Emirates':'ae','Uruguay':'uy','USA':'us','Uzbekistan':'uz','Venezuela':'ve','Vietnam':'vn',
      'Wales':'gb-wls','Zambia':'zm','Zimbabwe':'zw'
    };

    function flagCode(league) {
      return FLAGS[((league || '').split('/')[1] || '').trim()] || '';
    }

    Vue.onMounted(() => { runScan(); });

    return {
      today, clockTime, loading, hasRun, openCards, controlsOpen,
      activeFilter, loadingStep, loadingDetail,
      filters, marketOptions, filterOptions, selectedCompetition, sortBy, sortOptions,
      matches, qualifiedMatches, filteredMatches, stats, scanMeta,
      runScan, toggleMarket, toggleReasoning, getCompetition, flagCode,
      formatTime24, competitionOptions, venueFor, opponentFor, scorelineFor,
      ordinal, posLabel, signalGroups, capturedTime, probBand,
      slipItems, slipOpen, slipHint, stake, slipCount, totalOdds,
      potentialReturn, netProfit, inSlip, isPriced, toggleSlipPick,
      removeSlipItem, clearSlip, generateSlip
    };
  }
}).mount('#app');

}); // DOMContentLoaded
