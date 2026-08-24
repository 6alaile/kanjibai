document.addEventListener('DOMContentLoaded', function () {
const { createApp, ref, computed, reactive } = Vue;

// ─── FETCH ENRICHED DATA ─────────────────────────────────────────────────────
async function loadEnriched() {
  try {
    const resp = await fetch('/enriched.json?t=' + Date.now());
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } catch (e) {
    console.warn('enriched.json fetch failed:', e.message);
    return { meta: null, matches: [] };
  }
}

// ─── SCORER (ported from scorer.py) ──────────────────────────────────────────

const N = 5;

function cap5(arr) {
  return arr ? arr.slice(-N) : [];
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

function scoringStreak(goalsScored) {
  let streak = 0;
  const arr = cap5(goalsScored || []);
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] > 0) streak++; else break;
  }
  return streak;
}

function concedingStreak(goalsConceded) {
  let streak = 0;
  const arr = cap5(goalsConceded || []);
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] > 0) streak++; else break;
  }
  return streak;
}

function winningStreak(form) {
  let streak = 0;
  const arr = cap5(form || []);
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] === 'W') streak++; else break;
  }
  return streak;
}

function unbeatenStreak(form) {
  let streak = 0;
  const arr = cap5(form || []);
  for (let i = arr.length - 1; i >= 0; i--) {
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
  const matches = h2h.matches.slice(-5);
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
    return { label: 'Home Win', type: 'primary', confidence: Math.max(40, conf) };
  } else {
    const conf = Math.min(95, Math.round((at.winning_streak/5*0.4 + (1-h2h.home_win_rate)*0.3 + at.wins/5*0.3)*100));
    return { label: 'Away Win', type: 'primary', confidence: Math.max(40, conf) };
  }
}

function marketDC(side, ht, at, cfg) {
  if (!cfg.markets.includes('dc')) return null;
  if (ht.winning_streak >= 2 && at.winning_streak >= 2) {
    const conf = Math.min(90, Math.round((ht.winning_streak + at.winning_streak) / 10 * 100));
    return { label: '12 (Home or Away)', type: 'secondary', confidence: Math.max(45, conf) };
  }
  if (side === 'home' && ht.unbeaten_streak >= 3 && at.losses >= 2) {
    const conf = Math.min(90, Math.round((ht.unbeaten_streak/5*0.5 + at.losses/5*0.5)*100));
    return { label: '1X (Home or Draw)', type: 'secondary', confidence: Math.max(45, conf) };
  }
  if (side === 'away' && at.unbeaten_streak >= 3 && ht.losses >= 2) {
    const conf = Math.min(90, Math.round((at.unbeaten_streak/5*0.5 + ht.losses/5*0.5)*100));
    return { label: 'X2 (Away or Draw)', type: 'secondary', confidence: Math.max(45, conf) };
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
  if (score >= 0.55) return { label: 'Over 1.5 Goals', type: 'secondary', confidence: Math.min(92, Math.round(score*100)) };
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
    return { label: 'BTTS Yes', type: 'secondary', confidence: Math.max(45, Math.min(92, Math.round(base*100))) };
  }
  if (cs >= scoredGames || ht.clean_sheets >= 2 || at.clean_sheets >= 2) {
    const dom = cs / (scoredGames + 1);
    return { label: 'BTTS No', type: 'warn', confidence: Math.max(40, Math.min(85, Math.round(dom*60)+30)) };
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
    signals.push({ label: 'Home to Score (Over 0.5)', type: 'secondary', confidence: Math.max(50, conf) });
  }
  if (at.games_scored_in >= 4) {
    const conf = Math.min(92, Math.round((at.games_scored_in/n*0.6 + ht.games_conceded_in/n*0.3 + (1-ht.clean_sheets/n)*0.1)*100));
    signals.push({ label: 'Away to Score (Over 0.5)', type: 'secondary', confidence: Math.max(50, conf) });
  }
  return signals;
}

function marketBTTSOver25(ht, at, h2h, cfg) {
  if (!cfg.markets.includes('btts_over25')) return null;
  const n = ht.n;
  if (!n) return null;
  if (ht.games_scored_in < 3 || at.games_scored_in < 3) return null;
  if (ht.games_conceded_in < 3 || at.games_conceded_in < 3) return null;
  const totalGoals = ht.total_scored + at.total_scored + ht.total_conceded + at.total_conceded;
  const gpg = totalGoals / (n * 2);
  const score = (ht.over_25_count/n*0.25 + at.over_25_count/n*0.25 + Math.min(1, gpg/3)*0.30 + h2h.over_25_rate*0.20);
  if (score >= 0.50) return { label: 'BTTS + Over 2.5', type: 'combo', confidence: Math.max(45, Math.min(90, Math.round(score*100))) };
  return null;
}

function marketCombos(side, ht, at, over15, btts, toScore, cfg) {
  if (!cfg.markets.includes('combo')) return [];
  const combos = [];
  if (side === 'away' && over15 && over15.confidence >= 60)
    combos.push({ label: 'Away Win + Over 1.5', type: 'combo', confidence: Math.min(88, Math.round((over15.confidence+60)/2)) });
  if (side === 'home' && over15 && over15.confidence >= 60 && ht.unbeaten_streak >= 2)
    combos.push({ label: '1X + Over 1.5', type: 'combo', confidence: Math.min(88, Math.round((over15.confidence+65)/2)) });
  if (btts && btts.label === 'BTTS Yes' && btts.confidence >= 55) {
    toScore.forEach(ts => {
      if (ts.confidence >= 55)
        combos.push({ label: `${ts.label} + BTTS Yes`, type: 'combo', confidence: Math.min(88, Math.round((btts.confidence+ts.confidence)/2)) });
    });
  }
  return combos;
}

function generateSignals(home, away, ht, at, h2h, side) {
  const signals = [];
  const sig = (text, type = '') => signals.push({ text, type });
  if (ht.winning_streak >= 3) sig(`${home} on ${ht.winning_streak}-game winning streak`);
  if (at.winning_streak >= 3) sig(`${away} on ${at.winning_streak}-game winning streak`);
  if (ht.scoring_streak >= 4) sig(`${home} scored in last ${ht.scoring_streak} matches`, 'blue');
  if (at.scoring_streak >= 4) sig(`${away} scored in last ${at.scoring_streak} matches`, 'blue');
  if (at.conceding_streak >= 4) sig(`${away} conceded in last ${at.conceding_streak} matches`, 'blue');
  if (ht.conceding_streak >= 4) sig(`${home} conceded in last ${ht.conceding_streak} matches`, 'blue');
  if (ht.clean_sheets >= 3) sig(`${home} kept ${ht.clean_sheets} clean sheets in last 5`, 'warn');
  if (at.clean_sheets >= 3) sig(`${away} kept ${at.clean_sheets} clean sheets in last 5`, 'warn');
  sig(`${home} last 5: ${ht.wins}W ${ht.draws}D ${ht.losses}L`);
  sig(`${away} last 5: ${at.wins}W ${at.draws}D ${at.losses}L`);
  if (h2h.available) sig(`H2H: ${h2h.summary} — avg ${h2h.avg_total_goals} goals`, 'blue');
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
  if (!passes) return null;

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

  const bttsOver25 = marketBTTSOver25(ht, at, h2h, cfg);
  if (bttsOver25) bets.push(bttsOver25);

  const toScore = marketToScore(ht, at, cfg);
  toScore.forEach(b => bets.push(b));

  const combos = marketCombos(side, ht, at, over15, btts, toScore, cfg);
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
    h2hSummary: h2h.summary,
    odds: { home: odds.home, draw: odds.draw, away: odds.away },
    confidence,
    lowConfidence,
    lowConfTeam,
    recommendation: side,
    bets: bets.map(b => ({ label: b.label, type: b.type })),
    homeStats: {
      name: match.home,
      form: cap5(homeStats.form || []),
      notes: buildTeamNotes(match.home, ht)
    },
    awayStats: {
      name: match.away,
      form: cap5(awayStats.form || []),
      notes: buildTeamNotes(match.away, at)
    },
    signals: generateSignals(match.home, match.away, ht, at, h2h, side)
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

    const filters = reactive({
      homeOddsMax: 2.00,
      awayOddsMin: 5.00,
      minFormMatches: 5,
      leaguePositionWeight: 0.5,
      oppStrengthWeight: 0.5,
      markets: ['1x2', 'dc', 'over15', 'btts', 'to_score', 'btts_over25', 'combo']
    });

    const marketOptions = [
      { key: '1x2', label: '1X2' },
      { key: 'dc', label: 'Double Chance' },
      { key: 'over15', label: 'Over 1.5' },
      { key: 'btts', label: 'BTTS' },
      { key: 'to_score', label: 'To Score' },
      { key: 'btts_over25', label: 'BTTS+O2.5' },
      { key: 'combo', label: 'Combos' }
    ];

    const filterOptions = [
      { key: 'all', label: 'All' },
      { key: 'high', label: 'High Conf (75+)' },
      { key: 'combo', label: 'Has Combo' },
      { key: 'warn', label: 'Low Conf Flag' }
    ];

    // Scored matches — recomputed whenever filters change
    const matches = computed(() => scoreAll(rawMatches.value, filters));

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

    Vue.onMounted(() => { runScan(); });

    return {
      today, clockTime, loading, hasRun, openCards, controlsOpen,
      activeFilter, loadingStep, loadingDetail,
      filters, marketOptions, filterOptions,
      matches, qualifiedMatches, filteredMatches, stats, scanMeta,
      runScan, toggleMarket, toggleReasoning
    };
  }
}).mount('#app');

}); // DOMContentLoaded
