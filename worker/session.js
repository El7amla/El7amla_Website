// Stateless signed session tokens (HMAC-SHA256). Token lives only in the browser's memory.
export const SESSION_TTL_SECONDS = 30 * 60;

const enc = new TextEncoder();

function toB64u(bytes) {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function fromB64u(str) {
  let s = str.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}

const key = (secret) =>
  crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);

export async function createSession(secret, team, ttl = SESSION_TTL_SECONDS, now = Date.now()) {
  const payload = toB64u(enc.encode(JSON.stringify({ t: team, exp: Math.floor(now / 1000) + ttl })));
  const sig = await crypto.subtle.sign('HMAC', await key(secret), enc.encode(payload));
  return `${payload}.${toB64u(new Uint8Array(sig))}`;
}

export async function verifySession(secret, token, now = Date.now()) {
  if (typeof token !== 'string') return null;
  const [payload, sig] = token.split('.');
  if (!payload || !sig) return null;
  try {
    const ok = await crypto.subtle.verify('HMAC', await key(secret), fromB64u(sig), enc.encode(payload));
    if (!ok) return null;
    const data = JSON.parse(new TextDecoder().decode(fromB64u(payload)));
    if (!data.t || !(data.exp > now / 1000)) return null;
    return { team: data.t, exp: data.exp };
  } catch {
    return null;
  }
}
