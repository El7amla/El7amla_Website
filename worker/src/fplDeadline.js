// fplDeadline.js
// ─────────────────────────────────────────────
// Pure logic for turning an FPL bootstrap-static payload + a clock value
// into "what GW are we activating for, and when is its deadline". No
// fetch() here — the Worker fetches the bootstrap JSON and passes it in,
// which keeps this testable without mocking the network.
// ─────────────────────────────────────────────

import { SEASON_MAX_GW } from "./chipRules.js";

export class DeadlineResolutionError extends Error {
  constructor(message) {
    super(message);
    this.code = "deadline_unresolvable";
  }
}

/**
 * @param {object} bootstrap  parsed FPL bootstrap-static JSON ({ events: [...] })
 * @param {number} nowMs      current time in ms (server clock, e.g. Date.now())
 * @returns {{ gw: number, deadlineIso: string }}
 */
export function resolveCurrentGWAndDeadline(bootstrap, nowMs) {
  const events = Array.isArray(bootstrap?.events) ? bootstrap.events : [];
  if (events.length === 0) {
    throw new DeadlineResolutionError("FPL bootstrap payload has no events");
  }

  // Hard season boundary: GW36-38 (or any real-world FPL GW beyond our
  // league's 35) must never be considered at all here, not even as a
  // fallback. Filtering BEFORE picking "nearest upcoming" is what prevents
  // a post-season real FPL deadline from leaking in as if it were GW35's —
  // Math.min()-capping the id *after* the fact is not enough, because the
  // deadline value itself would still belong to the wrong (out-of-season)
  // event.
  const withDeadlines = events
    .filter((e) => e && e.deadline_time)
    .map((e) => ({ id: Number(e.id), deadline: e.deadline_time }))
    .filter((e) => Number.isFinite(e.id) && e.id >= 1 && e.id <= SEASON_MAX_GW);

  if (withDeadlines.length === 0) {
    throw new DeadlineResolutionError(
      `FPL bootstrap payload has no events within the season range (1-${SEASON_MAX_GW})`
    );
  }

  const upcoming = withDeadlines
    .filter((e) => new Date(e.deadline).getTime() > nowMs)
    .sort((a, b) => new Date(a.deadline).getTime() - new Date(b.deadline).getTime());

  let chosen;
  if (upcoming.length > 0) {
    chosen = upcoming[0];
  } else {
    // Every in-season deadline (GW1-35) has passed — the league's season is
    // over. Fall back to GW35's own deadline (already in the past), so any
    // activation attempt fails cleanly on "deadline passed" using a deadline
    // that actually belongs to our season, rather than crashing or borrowing
    // a real FPL GW36+ deadline that has nothing to do with this league.
    const sorted = [...withDeadlines].sort((a, b) => a.id - b.id);
    chosen = sorted[sorted.length - 1];
  }

  if (!chosen) {
    throw new DeadlineResolutionError("Could not resolve any in-season GW with a deadline");
  }

  // No Math.min() cap needed anymore — `chosen` was already filtered to the
  // season range above, so chosen.id is guaranteed to be <= SEASON_MAX_GW.
  return { gw: chosen.id, deadlineIso: chosen.deadline };
}
