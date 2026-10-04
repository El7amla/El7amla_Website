// github.js
// ─────────────────────────────────────────────
// Minimal GitHub Contents API client for reading/writing a single JSON
// file with optimistic concurrency. The GitHub token is read from env
// (a Cloudflare Worker Secret) and never touches the response body or logs.
//
// `fetchImpl` is injectable so tests can run against a fake in-memory
// GitHub without any network access.
// ─────────────────────────────────────────────

export class GitHubConflictError extends Error {
  constructor(message) {
    super(message);
    this.code = "github_conflict";
  }
}

function apiBase(owner, repo) {
  return `https://api.github.com/repos/${owner}/${repo}/contents`;
}

/**
 * @returns {{ json: object, sha: string }}
 */
export async function getJsonFile({ owner, repo, path, branch, token, fetchImpl = fetch }) {
  const url = `${apiBase(owner, repo)}/${path}?ref=${encodeURIComponent(branch)}`;
  const res = await fetchImpl(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      "User-Agent": "el7amla-chips-worker",
      Accept: "application/vnd.github+json",
    },
  });
  if (!res.ok) {
    throw new Error(`GitHub GET ${path} failed: HTTP ${res.status}`);
  }
  const body = await res.json();
  const content = decodeBase64Utf8(body.content.replace(/\n/g, ""));
  return { json: JSON.parse(content), sha: body.sha };
}

/**
 * @param {string} sha  the sha the caller last read; GitHub rejects the
 *                       write with 409/422 if the file has changed since.
 */
export async function putJsonFile({
  owner,
  repo,
  path,
  branch,
  token,
  json,
  sha,
  message,
  fetchImpl = fetch,
}) {
  const url = `${apiBase(owner, repo)}/${path}`;
  const body = {
    message,
    content: encodeBase64Utf8(JSON.stringify(json, null, 2) + "\n"),
    sha,
    branch,
  };
  const res = await fetchImpl(url, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${token}`,
      "User-Agent": "el7amla-chips-worker",
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (res.status === 409 || res.status === 422) {
    throw new GitHubConflictError(`GitHub PUT ${path} conflict: HTTP ${res.status}`);
  }
  if (!res.ok) {
    throw new Error(`GitHub PUT ${path} failed: HTTP ${res.status}`);
  }
  return res.json();
}

/**
 * Read-validate-mutate-write with bounded retry on concurrent-write conflicts.
 *
 * `mutator(currentJson) => nextJson` must be a pure function that performs
 * all rule validation itself and throws (not returns) on invalid input —
 * that exception aborts the whole operation without retrying, since a
 * business-rule rejection is not a concurrency problem and re-reading
 * won't fix it.
 *
 * @returns {{ result: object, activatedSlot: any }} whatever the mutator returns
 */
export async function updateJsonFileWithRetry({
  owner,
  repo,
  path,
  branch,
  token,
  mutator,
  message,
  fetchImpl = fetch,
  maxRetries = 5,
}) {
  let lastError;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const { json: current, sha } = await getJsonFile({ owner, repo, path, branch, token, fetchImpl });
    const mutation = await mutator(current); // may throw ChipValidationError — not retried
    try {
      await putJsonFile({
        owner,
        repo,
        path,
        branch,
        token,
        json: mutation.nextJson,
        sha,
        message,
        fetchImpl,
      });
      return mutation;
    } catch (err) {
      if (err instanceof GitHubConflictError) {
        lastError = err;
        continue; // another write landed first — re-read and retry
      }
      throw err;
    }
  }
  throw lastError || new Error("updateJsonFileWithRetry: exhausted retries");
}

function decodeBase64Utf8(b64) {
  const binary = atob(b64);
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  return new TextDecoder("utf-8").decode(bytes);
}

function encodeBase64Utf8(str) {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary);
}
