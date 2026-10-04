# El7amla chips Worker

يحفظ تفعيل الخواص في `data/chips.json` على GitHub، ويشغّل `update-standings.yml` بعد كل تفعيل.

## Endpoints
| Method | Path | الوظيفة |
|---|---|---|
| POST | `/auth/login` | `{team, password}` ← `{token, expires_in}` (30 دقيقة) |
| GET | `/chips/state` | حالة الفريق: الجولة، الديدلاين، الخواص، الخصم، السجل |
| POST | `/chips/activate` | `{chip, myPlayer?, oppPlayer?}` |
| GET | `/health` | فحص سريع |

أكواد الأخطاء: `bad_credentials`, `invalid_session`, `deadline_passed`, `already_used`, `chip_this_gw`, `invalid_player`, `no_match`, `season_over`, `github_conflict`, `github_error`, `fpl_unavailable`, `config_error`.

## القواعد (من `src/chipRules.js`)
- الدور الأول GW1–19، الثاني GW20–35 (نفس `half_key` في `update_standings.py`).
- وان تو وان ودبل: مرة لكل دور. بونص X3: مرة في الموسم. خاصية واحدة فقط لكل جولة.
- الخاصية بتتطبق على أول جولة ديدلاينها لسه ماجاش (من FPL). بدون ماتش (BYE) = مرفوض.
- الـ Worker هو اللي بيحسب `oppTeam` و`zeroedPlayer` ويكتب `status: "used"`.
- لو الفريقين فعّلوا 1v1 في نفس الجولة، `update_standings.py` بيلغي الاتنين؛ الـ Worker بيرجّع `warning: opponent_1v1_active` للتاني.

## النشر
```bash
cd worker
npm test                                   # اختياري: 4 اختبارات سريعة
# 1) عدّل GITHUB_REPO في wrangler.toml (owner/repo الحقيقي) وتأكد من ALLOWED_ORIGIN
# 2) ولّد باسوردات الفرق (بتظهر مرة واحدة في التيرمنال، وزّعها بشكل خاص)
node scripts/gen-credentials.mjs ../league.json
# 3) Secrets
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put SESSION_SECRET            # نص عشوائي طويل (32+ حرف)
npx wrangler secret put TEAM_CREDENTIALS < .team-credentials.local.json
rm .team-credentials.local.json
# 4) نشر
npx wrangler deploy
```

## GitHub token
Fine-grained PAT على الريبو ده فقط: **Contents: Read and write** و**Actions: Read and write**.
`Actions` لازم عشان `workflow_dispatch`؛ من غيره التفعيل بيتحفظ لكن الترتيب بيستنى الـ cron (حتى ساعتين).

## تحديث الترتيب بعد التفعيل
الـ Worker بعد ما يعمل commit لـ `data/chips.json` بينده `POST .../actions/workflows/update-standings.yml/dispatches`.
مفيش loop: الـ workflow ملوش trigger على `push`، والـ commit بتاعه `[skip ci]`.
يفضّل تضيف في `.github/workflows/update-standings.yml` (بعد `on:`) عشان التشغيلات المتداخلة تستنى بعض:
```yaml
concurrency:
  group: update-standings
  cancel-in-progress: false
```

## ملاحظات الخطة المجانية
- Workers Free: تحقق الباسورد بـ HMAC-SHA256 واحدة (مش PBKDF2) عشان CPU. آمنة لأن الباسوردات مولّدة عشوائيًا (12 حرف) من `gen-credentials.mjs`؛ ماتستخدمش باسوردات يدوية.
- مفيش rate-limit على الدخول داخل الـ Worker؛ الاعتماد على قوة الباسورد. لو عايز حماية زيادة استخدم قاعدة Rate Limiting من Cloudflare.
- GitHub Actions مجانية بلا حد على ريبو public. على private: 2000 دقيقة/شهر، والتشغيل كل ساعتين بيكبر مع كل جولة.
