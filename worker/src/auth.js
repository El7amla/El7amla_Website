// auth.js
// ─────────────────────────────────────────────
// Team password verification via PBKDF2-SHA256 (Web Crypto, zero deps —
// works identically in Cloudflare Workers and Node for testing).
//
// Credentials are never stored in the repo or shipped to the frontend.
// They live as a single JSON blob in the `TEAM_CREDENTIALS` Worker Secret:
//   { "Black Tech": { "salt": "<hex>", "hash": "<hex>", "iterations": 100000 }, ... }
// See worker/README.md for the offline command that generates this blob.
// ─────────────────────────────────────────────

const encoder = new TextEncoder();

export async function hashPassword(password, saltHex, iterations = 100000) {
  const salt = hexToBytes(saltHex);
  const keyMaterial = await crypto.subtle.importKey(
    "raw",
    encoder.encode(password),
    "PBKDF2",
    false,
    ["deriveBits"]
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations, hash: "SHA-256" },
    keyMaterial,
    256
  );
  return bytesToHex(new Uint8Array(bits));
}

/**
 * @param {object} credentialsBlob  parsed TEAM_CREDENTIALS secret
 */
export async function verifyTeamPassword(credentialsBlob, team, password) {
  const record = credentialsBlob?.[team];
  if (!record || !record.salt || !record.hash) return false;
  const computed = await hashPassword(password, record.salt, record.iterations || 100000);
  return timingSafeEqualHex(computed, record.hash);
}

export function randomSaltHex(byteLength = 16) {
  const bytes = crypto.getRandomValues(new Uint8Array(byteLength));
  return bytesToHex(bytes);
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  return bytes;
}

function bytesToHex(bytes) {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function timingSafeEqualHex(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
