import { ApiError } from './errors.js';

export const FIRST_HALF_LAST_GW = 19; // first half GW1-19, second half GW20-35
export const SEASON_GWS = 35;
export const CHIP_KEYS = ['one_v_one', 'bonus3', 'double_player'];

export const halfOfGW = (gw) => (gw <= FIRST_HALF_LAST_GW ? 1 : 2);
export const halfKey = (gw) => `h${halfOfGW(gw)}`;

export function blankTeamChips() {
  return {
    one_v_one: { h1: null, h2: null },
    bonus3: { used: false, gw: null, won: null, status: null },
    double_player: { h1: null, h2: null },
    borrow: { h1: null, h2: null },
    activeThisGW: null,
  };
}

export function normalizeTeamChips(tc) {
  const b = blankTeamChips();
  const t = tc && typeof tc === 'object' ? tc : {};
  return {
    ...t,
    one_v_one: { ...b.one_v_one, ...(t.one_v_one || {}) },
    bonus3: { ...b.bonus3, ...(t.bonus3 || {}) },
    double_player: { ...b.double_player, ...(t.double_player || {}) },
    borrow: { ...b.borrow, ...(t.borrow || {}) },
    activeThisGW: t.activeThisGW ?? null,
  };
}

export function findOpponent(fixtures, team, gw) {
  for (const m of (fixtures || {})[String(gw)] || []) {
    if (m[0] === team) return m[1] === 'BYE' ? null : m[1];
    if (m[1] === team) return m[0] === 'BYE' ? null : m[0];
  }
  return null;
}

const slotFor = (tc, chip, gw) => (chip === 'bonus3' ? (tc.bonus3.used ? tc.bonus3 : null) : tc[chip][halfKey(gw)] || null);

export function chipStatuses(rawTc, gw, locked) {
  const tc = normalizeTeamChips(rawTc);
  const active = tc.activeThisGW && tc.activeThisGW.gw === gw ? tc.activeThisGW.key : null;
  const out = {};
  for (const key of CHIP_KEYS) {
    const slot = slotFor(tc, key, gw);
    let status = 'available';
    if (slot) status = 'used';
    else if (active) status = 'blocked';
    else if (locked) status = 'locked';
    const per = key === 'bonus3' ? [tc.bonus3.used] : [tc[key].h1, tc[key].h2];
    out[key] = {
      status,
      gw: slot ? slot.gw : null,
      count: per.filter(Boolean).length,
      max: key === 'bonus3' ? 1 : 2,
    };
  }
  return out;
}

export function listActivations(rawTc) {
  const tc = normalizeTeamChips(rawTc);
  const rows = [];
  const add = (chip, s) => s && rows.push({ chip, gw: s.gw, myPlayer: s.myPlayer, oppTeam: s.oppTeam, oppPlayer: s.oppPlayer, doubledPlayer: s.doubledPlayer, status: s.status });
  add('one_v_one', tc.one_v_one.h1);
  add('one_v_one', tc.one_v_one.h2);
  if (tc.bonus3.used) add('bonus3', tc.bonus3);
  add('double_player', tc.double_player.h1);
  add('double_player', tc.double_player.h2);
  return rows.sort((a, b) => a.gw - b.gw);
}

// Pure: returns a new chips object. Throws ApiError with a stable code on any rule violation.
export function applyActivation({ chips, team, gw, chip, params = {}, league, fixtures, now = new Date() }) {
  if (!CHIP_KEYS.includes(chip)) throw new ApiError('unknown_chip');
  const leagueTeam = league?.teams?.[team];
  if (!leagueTeam) throw new ApiError('unknown_team', 404);
  const opponent = findOpponent(fixtures, team, gw);
  if (!opponent) throw new ApiError('no_match', 409);

  const next = structuredClone(chips && typeof chips === 'object' ? chips : {});
  const tc = normalizeTeamChips(next[team]);
  if (tc.activeThisGW && tc.activeThisGW.gw === gw) throw new ApiError('chip_this_gw', 409);
  if (slotFor(tc, chip, gw)) throw new ApiError('already_used', 409);

  const mine = Object.keys(leagueTeam.players || {});
  const theirs = Object.keys(league.teams[opponent]?.players || {});
  const stamp = { gw, status: 'used', activatedAt: now.toISOString() };
  let warning = null;

  if (chip === 'one_v_one') {
    const { myPlayer, oppPlayer } = params;
    if (!mine.includes(myPlayer) || !theirs.includes(oppPlayer)) throw new ApiError('invalid_player');
    tc.one_v_one[halfKey(gw)] = { ...stamp, myPlayer, oppTeam: opponent, oppPlayer };
    const o = next[opponent]?.one_v_one?.[halfKey(gw)];
    if (o && o.gw === gw && o.status === 'used') warning = 'opponent_1v1_active';
  } else if (chip === 'double_player') {
    const { myPlayer } = params;
    if (mine.length !== 2 || !mine.includes(myPlayer)) throw new ApiError('invalid_player');
    tc.double_player[halfKey(gw)] = { ...stamp, doubledPlayer: myPlayer, zeroedPlayer: mine.find((p) => p !== myPlayer) };
  } else {
    tc.bonus3 = { used: true, gw, won: null, status: 'used', activatedAt: stamp.activatedAt };
  }

  tc.activeThisGW = { key: chip, gw };
  next[team] = tc;
  return { chips: next, opponent, warning };
}
