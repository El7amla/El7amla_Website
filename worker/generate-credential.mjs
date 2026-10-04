// Usage: node generate-credential.mjs "Team Name" "the-password"
// Prints one team's {salt, hash} entry. Merge all teams into ONE JSON object,
// then paste it when running: wrangler secret put TEAM_CREDENTIALS
import { hashPassword, randomSaltHex } from "./src/auth.js";
const [, , team, password] = process.argv;
if (!team || !password) {
  console.error('Usage: node generate-credential.mjs "Team Name" "password"');
  process.exit(1);
}
const salt = randomSaltHex();
const hash = await hashPassword(password, salt);
console.log(JSON.stringify({ [team]: { salt, hash, iterations: 100000 } }, null, 2));
