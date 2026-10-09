import { ApiError } from './errors.js';
import { verifyTeamPassword, buildCredentialsAfterPasswordChange, MIN_PASSWORD_LENGTH } from './auth.js';
import { createSession, verifySession, SESSION_TTL_SECONDS } from './session.js';
import { getUpcomingGW } from './fplDeadline.js';
import { getJsonFile, putJsonFile, dispatchWorkflow } from './github.js';
import { applyActivation, chipStatuses, findOpponent, halfOfGW, listActivations, SEASON_GWS } from './chipRules.js';
import { putWorkerSecret } from './cfSecrets.js';

const json = (body, status, cors) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...cors } });

function corsHeaders(request, env) {
  const origin = request.headers.get('Origin');
  const allowed = String(env.ALLOWED_ORIGIN || '').split(',').map((s) => s.trim()).filter(Boolean);
  const h = { Vary: 'Origin' };
  if (origin && allowed.includes(origin)) {
    h['Access-Control-Allow-Origin'] = origin;
    h['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS';
    h['Access-Control-Allow-Headers'] = 'Content-Type, Authorization';
    h['Access-Control-Max-Age'] = '86400';
  }
  return h;
}

function checkConfig(env) {
  for (const k of ['GITHUB_TOKEN', 'SESSION_SECRET', 'TEAM_CREDENTIALS', 'GITHUB_REPO']) {
    if (!env[k]) throw new ApiError('config_error', 500);
  }
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(env.GITHUB_REPO) || /REPLACE|CONFIRM/i.test(env.GITHUB_REPO)) {
    throw new ApiError('config_error', 500);
  }
}

async function readJson(request) {
  const text = await request.text();
  if (text.length > 2000) throw new ApiError('too_large', 413);
  try {
    return JSON.parse(text || '{}');
  } catch {
    throw new ApiError('bad_json', 400);
  }
}

async function requireSession(request, env, now) {
  const m = /^Bearer (.+)$/.exec(request.headers.get('Authorization') || '');
  const s = m ? await verifySession(env.SESSION_SECRET, m[1], now) : null;
  if (!s) throw new ApiError('invalid_session', 401);
  return s;
}

const paths = (env) => ({
  chips: env.CHIPS_PATH || 'data/chips.json',
  league: env.LEAGUE_PATH || 'league.json',
  fixtures: env.FIXTURES_PATH || 'fixtures.json',
});

async function loadRepoData(env, fetchFn) {
  const p = paths(env);
  const [league, fixtures, chips] = await Promise.all([
    getJsonFile(env, p.league, fetchFn),
    getJsonFile(env, p.fixtures, fetchFn),
    getJsonFile(env, p.chips, fetchFn),
  ]);
  if (!league || !fixtures) throw new ApiError('repo_data_missing', 502);
  return { league: league.data, fixtures: fixtures.data.fixtures ?? fixtures.data, chips: chips?.data ?? {} };
}

async function login(request, env, now) {
  const { team, password } = await readJson(request);
  if (typeof team !== 'string' || typeof password !== 'string' || !team || team.length > 60 || password.length > 100) {
    throw new ApiError('bad_request', 400);
  }
  if (!(await verifyTeamPassword(env.TEAM_CREDENTIALS, team, password))) throw new ApiError('bad_credentials', 401);
  return { token: await createSession(env.SESSION_SECRET, team, SESSION_TTL_SECONDS, now), team, expires_in: SESSION_TTL_SECONDS };
}

async function changePassword(request, team, env, fetchFn) {
  const { oldPassword, newPassword } = await readJson(request);
  if (typeof oldPassword !== 'string' || typeof newPassword !== 'string' || newPassword.length < MIN_PASSWORD_LENGTH || newPassword.length > 100) {
    throw new ApiError('bad_password', 400);
  }
  let nextCreds;
  try {
    nextCreds = await buildCredentialsAfterPasswordChange(env.TEAM_CREDENTIALS, team, oldPassword, newPassword);
  } catch (e) {
    if (e.code === 'wrong_old_password') throw new ApiError('wrong_old_password', 401);
    throw e;
  }
  await putWorkerSecret(env, 'TEAM_CREDENTIALS', JSON.stringify(nextCreds), fetchFn);
  return { ok: true };
}

async function state(team, env, fetchFn, now) {
  const [dl, data] = await Promise.all([getUpcomingGW(fetchFn, now), loadRepoData(env, fetchFn)]);
  if (!data.league.teams?.[team]) throw new ApiError('unknown_team', 404);
  const seasonOver = dl.gw === null;
  const gw = seasonOver ? SEASON_GWS : dl.gw;
  const locked = seasonOver || now >= Date.parse(dl.deadline);
  const opp = seasonOver ? null : findOpponent(data.fixtures, team, gw);
  return {
    team,
    gw,
    deadline: dl.deadline,
    half: halfOfGW(gw),
    locked,
    season_over: seasonOver,
    bye: !seasonOver && !opp,
    players: Object.keys(data.league.teams[team].players || {}),
    opponent: opp ? { team: opp, players: Object.keys(data.league.teams[opp]?.players || {}) } : null,
    chips: chipStatuses(data.chips[team], gw, locked),
    history: listActivations(data.chips[team]),
  };
}

async function activate(request, team, env, fetchFn, now) {
  const body = await readJson(request);
  const dl = await getUpcomingGW(fetchFn, now);
  if (dl.gw === null) throw new ApiError('season_over', 409);
  if (now >= Date.parse(dl.deadline)) throw new ApiError('deadline_passed', 409);
  const { league, fixtures } = await loadRepoData(env, fetchFn);
  const chipsPath = paths(env).chips;

  let done = null;
  for (let attempt = 0; attempt < 3 && !done; attempt++) {
    const file = await getJsonFile(env, chipsPath, fetchFn); // re-read each attempt, re-validate on fresh data
    const result = applyActivation({ chips: file?.data ?? {}, team, gw: dl.gw, chip: body.chip, params: body, league, fixtures, now: new Date(now) });
    try {
      await putJsonFile(env, chipsPath, result.chips, file?.sha, `Chip: ${team} activated ${body.chip} (GW${dl.gw})`, fetchFn);
      done = result;
    } catch (e) {
      if (!(e instanceof ApiError && e.code === 'github_conflict')) throw e;
    }
  }
  if (!done) throw new ApiError('github_conflict', 409);

  let recompute = 'failed';
  try {
    recompute = (await dispatchWorkflow(env, fetchFn)) ? 'dispatched' : 'failed';
  } catch {
    /* activation is already saved; the 2-hour cron will pick it up */
  }
  return { ok: true, chip: body.chip, gw: dl.gw, opponent: done.opponent, warning: done.warning, recompute };
}

export async function handle(request, env, deps = {}) {
  const fetchFn = deps.fetch || fetch;
  const now = deps.now ?? Date.now();
  const cors = corsHeaders(request, env);
  if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
  try {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    if (route === 'GET /health') return json({ ok: true }, 200, cors);
    checkConfig(env);
    if (route === 'POST /auth/login') return json(await login(request, env, now), 200, cors);
    if (route === 'GET /chips/state') {
      const s = await requireSession(request, env, now);
      return json({ ...(await state(s.team, env, fetchFn, now)), session_expires_at: s.exp * 1000 }, 200, cors);
    }
    if (route === 'POST /chips/activate') {
      const s = await requireSession(request, env, now);
      return json(await activate(request, s.team, env, fetchFn, now), 200, cors);
    }
    if (route === 'POST /auth/change-password') {
      const s = await requireSession(request, env, now);
      return json(await changePassword(request, s.team, env, fetchFn), 200, cors);
    }
    throw new ApiError('not_found', 404);
  } catch (e) {
    if (e instanceof ApiError) return json({ error: e.code }, e.status, cors);
    console.error('unhandled', e && e.message);
    return json({ error: 'server_error' }, 500, cors);
  }
}

export default { fetch: (request, env) => handle(request, env) };
