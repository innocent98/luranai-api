# Pre-Deploy Guide

## TL;DR — run this sequence locally before pushing

After any change that touches encryption, DB schema, or seed scripts, run the full staging-equivalent flow locally first. If it passes, push with confidence. If it fails, fix locally — don't discover it on staging.

```bash
# 1. Rebuild the image with latest code
docker compose build api

# 2. Restart api so it uses the new image
docker compose up -d api

# 3. Run the full CD migration+backfill sequence
docker compose exec api alembic upgrade 145bebe88ecb
docker compose exec api python /app/scripts/backfill_encrypt_secrets.py
docker compose exec api python /app/scripts/backfill_encrypt_payments.py
docker compose exec api python /app/scripts/backfill_encrypt_messages.py
docker compose exec api python /app/scripts/backfill_encrypt_pii.py
docker compose exec api alembic upgrade 3d8e2fa91c07
docker compose exec api python /app/scripts/backfill_encrypt_user_admin.py
docker compose exec api alembic upgrade head
docker compose exec api python /app/scripts/seed_data.py
docker compose exec api python /app/scripts/seed_staging_e2e.py

# 4. Run it ALL AGAIN (idempotency check — CD will re-run on every deploy)
docker compose exec api python /app/scripts/backfill_encrypt_user_admin.py
docker compose exec api python /app/scripts/seed_staging_e2e.py

# 5. Smoke test login with a seeded user
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  --data-raw '{"email":"staging-e2e-pro@luranai.com","password":"StagingE2E123$"}' \
  | head -c 100
# Should start with {"access_token":"..."
```

If all steps succeed, push. If anything fails, fix locally first.

---

## Why three `alembic upgrade` calls instead of one?

Because **NOT NULL constraint migrations depend on data being backfilled first**.

The migration chain has this structure:

```
  b3f1a2c4d5e6          ← last migration before encryption
       ↓
  145bebe88ecb          ← adds encryption columns + nullable email_hash columns
       ↓
  2c7f9a1b4d8e          ← tightens customer_users.email_hash to NOT NULL
       ↓
  3d8e2fa91c07          ← adds encryption for users + platform_admins + nullable hashes
       ↓
  4f2a9b6c5d13 (head)   ← tightens users/platform_admins.email_hash to NOT NULL
```

If we ran `alembic upgrade head` straight through, we'd hit `2c7f9a1b4d8e` (NOT NULL) before the backfill populated the hash columns — fail. Same for `4f2a9b6c5d13`. The three-phase block breaks the chain at each safe checkpoint, runs the backfill, then continues.

## Do these commands always run on every deploy? What's the long-term plan?

**Yes, they run every deploy — but after the first successful deploy, they're essentially free no-ops.**

Here's what each line does on subsequent deploys (after the DB is already at head):

| Line | First deploy | Every deploy after |
|------|--------------|--------------------|
| `alembic upgrade 145bebe88ecb` | Applies the migration | No-op (already past — ~1s) |
| `backfill_encrypt_secrets.py` | Encrypts plaintext rows | Re-encrypts already-encrypted rows with same key (~seconds) |
| `backfill_encrypt_payments.py` | Same | Same |
| `backfill_encrypt_messages.py` | Same | Same (scales with message count) |
| `backfill_encrypt_pii.py` | Same | Same |
| `alembic upgrade 3d8e2fa91c07` | Applies the migration | No-op |
| `backfill_encrypt_user_admin.py` | Encrypts users + admins | Re-encrypts (~seconds) |
| `alembic upgrade head` | Applies remaining migrations (NOT NULL tighten) | No-op; or applies any NEW migration you add later |
| `seed_data.py` / `seed_staging_e2e.py` | Seeds data | Idempotent — skips existing rows |

### Adding new migrations later

Any new migration you write goes in `alembic/versions/` and is picked up by `alembic upgrade head` automatically. You don't need to change the CD script.

### When you can simplify

Once production is confirmed fully encrypted (verified with `psql` + `gAAAAA` prefix check) AND `ENCRYPTION_MIGRATION_MODE=false` is set in production `.env`, you can optionally collapse the block back to a single call per deploy:

```bash
docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head
docker compose -f docker-compose.prod.yml run --rm api python /app/scripts/seed_data.py
```

This saves ~10-30 seconds per deploy. Don't simplify before then — you'd lose the re-encryption safety net if any plaintext data somehow appears (e.g., from a rollback or manual SQL insert).

**Keep the backfill scripts in the repo** even after simplification. They're reference tooling for:
- Key rotation (rerun to re-encrypt under a new key)
- Disaster recovery (restore from backup, re-encrypt)
- Adding encryption to new columns in the future

---

## Encryption mode cheat sheet

`ENCRYPTION_MIGRATION_MODE` in each `.env`:

- **`true`** → tolerant: reads that fail decryption fall through to return the raw value. Use during **first deploy** of encryption changes so mixed plaintext/encrypted data can be migrated.
- **`false`** → strict: any read that fails decryption raises an error. Use after all data is confirmed encrypted, to catch bugs (e.g., a code path inserting plaintext) immediately.

### Rollout sequence per environment

1. **First deploy with new encryption**: set `ENCRYPTION_MIGRATION_MODE=true` in the env file, deploy, verify with `psql` spot check (`gAAAAA` prefix on encrypted columns).
2. **Switch to strict mode**: set `ENCRYPTION_MIGRATION_MODE=false`, redeploy. Any remaining plaintext will now throw errors (there shouldn't be any).

---

## Verifying encryption on the server

SSH into the VPS, then:

```bash
cd /opt/luran-staging        # or /opt/luran for prod
source .env

docker compose -f docker-compose.prod.yml exec -T db psql -U "$POSTGRES_USER" -d luran_db <<'SQL'
-- All should show 'gAAAAA...' prefix
SELECT 'action_secrets' AS t, substring(secret_value, 1, 10) FROM action_secrets LIMIT 1;
SELECT 'customer_users' AS t, substring(email, 1, 10), length(email_hash) FROM customer_users LIMIT 1;
SELECT 'users' AS t, substring(email, 1, 10), length(email_hash) FROM users LIMIT 1;
SELECT 'platform_admins' AS t, substring(email, 1, 10), length(email_hash) FROM platform_admins LIMIT 1;
SELECT 'messages' AS t, substring(content, 1, 10) FROM messages LIMIT 1;
SELECT 'visitors' AS t, substring(email, 1, 10) FROM visitors WHERE email IS NOT NULL LIMIT 1;
SELECT 'visitor_sessions' AS t, substring(ip_address, 1, 10) FROM visitor_sessions WHERE ip_address IS NOT NULL LIMIT 1;

-- None of these should be > 0
SELECT 'users null hash' AS check_name, COUNT(*) FROM users WHERE email_hash IS NULL;
SELECT 'admins null hash' AS check_name, COUNT(*) FROM platform_admins WHERE email_hash IS NULL;
SELECT 'customers null hash' AS check_name, COUNT(*) FROM customer_users WHERE email_hash IS NULL;
SQL
```

## Common pitfalls

1. **Local container running stale code.** After editing source files, you must `docker compose build api && docker compose up -d api` before testing. Containers don't hot-reload from mounted volumes unless you've set up a volume mount for `app/` (production uses COPY, not volume).

2. **Empty tables hide bugs.** If local tests pass but staging fails, it's almost always because local tables were empty. Seed data locally (`seed_staging_e2e.py`) before testing to reproduce staging's state.

3. **`.env.staging.enc` out of sync.** If you change `.env.staging` locally but forget to re-encrypt to `.env.staging.enc`, CD ships the old env. Re-encrypt:
   ```bash
   ENV_ENCRYPTION_KEY='<aes-password>' openssl enc -aes-256-cbc -e -pbkdf2 -iter 100000 \
     -in .env.staging -out .env.staging.enc -pass "pass:$ENV_ENCRYPTION_KEY"
   ```

4. **`workflow_run` uses the default branch's CD workflow file.** Changes to `cd.yml` on `develop` don't take effect for auto-triggered staging deploys until merged to `main`. For early testing, use `workflow_dispatch` ("Run workflow" button on GitHub Actions) to run the current branch's workflow file.

5. **Never rotate `ENCRYPTION_KEY` without setting `ENCRYPTION_KEY_PREVIOUS`.** Existing ciphertext becomes unrecoverable. Always save the current key before rotating.
