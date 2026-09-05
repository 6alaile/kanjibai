/**
 * transfermarkt_queue.ts — Queue manager for Transfermarkt scraping
 * Maintains a queue of teams/leagues to scrape, skipping already-cached items.
 * Designed for daily incremental runs with rate limiting.
 */

import { readFileSync, writeFileSync, existsSync } from "fs";
import { join } from "path";

const CACHE_FILE = join(__dirname, "..", "..", "data", "transfermarkt_cache.json");
const QUEUE_FILE = join(__dirname, "transfermarkt_queue.json");
const MAX_REQUESTS_PER_DAY = 100; // Transfermarkt rate limit safety margin

export type QueueItem = {
  id: string;           // unique identifier (e.g., "team:123" or "league:eng1")
  type: "team" | "league" | "player";
  name: string;
  url: string;
  priority: number;     // lower = higher priority
  addedAt: string;
  attempts: number;
  lastAttempt?: string;
  lastSuccess?: string;
};

export type QueueState = {
  queue: QueueItem[];
  processedToday: number;
  lastReset: string;    // date when daily counter was reset
};

function loadQueue(): QueueState {
  if (existsSync(QUEUE_FILE)) {
    return JSON.parse(readFileSync(QUEUE_FILE, "utf-8"));
  }
  return { queue: [], processedToday: 0, lastReset: new Date().toISOString().split("T")[0] };
}

function saveQueue(state: QueueState): void {
  writeFileSync(QUEUE_FILE, JSON.stringify(state, null, 2));
}

export function loadCache(): any {
  if (existsSync(CACHE_FILE)) {
    return JSON.parse(readFileSync(CACHE_FILE, "utf-8"));
  }
  return { version: 1, lastUpdated: new Date().toISOString(), teams: {}, players: {}, fixtures: {}, leagues: {} };
}

export function saveCache(cache: any): void {
  cache.lastUpdated = new Date().toISOString();
  writeFileSync(CACHE_FILE, JSON.stringify(cache, null, 2));
}

export function isCached(cache: any, type: "team" | "player" | "fixture" | "league", key: string): boolean {
  return !!cache[type + "s"][key];
}

export function getCached(cache: any, type: "team" | "player" | "fixture" | "league", key: string): any {
  return cache[type + "s"][key];
}

export function setCached(cache: any, type: "team" | "player" | "fixture" | "league", key: string, data: any): void {
  cache[type + "s"][key] = { data, fetchedAt: new Date().toISOString() };
  saveCache(cache);
}

/**
 * Add a team to the scrape queue if not already cached
 */
export function queueTeam(teamName: string, teamUrl: string, priority = 50): void {
  const state = loadQueue();
  const id = `team:${teamUrl.split("/").pop()}`;

  if (state.queue.some(q => q.id === id)) return;

  state.queue.push({
    id,
    type: "team",
    name: teamName,
    url: teamUrl,
    priority,
    addedAt: new Date().toISOString(),
    attempts: 0,
  });
  state.queue.sort((a, b) => a.priority - b.priority);
  saveQueue(state);
}

/**
 * Add a league to the scrape queue
 */
export function queueLeague(leagueName: string, leagueUrl: string, priority = 10): void {
  const state = loadQueue();
  const id = `league:${leagueUrl.split("/").pop()}`;

  if (state.queue.some(q => q.id === id)) return;

  state.queue.push({
    id,
    type: "league",
    name: leagueName,
    url: leagueUrl,
    priority,
    addedAt: new Date().toISOString(),
    attempts: 0,
  });
  state.queue.sort((a, b) => a.priority - b.priority);
  saveQueue(state);
}

/**
 * Get next items to process (respecting daily rate limit)
 */
export function getNextBatch(batchSize = 20): QueueItem[] {
  const state = loadQueue();
  const today = new Date().toISOString().split("T")[0];

  // Reset daily counter if new day
  if (state.lastReset !== today) {
    state.processedToday = 0;
    state.lastReset = today;
  }

  const remaining = Math.max(0, MAX_REQUESTS_PER_DAY - state.processedToday);
  const toProcess = Math.min(batchSize, remaining, state.queue.length);

  const batch = state.queue.splice(0, toProcess);
  state.processedToday += batch.length;
  saveQueue(state);

  return batch;
}

/**
 * Mark queue item as attempted (success or failure)
 */
export function markAttempted(id: string, success: boolean): void {
  const state = loadQueue();
  const item = state.queue.find(q => q.id === id);
  if (item) {
    item.attempts++;
    item.lastAttempt = new Date().toISOString();
    if (success) {
      item.lastSuccess = new Date().toISOString();
      // Re-queue with lower priority for periodic refresh (weekly)
      const requeueItem: QueueItem = {
        ...item,
        priority: item.priority + 100, // lower priority for refresh
        attempts: 0,
      };
      state.queue.push(requeueItem);
      state.queue.sort((a, b) => a.priority - b.priority);
    } else if (item.attempts >= 3) {
      // Max retries reached, move to end with very low priority
      item.priority = 1000;
      state.queue.push(item);
      state.queue.sort((a, b) => a.priority - b.priority);
    } else {
      // Re-queue for retry
      state.queue.push(item);
      state.queue.sort((a, b) => a.priority - b.priority);
    }
  }
  saveQueue(state);
}

/**
 * Initialize queue with seed leagues
 */
export function seedQueue(): void {
  const state = loadQueue();
  if (state.queue.length > 0) return; // Already seeded

  // Major European leagues - Transfermarkt league IDs
  const seedLeagues = [
    { name: "Premier League", url: "https://www.transfermarkt.com/premier-league/startseite/wettbewerb/GB1" },
    { name: "La Liga", url: "https://www.transfermarkt.com/laliga/startseite/wettbewerb/ES1" },
    { name: "Bundesliga", url: "https://www.transfermarkt.com/bundesliga/startseite/wettbewerb/L1" },
    { name: "Serie A", url: "https://www.transfermarkt.com/serie-a/startseite/wettbewerb/IT1" },
    { name: "Ligue 1", url: "https://www.transfermarkt.com/ligue-1/startseite/wettbewerb/FR1" },
    { name: "Eredivisie", url: "https://www.transfermarkt.com/eredivisie/startseite/wettbewerb/NL1" },
    { name: "Primeira Liga", url: "https://www.transfermarkt.com/primeira-liga/startseite/wettbewerb/PO1" },
    { name: "Championship", url: "https://www.transfermarkt.com/championship/startseite/wettbewerb/GB2" },
    { name: "Segunda Division", url: "https://www.transfermarkt.com/segunda-division/startseite/wettbewerb/ES2" },
    { name: "2. Bundesliga", url: "https://www.transfermarkt.com/2-bundesliga/startseite/wettbewerb/L2" },
  ];

  for (const league of seedLeagues) {
    queueLeague(league.name, league.url, 10);
  }
}

/**
 * Get queue stats for monitoring
 */
export function getQueueStats(): { pending: number; processedToday: number; remaining: number } {
  const state = loadQueue();
  const today = new Date().toISOString().split("T")[0];
  const processed = state.lastReset === today ? state.processedToday : 0;
  return {
    pending: state.queue.length,
    processedToday: processed,
    remaining: Math.max(0, MAX_REQUESTS_PER_DAY - processed),
  };
}

/**
 * Populate queue from today's fixtures (teams playing today)
 */
export function queueFromFixtures(fixtures: any[]): void {
  const state = loadQueue();
  const cache = loadCache();

  for (const fixture of fixtures) {
    for (const teamKey of ["home", "away"]) {
      const team = fixture[teamKey];
      if (!team) continue;

      // Create a cache key from team name
      const cacheKey = team.toLowerCase().replace(/[^a-z0-9]/g, "_");
      if (isCached(cache, "team", cacheKey)) continue;

      // We'd need the Transfermarkt URL - for now skip if not cached
      // In practice, we'd search for the team URL or maintain a mapping
      // queueTeam(team, `https://www.transfermarkt.com/${team}/profil/verein/...`);
    }
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  // CLI usage
  const command = process.argv[2];
  if (command === "seed") {
    seedQueue();
    console.log("Queue seeded with major leagues");
  } else if (command === "stats") {
    console.log(getQueueStats());
  } else if (command === "next") {
    console.log(getNextBatch(5));
  }
}