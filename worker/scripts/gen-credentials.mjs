// Usage (from worker/):  node scripts/gen-credentials.mjs ../league.json
// WARNING: This OVERWRITES all team passwords. Do NOT run after teams
// have changed passwords from khawas.html.
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { createInterface } from 'node:readline/promises';
import { stdin as input, stdout as output } from 'node:process';
import { hashPassword, randomSaltHex } from '../src/auth.js';

const leaguePath = process.argv[2] || '../league.json';
const league = JSON.parse(readFileSync(leaguePath, 'utf8'));
const ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';

function randomPassword(len = 12) {
  let out = '';
  while (out.length < len) {
    for (const b of crypto.getRandomValues(new Uint8Array(32))) {
      if (b < 224 && out.length < len) out += ALPHABET[b % ALPHABET.length];
    }
  }
  return out;
}

const rl = createInterface({ input, output });
console.log('⚠️  WARNING: This will generate NEW random passwords for ALL teams.');
console.log('   Any password a team changed from the website will be ERASED.');
console.log('   Run this ONLY for first-time setup (or a full intentional reset).\n');

if (existsSync('.team-credentials.local.json')) {
  console.log('Found existing .team-credentials.local.json');
}

const answer = (await rl.question('Type YES (all caps) to continue: ')).trim();
rl.close();

if (answer !== 'YES') {
  console.log('Aborted. No files written.');
  process.exit(0);
}

const creds = {};
for (const team of Object.keys(league.teams)) {
  const password = randomPassword();
  const salt = randomSaltHex();
  creds[team] = { salt, hash: await hashPassword(password, salt) };
  console.log(`${team}\t${password}`);
}
writeFileSync('.team-credentials.local.json', JSON.stringify(creds));
console.log('\nWrote .team-credentials.local.json — save the table above, then:');
console.log('  npx wrangler secret put TEAM_CREDENTIALS < .team-credentials.local.json');
console.log('  rm .team-credentials.local.json');
console.log('\nDo NOT run this script again unless you intend a full password reset.');
