// session.js
// ─────────────────────────────────────────────
// Stateless, signed, short-lived session tokens.
//
// Token shape: base64url(json payload) + "." + base64url(HMAC-SHA256 signature)
// Payload: { team, iat, exp }
//
// No session store (no KV, no DB) is needed: the Worker can verify a token
// on any request using only the shared secret (a Worker Secret, never
// shipped to the client). This keeps the whole auth layer free and
// stateless, and a token cannot be forged or extended without the secret.
// ─────────────────────────────────────────────

const encoder = new TextEncoder();

function base64url(bytes) {
  let str;
  if (typeof bytes === "string") {
    str = btoa(unescape(encodeURIComponent(bytes)));
  } else {
    str = btoa(String.fromCharCode(...new Uint8Array(bytes)));
  }
  return str.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64urlDecodeToString(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((b64url.length + 3) % 4);
  return decodeURIComponent(escape(atob(b64)));
}

async function hmacKey(secret) {
  return crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"]
  );
}

/**
 * @param {{team: string}} claims
 * @param {string} secret
 * @param {number} ttlSeconds default 30 minutes, matching admin.html's existing session UX
 * @param {number} nowMs override for testing
 */
export async function signSession(claims, secret, ttlSeconds = 1800, nowMs = Date.now()) {
  const payload = {
    team: claims.team,
    iat: Math.floor(nowMs / 1000),
    exp: Math.floor(nowMs / 1000) + ttlSeconds,
  };
  const payloadStr = JSON.stringify(payload);
  const key = await hmacKey(secret);
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(payloadStr));
  return `${base64url(payloadStr)}.${base64url(sig)}`;
}

/**
 * Returns the verified payload, or null if the token is malformed, has a
 * bad signature, or is expired. Never throws.
 * @param {number} nowMs override for testing
 */
export async function verifySession(token, secret, nowMs = Date.now()) {
  if (!token || typeof token !== "string" || !token.includes(".")) return null;
  const [payloadPart, sigPart] = token.split(".");
  try {
    const payloadStr = base64urlDecodeToString(payloadPart);
    const key = await hmacKey(secret);
    const expectedSig = await crypto.subtle.sign("HMAC", key, encoder.encode(payloadStr));
    const expectedSigB64 = base64url(expectedSig);
    if (!timingSafeEqual(expectedSigB64, sigPart)) return null;

    const payload = JSON.parse(payloadStr);
    if (typeof payload.exp !== "number" || Math.floor(nowMs / 1000) >= payload.exp) return null;
    return payload;
  } catch {
    return null;
  }
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
