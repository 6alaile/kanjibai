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

function passesOddsFilter(odds, cfg) {
  if (odds.home < cfg.homeOddsMax && odds.away >= cfg.awayOddsMin) return [true, 'home'];
  if (odds.away < cfg.homeOddsMax && odds.home >= cfg.awayOddsMin) return [true, 'away'];
  return [false, ''];
}

function formConfidence(stats, cfg) {
  const n = (stats.form || []).length;
  return n < cfg.minFormMatches;
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
    return { available: false, home_win_rate: 0.5, over_15_rate: 0.5, over_25_rate: 0.5, btts_rate: 0.5, avg_total_goals: 0, summary: 'No H2H data' };
  }
  // H2H arrives newest first; the five most recent meetings are the ones shown.
  const matches = h2h.matches.slice(0, 5);
  const total = matches.length;
  let homeWins = 0, btts = 0;
  const goalTotals = matches.map(m => {
    const [hg, ag] = m.score;
    if ((m.home === homeTeam && hg > ag) || (m.away === homeTeam && ag > hg)) homeWins++;
    if (hg > 0 && ag > 0) btts++;
    return hg + ag;
  });
  const avg = safeAvg(goalTotals);
  return {
    available: true,
    matches: matches,
    home_win_rate: homeWins / total,
    over_15_rate: goalTotals.filter(g => g > 1).length / total,
    over_25_rate: goalTotals.filter(g => g > 2).length / total,
    btts_rate: btts / total,
    avg_total_goals: Math.round(avg * 100) / 100,
    summary: `${homeTeam} W${homeWins} of last ${total} H2H`
  };
}

function positionScore(hp, ap, total, weight) {
  if (!hp || !ap || !total || weight === 0) return 0.5;
  const hn = 1 - ((hp - 1) / (total - 1));
  const an = 1 - ((ap - 1) / (total - 1));
  return Math.round((0.5 + ((hn - an + 1) / 2 - 0.5) * weight) * 1000) / 1000;
}

function market1x2(side, ht, at, h2h) {
  if (side === 'home') {
    const conf = Math.min(95, Math.round((ht.winning_streak/5*0.4 + h2h.home_win_rate*0.3 + ht.wins/5*0.3)*100));
    return { label: 'Home Win', type: 'primary', confidence: Math.max(40, conf), market: ['1x2', 'home'] };
  } else {
    const conf = Math.min(95, Math.round((at.winning_streak/5*0.4 + (1-h2h.home_win_rate)*0.3 + at.wins/5*0.3)*100));
    return { label: 'Away Win', type: 'primary', confidence: Math.max(40, conf), market: ['1x2', 'away'] };
  }
}

function marketDC(side, ht, at, cfg) {
  if (!cfg.markets.includes('dc')) return null;
  if (ht.winning_streak >= 2 && at.winning_streak >= 2) {
    const conf = Math.min(90, Math.round((ht.winning_streak + at.winning_streak) / 10 * 100));
    return { label: '12 (Home or Away)', type: 'secondary', confidence: Math.max(45, conf), market: ['dc', 'home_away'] };
  }
  if (side === 'home' && ht.unbeaten_streak >= 3 && at.losses >= 2) {
    const conf = Math.min(90, Math.round((ht.unbeaten_streak/5*0.5 + at.losses/5*0.5)*100));
    return { label: '1X (Home or Draw)', type: 'secondary', confidence: Math.max(45, conf), market: ['dc', 'home_draw'] };
  }
  if (side === 'away' && at.unbeaten_streak >= 3 && ht.losses >= 2) {
    const conf = Math.min(90, Math.round((at.unbeaten_streak/5*0.5 + ht.losses/5*0.5)*100));
    return { label: 'X2 (Away or Draw)', type: 'secondary', confidence: Math.max(45, conf), market: ['dc', 'draw_away'] };
  }
  return null;
}

function marketOver15(ht, at, h2h, cfg) {
  if (!cfg.markets.includes('over15')) return null;
  const n = ht.n;
  if (!n) return null;
  const score = (
    ht.over_15_count/n*0.20 + at.over_15_count/n*0.20 +
    ht.games_scored_in/n*0.15 + at.games_scored_in/n*0.15 +
    ht.games_conceded_in/n*0.10 + at.games_conceded_in/n*0.10 +
    h2h.over_15_rate*0.10
  );
  if (score >= 0.55) return { label: 'Over 1.5 Goals', type: 'secondary', confidence: Math.min(92, Math.round(score*100)), market: ['total_goals', '1.5', 'over'] };
  return null;
}

function marketBTTS(ht, at, h2h, cfg) {
  if (!cfg.markets.includes('btts')) return null;
  const n = ht.n;
  if (!n) return null;
  const cs = ht.clean_sheets + at.clean_sheets;
  const scoredGames = ht.games_scored_in + at.games_scored_in;
  if (ht.games_scored_in >= 3 && at.games_scored_in >= 3 && ht.games_conceded_in >= 3 && at.games_conceded_in >= 3) {
    const goalsW = Math.min(1, (ht.total_scored + at.total_scored + ht.total_conceded + at.total_conceded) / (n*4));
    const base = (scoredGames/(n*2)*0.35 + (ht.games_conceded_in+at.games_conceded_in)/(n*2)*0.35 + h2h.btts_rate*0.20 + goalsW*0.10);
    return { label: 'BTTS Yes', type: 'secondary', confidence: Math.max(45, Math.min(92, Math.round(base*100))), market: ['btts', 'yes'] };
  }
  if (cs >= scoredGames || ht.clean_sheets >= 2 || at.clean_sheets >= 2) {
    const dom = cs / (scoredGames + 1);
    return { label: 'BTTS No', type: 'warn', confidence: Math.max(40, Math.min(85, Math.round(dom*60)+30)), market: ['btts', 'no'] };
  }
  return null;
}

function marketToScore(ht, at, cfg) {
  if (!cfg.markets.includes('to_score')) return [];
  const n = ht.n;
  if (!n) return [];
  const signals = [];
  if (ht.games_scored_in >= 4) {
    const conf = Math.min(92, Math.round((ht.games_scored_in/n*0.6 + at.games_conceded_in/n*0.3 + (1-at.clean_sheets/n)*0.1)*100));
    signals.push({ label: 'Home to Score (Over 0.5)', type: 'secondary', confidence: Math.max(50, conf), market: ['home_goals', '0.5', 'over'] });
  }
  if (at.games_scored_in >= 4) {
    const conf = Math.min(92, Math.round((at.games_scored_in/n*0.6 + ht.games_conceded_in/n*0.3 + (1-ht.clean_sheets/n)*0.1)*100));
    signals.push({ label: 'Away to Score (Over 0.5)', type: 'secondary', confidence: Math.max(50, conf), market: ['away_goals', '0.5', 'over'] });
  }
  return signals;
}

// The three combo picks: both legs must qualify (confidence ≥ 55 each),
// each priced from BetPawa's combined market.
function marketCombos(mainBet, dc, over15, btts, cfg) {
  if (!cfg.markets.includes('combo')) return [];
  const combos = [];
  const both = (a, b) => a && b && a.confidence >= 55 && b.confidence >= 55;
  const avg = (a, b) => Math.min(88, Math.round((a.confidence + b.confidence) / 2));
  const side = mainBet.market[1]; // home | away (draw once phase 4 lands)

  if (both(mainBet, btts)) {
    combos.push({
      label: `${mainBet.label} + ${btts.label}`,
      type: 'combo', confidence: avg(mainBet, btts),
      market: ['x1x2_btts', `${side}_${btts.market[1]}`]
    });
  }
  if (both(dc, btts)) {
    combos.push({
      label: `${dc.label.slice(0, 2)} + ${btts.label}`,
      type: 'combo', confidence: avg(dc, btts),
      market: ['dc_btts', `${dc.market[1]}_${btts.market[1]}`]
    });
  }
  if (both(mainBet, over15)) {
    combos.push({
      label: `${mainBet.label} + ${over15.label}`,
      type: 'combo', confidence: avg(mainBet, over15),
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

function compositeConfidence(side, ht, at, h2h, pos, lowConfidence) {
  const n = ht.n;
  if (!n) return 30;
  const formSig = side === 'home'
    ? ht.wins/n*0.6 + ht.winning_streak/5*0.4
    : at.wins/n*0.6 + at.winning_streak/5*0.4;
  const h2hSig = side === 'home' ? h2h.home_win_rate : 1 - h2h.home_win_rate;
  const goalsSig = Math.min(1, (ht.total_scored + at.total_scored) / (n*3));
  let raw = formSig*0.40 + pos*0.20 + h2hSig*0.20 + goalsSig*0.20;
  if (lowConfidence) raw *= 0.85;
  return Math.max(30, Math.min(95, Math.round(raw*100)));
}

function scoreMatch(match, cfg) {
  const odds = match.odds;
  const [passes, side] = passesOddsFilter(odds, cfg);
  if (!passes) return {
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
    reason: 'Odds do not meet filter criteria (home odds > ' + cfg.homeOddsMax + ' or away odds < ' + cfg.awayOddsMin + ')',
    bets: [],
    homeStats: {
      name: match.home,
      form: [],
      notes: ['Odds below filtering threshold']
    },
    awayStats: {
      name: match.away,
      form: [],
      notes: ['Odds below filtering threshold']
    },
    signals: []
  };

  const homeStats = match.home_stats || match.homeStats || {};
  const awayStats = match.away_stats || match.awayStats || {};
  const h2hRaw = match.h2h || null;

  const homeLowConf = formConfidence(homeStats, cfg);
  const awayLowConf = formConfidence(awayStats, cfg);
  const lowConfidence = homeLowConf || awayLowConf;
  const lowConfTeam = homeLowConf ? match.home : (awayLowConf ? match.away : null);

  const ht = teamTendency(homeStats);
  const at = teamTendency(awayStats);
  const h2h = analyseH2H(h2hRaw, match.home);
  const pos = positionScore(
    homeStats.league_position, awayStats.league_position,
    match.total_teams_in_league || 20, cfg.leaguePositionWeight
  );

  const bets = [];
  bets.push(market1x2(side, ht, at, h2h));

  const dc = marketDC(side, ht, at, cfg);
  if (dc) bets.push(dc);

  const over15 = marketOver15(ht, at, h2h, cfg);
  if (over15) bets.push(over15);

  const btts = marketBTTS(ht, at, h2h, cfg);
  if (btts) bets.push(btts);

  const toScore = marketToScore(ht, at, cfg);
  toScore.forEach(b => bets.push(b));

  const combos = marketCombos(bets[0], dc, over15, btts, cfg);
  combos.forEach(b => bets.push(b));

  const confidence = compositeConfidence(side, ht, at, h2h, pos, lowConfidence);

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
    recommendation: side,
    reason: '',
    bets: bets.map(b => ({
      label: b.label, type: b.type, market: b.market || null,
      price: betOdds(match.markets, b.market)
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
      markets: ['1x2', 'dc', 'over15', 'btts', 'to_score', 'combo']
    });

    const marketOptions = [
      { key: '1x2', label: '1X2' },
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

    const steps = [
      ['Loading fixtures...', 'Fetching enriched match data from GitHub'],
      ['Applying odds filter...', `Home ≤ ${filters.homeOddsMax} · Away ≥ ${filters.awayOddsMin}`],
      ['Scoring matches...', 'Running signal engine in browser'],
      ['Generating signals...', 'Building market recommendations'],
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
      ordinal, posLabel, signalGroups, capturedTime
    };
  }
}).mount('#app');

}); // DOMContentLoaded
