import { ApiError } from './errors.js';

// Updates this Worker's own TEAM_CREDENTIALS secret via the Cloudflare API, so a team can
// change its password without anyone touching `wrangler secret put` by hand. Needs two extra
// secrets: CF_API_TOKEN (scoped to "Account > Workers Scripts > Edit" on this account only)
// and CF_ACCOUNT_ID. Script name is read from env.CF_WORKER_NAME (defaults to wrangler.toml's
// `name`). See worker/README.md "Changing passwords" for how to create the token.
export async function putWorkerSecret(env, secretName, secretValue, fetchFn = fetch) {
  if (!env.CF_API_TOKEN || !env.CF_ACCOUNT_ID) throw new ApiError('password_change_unavailable', 501);
  const scriptName = env.CF_WORKER_NAME || 'el7amla-chips';
  const res = await fetchFn(
    `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/workers/scripts/${scriptName}/secrets`,
    {
      method: 'PUT',
      headers: { Authorization: `Bearer ${env.CF_API_TOKEN}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: secretName, text: secretValue, type: 'secret_text' }),
    },
  );
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.success === false) throw new ApiError('password_change_failed', 502);
}
