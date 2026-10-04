// chipRules.js
// ─────────────────────────────────────────────
// Pure logic for El7amla Chip activation.
// No fetch, no Date.now() side effects taken internally except where a
// clock value is explicitly passed in — this keeps every function
// deterministic and unit-testable without mocking network or time.
//
// This mirrors (and must stay in sync with) the scoring rules already
// implemented in scripts/update_standings.py:
//   - half_key(gw): "h1" if gw <= 19 else "h2"
//   - get_chip_slot(): a slot only counts for scoring if status === "used"
//   - bonus3 is single-use per season (not per half)
//   - double_player doubles one player's points and zeroes the other
//   - "borrow" is permanently disabled
// ─────────────────────────────────────────────

export const VALID_CHIP_KEYS = ["one_v_one", "bonus3", "double_player"];
export const DISABLED_CHIP_KEYS = ["borrow"];

export const SEASON_MAX_GW = 35;
export const HALF_SPLIT_GW = 19; // GW1-19 = h1, GW20-35 = h2 (matches update_standings.py)

export class ChipValidationError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

/**
 * GW1-19 -> "h1", GW20-35 -> "h2"
 * Matches scripts/update_standings.py half_key() exactly.
 */
export function computeHalf(gw) {
  if (!Number.isInteger(gw) || gw < 1 || gw > SEASON_MAX_GW) {
    throw new ChipValidationError(
      "invalid_gw",
      `GW ${gw} is outside the current season (1-${SEASON_MAX_GW})`
    );
  }
  return gw <= HALF_SPLIT_GW ? "h1" : "h2";
}

/**
 * Returns the blank per-team chip record shape, used when a team has no
 * prior entry in chips.json yet.
 */
export function blankTeamChips() {
  return {
    one_v_one: { h1: null, h2: null },
    bonus3: { used: false, gw: null, won: null, status: null },
    double_player: { h1: null, h2: null },
    borrow: { h1: null, h2: null },
    activeThisGW: null,
  };
}

/**
 * Find the team scheduled against `team` in fixtures[gw].
 * fixtures shape: { "7": [["Black Tech","Falcons"], ["The Pharaohs","Boys"], ...], ... }
 * Returns null if team has a BYE or no fixture that GW.
 */
export function findScheduledOpponent(fixtures, team, gw) {
  const matchups = fixtures?.[String(gw)] || fixtures?.[gw] || [];
  for (const pair of matchups) {
    if (!Array.isArray(pair) || pair.length < 2) continue;
    const [home, away] = pair;
    if (home === team && away !== "BYE") return away;
    if (away === team && home !== "BYE") return home;
  }
  return null;
}

/**
 * Returns the roster (array of player names) for a team, from league.json's
 * shape: { teams: { "TeamName": { players: { "PlayerName": entryId, ... } } } }
 */
export function rosterFor(league, team) {
  const players = league?.teams?.[team]?.players;
  if (!players) return [];
  return Object.keys(players);
}

/**
 * Core validation + activation builder. Does not mutate its inputs.
 *
 * @param {object} args
 * @param {object} args.chipsState   full chips.json content (all teams)
 * @param {object} args.fixtures     fixtures.json's `fixtures` map
 * @param {object} args.league       league.json content
 * @param {string} args.team         activating team name
 * @param {string} args.chipKey      "one_v_one" | "bonus3" | "double_player" | "borrow"
 * @param {number} args.gw           gameweek being activated for
 * @param {object} args.payload      chip-specific fields (see below)
 * @param {string} args.nowIso       ISO timestamp of "now" (server clock)
 * @param {string} args.deadlineIso  ISO timestamp of the GW deadline being enforced
 *
 * one_v_one payload:   { myPlayer, oppPlayer }
 * double_player payload: { doubledPlayer, zeroedPlayer }
 * bonus3 payload:      {} (no fields required)
 *
 * @returns {{ nextChipsState: object, activatedSlot: object }}
 * @throws {ChipValidationError}
 */
export function validateAndBuildActivation({
  chipsState,
  fixtures,
  league,
  team,
  chipKey,
  gw,
  payload = {},
  nowIso,
  deadlineIso,
}) {
  if (DISABLED_CHIP_KEYS.includes(chipKey)) {
    throw new ChipValidationError("chip_disabled", `${chipKey} is permanently disabled`);
  }
  if (!VALID_CHIP_KEYS.includes(chipKey)) {
    throw new ChipValidationError("unknown_chip", `Unknown chip: ${chipKey}`);
  }
  if (!team || !league?.teams?.[team]) {
    throw new ChipValidationError("unknown_team", `Unknown team: ${team}`);
  }

  // Deadline check — server clock only, never trust a client-supplied "now".
  if (new Date(nowIso).getTime() >= new Date(deadlineIso).getTime()) {
    throw new ChipValidationError(
      "deadline_passed",
      `GW${gw} deadline (${deadlineIso}) has passed`
    );
  }

  const half = computeHalf(gw); // throws invalid_gw if out of range

  const state = structuredCloneLike(chipsState);
  if (!state[team]) state[team] = blankTeamChips();

  const activatedAt = nowIso;
  const deadlineUsed = deadlineIso;

  if (chipKey === "bonus3") {
    if (state[team].bonus3?.used) {
      throw new ChipValidationError("already_used", "bonus3 already used this season");
    }
    const slot = { used: true, gw, won: null, status: "used", activatedAt, deadlineUsed };
    state[team].bonus3 = slot;
    state[team].activeThisGW = { key: "bonus3", gw };
    return { nextChipsState: state, activatedSlot: slot };
  }

  // one_v_one and double_player share the per-half-slot shape.
  const existing = state[team][chipKey]?.[half];
  if (existing && existing.status) {
    throw new ChipValidationError(
      "already_used",
      `${chipKey} already used in this half (GW${existing.gw})`
    );
  }

  if (chipKey === "one_v_one") {
    const { myPlayer, oppPlayer } = payload;
    if (!myPlayer || !oppPlayer) {
      throw new ChipValidationError("missing_fields", "myPlayer and oppPlayer are required");
    }

    const oppTeam = findScheduledOpponent(fixtures, team, gw);
    if (!oppTeam) {
      throw new ChipValidationError("no_fixture", `${team} has no fixture (or a BYE) in GW${gw}`);
    }

    const myRoster = rosterFor(league, team);
    if (!myRoster.includes(myPlayer)) {
      throw new ChipValidationError("invalid_player", `${myPlayer} is not on ${team}`);
    }
    const oppRoster = rosterFor(league, oppTeam);
    if (!oppRoster.includes(oppPlayer)) {
      throw new ChipValidationError("invalid_player", `${oppPlayer} is not on ${oppTeam}`);
    }

    // Guard against the known "mutual activation" dead-outcome: if the
    // scheduled opponent has ALREADY activated their own one_v_one against
    // this exact fixture/GW, the scorer (get_1v1_duel_for_match) cancels
    // BOTH activations. Reject proactively rather than letting a team burn
    // one of their two season uses on a guaranteed cancellation.
    const oppSlot = state[oppTeam]?.[chipKey]?.[half];
    if (oppSlot && oppSlot.gw === gw && oppSlot.oppTeam === team && oppSlot.status === "used") {
      throw new ChipValidationError(
        "mutual_conflict",
        `${oppTeam} already activated one_v_one against ${team} for GW${gw}; ` +
          `both would be canceled by the scorer. Ask them to hold off, or coordinate first.`
      );
    }

    const slot = {
      gw,
      myPlayer,
      oppTeam,
      oppPlayer,
      status: "used",
      activatedAt,
      deadlineUsed,
    };
    state[team].one_v_one[half] = slot;
    state[team].activeThisGW = { key: "one_v_one", gw };
    return { nextChipsState: state, activatedSlot: slot };
  }

  if (chipKey === "double_player") {
    const { doubledPlayer, zeroedPlayer } = payload;
    if (!doubledPlayer || !zeroedPlayer) {
      throw new ChipValidationError(
        "missing_fields",
        "doubledPlayer and zeroedPlayer are required"
      );
    }
    if (doubledPlayer === zeroedPlayer) {
      throw new ChipValidationError("invalid_player", "doubledPlayer and zeroedPlayer must differ");
    }
    const myRoster = rosterFor(league, team);
    if (!myRoster.includes(doubledPlayer)) {
      throw new ChipValidationError("invalid_player", `${doubledPlayer} is not on ${team}`);
    }
    if (!myRoster.includes(zeroedPlayer)) {
      throw new ChipValidationError("invalid_player", `${zeroedPlayer} is not on ${team}`);
    }

    const slot = {
      gw,
      doubledPlayer,
      zeroedPlayer,
      status: "used",
      activatedAt,
      deadlineUsed,
    };
    state[team].double_player[half] = slot;
    state[team].activeThisGW = { key: "double_player", gw };
    return { nextChipsState: state, activatedSlot: slot };
  }

  // Unreachable given the VALID_CHIP_KEYS check above.
  throw new ChipValidationError("unknown_chip", `Unhandled chip: ${chipKey}`);
}

/**
 * Small structuredClone shim so this file has zero runtime dependencies
 * and works identically in Node (tests) and the Workers runtime (which
 * also has structuredClone natively — this just guards older/edge cases).
 */
function structuredCloneLike(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}
