# El7amla Chips Worker

Makes Chip activation authoritative: `khawas.html` no longer decides
anything itself — it calls this Worker, which validates every rule
server-side against a real FPL deadline and writes the result straight
into `data/chips.json` in the repo (the same file `scripts/update_standings.py`
already reads).

## What this is NOT
- Not a database. No KV, no D1, no paid tier of anything.
- Not an AI service — every check here is plain deterministic code.
- Not a place secrets live in source. `wrangler.toml` only holds non-secret
  config (repo owner/name/branch).

## One-time setup

1. Install Wrangler if you don't have it: `npm install -g wrangler`
2. Edit `wrangler.toml`: set `GITHUB_REPO` to your actual repo name.
3. Create a **fine-grained GitHub PAT** scoped to *only this repository*,
   with **Contents: Read and write** permission and nothing else. Do not
   use a classic token with broad scope.
4. Set the three secrets (you'll be prompted for the value, it's never
   echoed or logged):
   ```bash
   cd worker
   wrangler secret put GITHUB_TOKEN
   wrangler secret put SESSION_SECRET      # any random string, e.g. `openssl rand -hex 32`
   wrangler secret put TEAM_CREDENTIALS    # see below for how to generate this
   ```

## Generating TEAM_CREDENTIALS

`TEAM_CREDENTIALS` is a single JSON blob (one Worker Secret, not one per
team) shaped like:

```json
{
  "Black Tech": { "salt": "…hex…", "hash": "…hex…", "iterations": 100000 },
  "The Pharaohs": { "salt": "…hex…", "hash": "…hex…", "iterations": 100000 }
}
```

Generate one team's `{salt, hash}` pair locally (Node, using the same
`hashPassword`/`randomSaltHex` functions the Worker itself uses — nothing
bespoke, no separate crypto implementation to keep in sync):

```js
// generate-credential.mjs — run with: node generate-credential.mjs "TeamName" "the-password"
import { hashPassword, randomSaltHex } from "./src/auth.js";
const [, , team, password] = process.argv;
const salt = randomSaltHex();
const hash = await hashPassword(password, salt);
console.log(JSON.stringify({ [team]: { salt, hash, iterations: 100000 } }, null, 2));
```

Run it once per team, merge the results into one JSON object, and that's
the value you paste into `wrangler secret put TEAM_CREDENTIALS`.

**Passwords are never stored in plaintext anywhere** — not in this repo,
not in the Worker, not in the frontend. Only the salted PBKDF2 hash lives
in the secret.

## Deploy

```bash
cd worker
wrangler deploy
```

This does **not** happen automatically as part of this change — you run
it yourself when ready, per the instruction not to auto-deploy anything.

After deploying, put the Worker's `*.workers.dev` URL into `khawas.html`'s
`WORKER_URL` constant (that constant is not sensitive — it's just an
endpoint address, same pattern `admin.html` already uses for its own
Worker URL).

## Running the tests (no deployment needed)

All tests run fully offline against fakes — no real GitHub, FPL, or
Cloudflare network calls:

```bash
cd worker
node --test test/chipRules.test.js test/session.test.js test/integration.test.js
```

## Routes

| Method | Path              | Auth              | Purpose |
|--------|-------------------|-------------------|---------|
| POST   | `/auth/login`     | none              | `{team, password}` → `{token, expiresInSeconds}` |
| POST   | `/chips/activate` | `Bearer <token>`  | `{chipKey, payload}` → validated, persisted activation |
| GET    | `/chips/state`    | none (public read)| `?team=X` → that team's persisted chip record |

`GW` and the deadline are **never** accepted from the client on
`/chips/activate` — the Worker always resolves them itself from FPL's
`bootstrap-static` endpoint against its own clock.
