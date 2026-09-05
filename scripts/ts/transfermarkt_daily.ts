/**
 * transfermarkt_daily.ts — Daily incremental Transfermarkt scraper
 * Processes the queue, respecting rate limits, and populates the cache.
 * Run once daily via GitHub Actions.
 */

import { fetchClubFixtures, fetchTransfermarktProfile, fetchMatchEvents, fetchMatchLineups } from "./transfermarkt.js";
import { 
  loadQueue, saveQueue, loadCache, saveCache,
  getNextBatch, markAttempted, queueTeam, getQueueStats,
  seedQueue, isCached, setCached, getCached
} from "./transfermarkt_queue.js";

const DELAY_MS = 3000; // 3 seconds between requests

async function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function fetchWithRetry(url: string, retries = 2): Promise<Response | null> {
  for (let i = 0; i <= retries; i++) {
    try {
      const res = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" } });
      if (res.ok) return res;
      if (res.status === 429) {
        console.log(`Rate limited, waiting ${(i + 1) * 10}s...`);
        await sleep((i + 1) * 10000);
        continue;
      }
      console.warn(`HTTP ${res.status} for ${url}`);
      if (i < retries) await sleep(5000);
    } catch (e) {
      console.warn(`Fetch error for ${url}: ${e}`);
      if (i < retries) await sleep(5000);
    }
  }
  return null;
}

async function processTeam(teamUrl: string, teamName: string): Promise<boolean> {
  const cache = loadCache();
  const cacheKey = teamUrl.split("/").pop() || teamName.toLowerCase().replace(/[^a-z0-9]/g, "_");
  
  if (isCached(cache, "team", cacheKey)) {
    console.log(`  Already cached: ${teamName}`);
    return true;
  }

  console.log(`  Fetching profile for ${teamName}...`);
  const profileRes = await fetchTransfermarktProfile(teamUrl, fetchWithRetry);
  if (profileRes.sources.length > 0) {
    setCached(cache, "team", cacheKey, { profile: profileRes, name: teamName, url: teamUrl });
    console.log(`  ✓ Cached profile for ${teamName}`);
  }

  await sleep(DELAY_MS);

  // Fetch fixtures for the team
  const fixturesUrl = teamUrl.replace("/profil/", "/spielplan/");
  console.log(`  Fetching fixtures for ${teamName}...`);
  const fixturesRes = await fetchClubFixtures(fixturesUrl, fetchWithRetry);
  if (fixturesRes.fixtures.length > 0) {
    setCached(cache, "fixture", cacheKey, { fixtures: fixturesRes.fixtures, url: fixturesUrl });
    console.log(`  ✓ Cached ${fixturesRes.fixtures.length} fixtures for ${teamName}`);

    // Queue opponent teams that we don't have
    for (const fixture of fixturesRes.fixtures) {
      if (fixture.opponentUrl && !isCached(cache, "team", fixture.opponentUrl.split("/").pop() || "")) {
        queueTeam(fixture.opponent, fixture.opponentUrl, 60);
      }
    }
  }

  await sleep(DELAY_MS);
  return true;
}

async function processLeague(leagueUrl: string, leagueName: string): Promise<boolean> {
  const cache = loadCache();
  const cacheKey = leagueUrl.split("/").pop() || leagueName.toLowerCase().replace(/[^a-z0-9]/g, "_");

  if (isCached(cache, "league", cacheKey)) {
    console.log(`  Already cached: ${leagueName}`);
    return true;
  }

  console.log(`  Fetching league teams from ${leagueName}...`);
  // League page has team links - we'd need to parse the league page
  // For now, just cache the league URL
  setCached(cache, "league", cacheKey, { name: leagueName, url: leagueUrl });
  console.log(`  ✓ Cached league: ${leagueName}`);
  
  // TODO: Parse league page to get team URLs and queue them
  return true;
}

async function processPlayer(playerUrl: string, playerName: string): Promise<boolean> {
  const cache = loadCache();
  const cacheKey = playerUrl.split("/").pop() || playerName.toLowerCase().replace(/[^a-z0-9]/g, "_");
  
  if (isCached(cache, "player", cacheKey)) {
    console.log(`  Already cached: ${playerName}`);
    return true;
  }

  console.log(`  Fetching profile for ${playerName}...`);
  const profileRes = await fetchTransfermarktProfile(playerUrl, fetchWithRetry);
  if (profileRes.sources.length > 0) {
    setCached(cache, "player", cacheKey, { profile: profileRes, name: playerName, url: playerUrl });
    console.log(`  ✓ Cached profile for ${playerName}`);
    return true;
  }
  return false;
}

async function runDaily(): Promise<void> {
  console.log("=".repeat(50));
  console.log(`Transfermarkt Daily Scraper - ${new Date().toISOString()}`);
  console.log("=".repeat(50));

  seedQueue();
  
  const stats = getQueueStats();
  console.log(`Queue stats: ${stats.pending} pending, ${stats.processedToday} processed today, ${stats.remaining} remaining`);

  if (stats.remaining <= 0) {
    console.log("Daily rate limit reached. Exiting.");
    return;
  }

  const batch = getNextBatch(stats.remaining);
  console.log(`Processing ${batch.length} items...`);

  for (const item of batch) {
    let success = false;
    try {
      switch (item.type) {
        case "team":
          success = await processTeam(item.url, item.name);
          break;
        case "league":
          success = await processLeague(item.url, item.name);
          break;
        case "player":
          success = await processPlayer(item.url, item.name);
          break;
      }
    } catch (e) {
      console.error(`Error processing ${item.name}: ${e}`);
      success = false;
    }
    markAttempted(item.id, success);
    
    // Small delay between items
    if (batch.indexOf(item) < batch.length - 1) {
      await sleep(DELAY_MS);
    }
  }

  const finalStats = getQueueStats();
  console.log("=".repeat(50));
  console.log(`Done. Processed ${batch.length} items.`);
  console.log(`Queue: ${finalStats.pending} pending, ${finalStats.remaining} requests remaining today`);
  console.log("=".repeat(50));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  runDaily().catch(console.error);
}