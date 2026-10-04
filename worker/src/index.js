// index.js
// ─────────────────────────────────────────────
// El7amla Chips Worker.
//
// Routes:
//   POST /auth/login        { team, password } -> { token, expiresInSeconds }
//   POST /chips/activate    Bearer token + { chipKey, payload } -> { activatedSlot, gw, deadline }
//   GET  /chips/state?team= -> that team's persisted chip record (public read)
//
// Required Worker Secrets (set via `wrangler secret put`, never in code):
//   GITHUB_TOKEN        - fine-grained PAT with contents:write on this repo only
//   SESSION_SECRET       - random string, signs session tokens
//   TEAM_CREDENTIALS    - JSON blob of { team: { salt, hash, iterations } }
//
// Required plain vars (wrangler.toml [vars], not secret):
//   GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH
//
// No AI/LLM calls anywhere in this file. No paid services. No KV required.
// ─────────────────────────────────────────────

import { signSession, verifySession } from "./session.js";
import { verifyTeamPassword } from "./auth.js";
import { resolveCurrentGWAndDeadline } from "./fplDeadline.js";
import { getJsonFile, updateJsonFileWithRetry, GitHubConflictError } from "./github.js";
import { validateAndBuildActivation, ChipValidationError, blankTeamChips } from "./chipRules.js";

const CHIPS_PATH = "data/chips.json";
const FIXTURES_PATH = "fixtures.json";
const LEAGUE_PATH = "league.json";
const FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/";

// Short server-side cache for the FPL bootstrap payload so a burst of
// activation requests doesn't hammer FPL's API. A plain in-memory Map is
// enough here (traffic is tiny — a handful of teams, occasional activations)
// and it naturally resets on each new Worker isolate, so no KV is needed.
//
// This is a module-level singleton for the real deployed Worker (isolates
// are reused across requests, so the cache is useful there), but tests
// inject their own fresh Map via `deps.fplCache` so one test's cached FPL
// response can never leak into another test.
const productionFplCache = new Map();

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
  };
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}

async function fetchFplBootstrap(fetchImpl, cache, cacheTtlSeconds = 60) {
  const cacheKey = "fpl-bootstrap";
  const cached = cache.get(cacheKey);
  if (cached && cached.expiresAt > Date.now()) return cached.data;

  const res = await fetchImpl(FPL_BOOTSTRAP_URL, {
    headers: { "User-Agent": "el7amla-chips-worker" },
  });
  if (!res.ok) throw new Error(`FPL bootstrap fetch failed: HTTP ${res.status}`);
  const data = await res.json();
  cache.set(cacheKey, { data, expiresAt: Date.now() + cacheTtlSeconds * 1000 });
  return data;
}

async function handleLogin(request, env) {
  const { team, password } = await request.json().catch(() => ({}));
  if (!team || !password) return json({ error: "team and password required" }, 400);

  const credentials = JSON.parse(env.TEAM_CREDENTIALS || "{}");
  const ok = await verifyTeamPassword(credentials, team, password);
  if (!ok) return json({ error: "invalid team or password" }, 401);

  const ttlSeconds = 1800;
  const token = await signSession({ team }, env.SESSION_SECRET, ttlSeconds);
  return json({ token, expiresInSeconds: ttlSeconds });
}

async function requireSession(request, env) {
  const authHeader = request.headers.get("Authorization") || "";
  const token = authHeader.startsWith("Bearer ") ? authHeader.slice(7) : null;
  const payload = await verifySession(token, env.SESSION_SECRET);
  return payload; // null if invalid/expired
}

async function handleActivate(request, env, deps) {
  const session = await requireSession(request, env);
  if (!session) return json({ error: "unauthorized" }, 401);

  const { chipKey, payload } = await request.json().catch(() => ({}));
  if (!chipKey) return json({ error: "chipKey required" }, 400);

  const fetchImpl = deps.fetchImpl;

  let gw, deadlineIso;
  try {
    const bootstrap = await fetchFplBootstrap(fetchImpl, deps.fplCache || productionFplCache);
    ({ gw, deadlineIso } = resolveCurrentGWAndDeadline(bootstrap, Date.now()));
  } catch (err) {
    return json({ error: "could not resolve current GW/deadline", detail: err.message }, 502);
  }

  const [fixturesFile, leagueFile] = await Promise.all([
    getJsonFile({
      owner: env.GITHUB_OWNER,
      repo: env.GITHUB_REPO,
      branch: env.GITHUB_BRANCH,
      token: env.GITHUB_TOKEN,
      path: FIXTURES_PATH,
      fetchImpl,
    }),
    getJsonFile({
      owner: env.GITHUB_OWNER,
      repo: env.GITHUB_REPO,
      branch: env.GITHUB_BRANCH,
      token: env.GITHUB_TOKEN,
      path: LEAGUE_PATH,
      fetchImpl,
    }),
  ]);
  const fixtures = fixturesFile.json.fixtures || fixturesFile.json;
  const league = leagueFile.json.teams ? leagueFile.json : { teams: leagueFile.json };

  try {
    const mutation = await updateJsonFileWithRetry({
      owner: env.GITHUB_OWNER,
      repo: env.GITHUB_REPO,
      branch: env.GITHUB_BRANCH,
      token: env.GITHUB_TOKEN,
      path: CHIPS_PATH,
      fetchImpl,
      message: `chips: ${session.team} activates ${chipKey} for GW${gw}`,
      mutator: (currentChipsJson) => {
        const chipsState = { ...currentChipsJson };
        delete chipsState._comment;
        if (!chipsState[session.team]) chipsState[session.team] = blankTeamChips();

        const { nextChipsState, activatedSlot } = validateAndBuildActivation({
          chipsState,
          fixtures,
          league,
          team: session.team,
          chipKey,
          gw,
          payload,
          nowIso: new Date().toISOString(),
          deadlineIso,
        });

        const nextJson = { _comment: currentChipsJson._comment, ...nextChipsState };
        return { nextJson, activatedSlot, gw, deadlineIso };
      },
    });

    return json({
      ok: true,
      gw: mutation.gw,
      deadline: mutation.deadlineIso,
      activatedSlot: mutation.activatedSlot,
    });
  } catch (err) {
    if (err instanceof ChipValidationError) {
      return json({ error: err.code, message: err.message }, 409);
    }
    if (err instanceof GitHubConflictError) {
      return json({ error: "concurrent_write", message: "Too much contention, try again" }, 503);
    }
    return json({ error: "internal_error", message: err.message }, 500);
  }
}

async function handleState(request, env, deps) {
  const url = new URL(request.url);
  const team = url.searchParams.get("team");
  if (!team) return json({ error: "team query param required" }, 400);

  const { json: chipsJson } = await getJsonFile({
    owner: env.GITHUB_OWNER,
    repo: env.GITHUB_REPO,
    branch: env.GITHUB_BRANCH,
    token: env.GITHUB_TOKEN,
    path: CHIPS_PATH,
    fetchImpl: deps.fetchImpl,
  });

  return json({ team, chips: chipsJson[team] || blankTeamChips() });
}

export { handleLogin, handleActivate, handleState };

export default {
  async fetch(request, env, ctx) {
    const deps = { fetchImpl: fetch, fplCache: productionFplCache };
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }

    try {
      if (request.method === "POST" && url.pathname === "/auth/login") {
        return await handleLogin(request, env);
      }
      if (request.method === "POST" && url.pathname === "/chips/activate") {
        return await handleActivate(request, env, deps);
      }
      if (request.method === "GET" && url.pathname === "/chips/state") {
        return await handleState(request, env, deps);
      }
      return json({ error: "not_found" }, 404);
    } catch (err) {
      return json({ error: "internal_error", message: err.message }, 500);
    }
  },
};
