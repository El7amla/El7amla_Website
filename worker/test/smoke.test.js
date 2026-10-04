import test from 'node:test';
import assert from 'node:assert/strict';
import { handle } from '../src/index.js';
import { hashPassword } from '../src/auth.js';
import { halfOfGW, applyActivation } from '../src/chipRules.js';
import { resetDeadlineCache } from '../src/fplDeadline.js';
import { b64ToUtf8, utf8ToB64 } from '../src/github.js';

const league = { teams: { A: { players: { a1: 1, a2: 2 } }, B: { players: { b1: 3, b2: 4 } } } };
const fixtures = { fixtures: { 6: [['A', 'B']], 7: [['A', 'BYE']] } };
const NOW = Date.parse('2026-10-04T10:00:00Z');

async function setup() {
  resetDeadlineCache();
  const salt = '11'.repeat(16);
  const store = { 'league.json': league, 'fixtures.json': fixtures, 'data/chips.json': { _comment: 'x' } };
  const calls = { dispatch: 0, puts: 0 };
  const fetchFn = async (url, init = {}) => {
    const u = String(url);
    if (u.includes('bootstrap-static')) return Response.json({ events: [{ id: 6, deadline_time: '2026-10-10T10:00:00Z' }] });
    if (u.endsWith('/dispatches')) { calls.dispatch++; return new Response(null, { status: 204 }); }
    const path = u.split('/contents/')[1]?.split('?')[0];
    if (init.method === 'PUT') {
      calls.puts++;
      store[path] = JSON.parse(b64ToUtf8(JSON.parse(init.body).content));
      return Response.json({}, { status: 200 });
    }
    return store[path] ? Response.json({ content: utf8ToB64(JSON.stringify(store[path])), sha: 's' }) : new Response('', { status: 404 });
  };
  const env = {
    GITHUB_TOKEN: 't', SESSION_SECRET: 's3cret', GITHUB_REPO: 'o/r', ALLOWED_ORIGIN: 'https://x.io',
    TEAM_CREDENTIALS: JSON.stringify({ A: { salt, hash: await hashPassword('pw', salt) } }),
  };
  const call = (method, path, body, token) =>
    handle(new Request(`https://w.dev${path}`, { method, body: body && JSON.stringify(body), headers: token ? { Authorization: `Bearer ${token}` } : {} }), env, { fetch: fetchFn, now: NOW });
  return { call, store, calls, env };
}

test('half split is 1-19 / 20-35', () => {
  assert.equal(halfOfGW(19), 1);
  assert.equal(halfOfGW(20), 2);
});

test('login, state, activate, reject reuse, dispatch', async () => {
  const { call, store, calls } = await setup();
  assert.equal((await call('POST', '/auth/login', { team: 'A', password: 'bad' })).status, 401);
  const { token } = await (await call('POST', '/auth/login', { team: 'A', password: 'pw' })).json();
  const st = await (await call('GET', '/chips/state', null, token)).json();
  assert.equal(st.gw, 6);
  assert.equal(st.opponent.team, 'B');
  assert.equal((await call('GET', '/chips/state')).status, 401);

  const ok = await call('POST', '/chips/activate', { chip: 'double_player', myPlayer: 'a1' }, token);
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).recompute, 'dispatched');
  assert.equal(store['data/chips.json'].A.double_player.h1.zeroedPlayer, 'a2');
  assert.equal(store['data/chips.json'].A.double_player.h1.status, 'used');
  assert.equal(calls.dispatch, 1);

  assert.equal((await (await call('POST', '/chips/activate', { chip: 'bonus3' }, token)).json()).error, 'chip_this_gw');
});

test('rules: invalid player, bye, already used', () => {
  const base = { team: 'A', gw: 6, league, fixtures: fixtures.fixtures };
  assert.throws(() => applyActivation({ ...base, chips: {}, chip: 'one_v_one', params: { myPlayer: 'a1', oppPlayer: 'zzz' } }), { code: 'invalid_player' });
  assert.throws(() => applyActivation({ ...base, gw: 7, chips: {}, chip: 'bonus3' }), { code: 'no_match' });
  const r = applyActivation({ ...base, chips: {}, chip: 'bonus3' });
  assert.throws(() => applyActivation({ ...base, gw: 6, chips: { A: { ...r.chips.A, activeThisGW: null } }, chip: 'bonus3' }), { code: 'already_used' });
});

test('config guard rejects placeholder repo', async () => {
  const { call, env } = await setup();
  env.GITHUB_REPO = 'El7amla/REPLACE_WITH_REPO_NAME';
  assert.equal((await call('POST', '/auth/login', { team: 'A', password: 'pw' })).status, 500);
});
