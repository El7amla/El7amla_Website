// Usage (from worker/):  node scripts/gen-credentials.mjs ../league.json
// Prints one generated password per team (shown ONCE, hand them out privately) and writes the
// salted hashes to .team-credentials.local.json (gitignored) for:
//   npx wrangler secret put TEAM_CREDENTIALS < .team-credentials.local.json
import { readFileSync, writeFileSync } from 'node:fs';
import { hashPassword, randomSaltHex } from '../src/auth.js';

const league = JSON.parse(readFileSync(process.argv[2] || '../league.json', 'utf8'));
const ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'; // 56 chars, no look-alikes

function randomPassword(len = 12) {
  let out = '';
  while (out.length < len) {
    for (const b of crypto.getRandomValues(new Uint8Array(32))) {
      if (b < 224 && out.length < len) out += ALPHABET[b % ALPHABET.length]; // rejection sampling, no bias
    }
  }
  return out;
}

const creds = {};
for (const team of Object.keys(league.teams)) {
  const password = randomPassword();
  const salt = randomSaltHex();
  creds[team] = { salt, hash: await hashPassword(password, salt) };
  console.log(`${team}\t${password}`);
}
writeFileSync('.team-credentials.local.json', JSON.stringify(creds));
console.log('\nWrote .team-credentials.local.json - delete it after `wrangler secret put`.');
