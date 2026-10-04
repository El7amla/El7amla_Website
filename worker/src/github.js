import { ApiError } from './errors.js';

const API = 'https://api.github.com';

const headers = (env) => ({
  Authorization: `Bearer ${env.GITHUB_TOKEN}`,
  Accept: 'application/vnd.github+json',
  'X-GitHub-Api-Version': '2022-11-28',
  'User-Agent': 'el7amla-chips-worker',
});
const branch = (env) => env.GITHUB_BRANCH || 'main';

export function utf8ToB64(str) {
  let bin = '';
  for (const b of new TextEncoder().encode(str)) bin += String.fromCharCode(b);
  return btoa(bin);
}
export function b64ToUtf8(b64) {
  return new TextDecoder().decode(Uint8Array.from(atob(b64.replace(/\s/g, '')), (c) => c.charCodeAt(0)));
}

export async function getJsonFile(env, path, fetchFn = fetch) {
  const res = await fetchFn(`${API}/repos/${env.GITHUB_REPO}/contents/${path}?ref=${encodeURIComponent(branch(env))}`, {
    headers: headers(env),
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError('github_error', 502);
  const j = await res.json();
  try {
    return { data: JSON.parse(b64ToUtf8(j.content)), sha: j.sha };
  } catch {
    throw new ApiError('github_error', 502);
  }
}

export async function putJsonFile(env, path, data, sha, message, fetchFn = fetch) {
  const body = { message, content: utf8ToB64(JSON.stringify(data, null, 2) + '\n'), branch: branch(env) };
  if (sha) body.sha = sha;
  const res = await fetchFn(`${API}/repos/${env.GITHUB_REPO}/contents/${path}`, {
    method: 'PUT',
    headers: { ...headers(env), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (res.status === 409 || res.status === 422) throw new ApiError('github_conflict', 409);
  if (!res.ok) throw new ApiError('github_error', 502);
}

// Needs "Actions: Read and write" on the fine-grained token. Returns true on 204.
export async function dispatchWorkflow(env, fetchFn = fetch) {
  const res = await fetchFn(
    `${API}/repos/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE || 'update-standings.yml'}/dispatches`,
    {
      method: 'POST',
      headers: { ...headers(env), 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: branch(env) }),
    },
  );
  return res.status === 204;
}
