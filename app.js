document.addEventListener('DOMContentLoaded', function() {
const { createApp, ref, computed, reactive } = Vue;

// ─── DATA SOURCE — reads from results.json (written by GitHub Actions) ───────
// Falls back to empty state if file not found (first run / local dev)
async function loadResults() {
  try {
    const resp = await fetch('https://raw.githubusercontent.com/6alaile/kanjibai/main/results.json?t=' + Date.now());
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    return data;
  } catch (e) {
    console.warn('results.json not found or invalid:', e.message);
    return { meta: null, matches: [] };
  }
}

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

    // Live clock in EAT
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
      markets: ['1x2', 'ou', 'btts', 'combo']
    });

    const marketOptions = [
      { key: '1x2', label: '1X2' },
      { key: 'ou', label: 'O/U' },
      { key: 'btts', label: 'BTTS' },
      { key: 'combo', label: 'Combo' }
    ];

    const filterOptions = [
      { key: 'all', label: 'All' },
      { key: 'high', label: 'High Conf (75+)' },
      { key: 'combo', label: 'Has Combo' },
      { key: 'warn', label: 'Low Conf Flag' }
    ];

    const matches = ref([]);

    const qualifiedMatches = computed(() => matches.value);

    const filteredMatches = computed(() => {
      if (activeFilter.value === 'high') return matches.value.filter(m => m.confidence >= 75);
      if (activeFilter.value === 'combo') return matches.value.filter(m => m.bets.some(b => b.type === 'combo'));
      if (activeFilter.value === 'warn') return matches.value.filter(m => m.lowConfidence);
      return matches.value;
    });

    const scanMeta = ref(null);

    const stats = computed(() => ({
      scanned: scanMeta.value?.scanned ?? (hasRun.value ? '—' : 0),
      combos: matches.value.filter(m => m.bets?.some(b => b.type === 'combo')).length,
      lowConf: matches.value.filter(m => m.lowConfidence).length
    }));

    const steps = [
      ['Fetching fixtures...', 'Pulling today\'s matches from SportyBet & BetPawa'],
      ['Applying odds filter...', `Home ≤ ${2.00} · Away ≥ ${5.00}`],
      ['Loading form data...', 'Scraping last 5 matches per team from Sofascore'],
      ['Analysing H2H...', 'Checking head-to-head history & streaks'],
      ['Checking standings...', 'Applying league position weighting'],
      ['Scoring matches...', 'Running signal engine & generating recommendations'],
    ];

    async function runScan() {
      loading.value = true;
      matches.value = [];

      // Show animated steps
      for (const [step, detail] of steps) {
        loadingStep.value = step;
        loadingDetail.value = detail;
        await new Promise(r => setTimeout(r, 500));
      }

      // Load real results.json
      const data = await loadResults();
      const raw = data.matches || [];

      // Apply frontend filter re-check (in case config differs from last scan)
      matches.value = raw.filter(m =>
        m.odds.home <= Number(filters.homeOddsMax) &&
        m.odds.away >= Number(filters.awayOddsMin)
      ).map(m => ({
        ...m,
        // Normalise field names from scorer.py output to dashboard expectations
        homePos: m.homePos ?? m.home_pos ?? null,
        awayPos: m.awayPos ?? m.away_pos ?? null,
        h2hSummary: m.h2hSummary ?? m.h2h_summary ?? '—',
        lowConfidence: m.lowConfidence ?? m.low_confidence ?? false,
        lowConfTeam: m.lowConfTeam ?? m.low_conf_team ?? null,
        homeStats: m.homeStats ?? m.home_stats ?? {},
        awayStats: m.awayStats ?? m.away_stats ?? {},
      }));

      // Pull meta stats if available
      if (data.meta) {
        scanMeta.value = data.meta;
      }

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

    // Auto-load latest results.json on page open
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
