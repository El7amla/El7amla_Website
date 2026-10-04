// Password check: HMAC-SHA256(key = salt, message = password).
// One native HMAC call per login keeps CPU far below the free-plan limit.
// Safe only because passwords are generated (high entropy) by scripts/gen-credentials.mjs.
const enc = new TextEncoder();

const toHex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
const fromHex = (hex) => Uint8Array.from(hex.match(/../g) || [], (h) => parseInt(h, 16));

export function randomSaltHex(bytes = 16) {
  return toHex(crypto.getRandomValues(new Uint8Array(bytes)));
}

export async function hashPassword(password, saltHex) {
  const k = await crypto.subtle.importKey('raw', fromHex(saltHex), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return toHex(await crypto.subtle.sign('HMAC', k, enc.encode(password)));
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

const DUMMY_SALT = '00'.repeat(16);

// credentialsJson: the TEAM_CREDENTIALS secret -> {"Team": {"salt": "...", "hash": "..."}, ...}
export async function verifyTeamPassword(credentialsJson, team, password) {
  let creds;
  try {
    creds = JSON.parse(credentialsJson);
  } catch {
    return false;
  }
  const entry = Object.prototype.hasOwnProperty.call(creds, team) ? creds[team] : null;
  const computed = await hashPassword(password, entry?.salt || DUMMY_SALT);
  return !!entry && safeEqual(computed, String(entry.hash || ''));
}
