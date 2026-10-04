import { ApiError } from './errors.js';
import { SEASON_GWS } from './chipRules.js';

const BOOTSTRAP = 'https://fantasy.premierleague.com/api/bootstrap-static/';
let cache = null;

// Returns the next gameweek whose deadline has not passed yet (the one chips apply to).
// gw === null means the season is over (or FPL has no upcoming deadline).
export async function getUpcomingGW(fetchFn = fetch, now = Date.now()) {
  if (cache && now - cache.at < 60_000) return cache.value;
  let data;
  try {
    const res = await fetchFn(BOOTSTRAP, {
      headers: { 'User-Agent': 'Mozilla/5.0 (compatible; El7amla-Worker/1.0)' },
    });
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch {
    throw new ApiError('fpl_unavailable', 503);
  }
  const upcoming = (data.events || [])
    .filter((e) => e.deadline_time && Date.parse(e.deadline_time) > now)
    .sort((a, b) => Date.parse(a.deadline_time) - Date.parse(b.deadline_time))[0];
  const value =
    upcoming && upcoming.id <= SEASON_GWS ? { gw: upcoming.id, deadline: upcoming.deadline_time } : { gw: null, deadline: null };
  cache = { at: now, value };
  return value;
}

export function resetDeadlineCache() {
  cache = null;
}
