# scripts/

Local Python helpers for things the dashboard either can't do or shouldn't do. Three scripts; pick by symptom.

All three mirror the JavaScript crypto in [`worktrace-app/docs/auth/auth.js`](https://github.com/kjain-Cloudforia/worktrace-app/blob/main/docs/auth/auth.js) exactly — same PBKDF2 (600k iters, HMAC-SHA-256), same AES-GCM-256, same 16-byte salt + 12-byte IV, no AAD. Each script round-trip-verifies what it built before writing it to disk, so a constant drift between Python and JS gets caught locally, not after a failed push.

## Decision flow

```
Need to bootstrap auth from scratch?
  └─▶ build_initial_user_records.py        (one-off; creates kashish.json + admin.json)

Need to set up password recovery for the first time, or rotate
the existing recovery code?
  └─▶ build_recovery_artifacts.py          (creates admin.recovery.json + every escrow/<u>.json)

Admin can't sign in AND lost the recovery code, but the laptop
still has config.json with the admin PAT?
  └─▶ reset_admin.py                        (rewrites admin.json under a new password)
```

If none of these match: you probably want the **Admin Console** tile in the dashboard. Almost every routine operation (add user, reset user, revoke user, change password, admin self-recovery) is there now. These scripts are for bootstrap + break-glass only.

## Setup (one-time)

Scripts depend on `cryptography` (Python's authoritative crypto lib). Install once:

```bash
python3 -m pip install --user cryptography
```

All scripts read from `~/Documents/DevPlatform/config.json` and write into `~/Documents/DevPlatform/auth/` (which must be a clone of [`worktrace-auth`](https://github.com/kjain-Cloudforia/worktrace-auth)).

After any script run, you commit + push from `~/Documents/DevPlatform/auth/` yourself — the scripts never call git, so they can't accidentally cause merge conflicts or auth issues.

---

## `build_initial_user_records.py`

**When to run:** exactly once, at project bootstrap. After that, the Admin Console handles new-user provisioning.

**What it does:**
1. Reads `platform.github_token` (kashish's PAT) and `platform.admin_token` (admin's PAT) from `config.json`.
2. Prompts you twice for kashish's password and twice for admin's password (with policy enforcement: ≥12 chars, mixed case, digit).
3. Encrypts each PAT under its respective password using PBKDF2 + AES-GCM, mirroring `auth.js`.
4. Round-trip verifies: decrypts each ciphertext back, asserts the plaintext matches the original PAT.
5. Writes `auth/users/kashish.json` and `auth/users/admin.json`.

You then commit + push:

```bash
cd ~/Documents/DevPlatform/auth
git add users/
git commit -m "Bootstrap initial user records"
git push
```

**When NOT to run:** if `auth/users/kashish.json` or `auth/users/admin.json` already exist on GitHub. Running this overwrites them; if you've forgotten the passwords, the existing files become permanently undecryptable and every user has to re-sign-in with new credentials.

---

## `build_recovery_artifacts.py`

**When to run:**
- **First time:** after `build_initial_user_records.py`, to set up admin password recovery + escrow files.
- **Rotating the recovery code:** if the existing code is compromised, or you want to mint a fresh one as a precaution.

**What it does:**
1. Reads kashish's PAT and admin's PAT from `config.json`.
2. *(First-time path)* Generates a fresh 24-char Crockford-base32 recovery code and prints it ONCE.
3. *(Rotation path)* Prompts for the existing recovery code (needed to decrypt existing escrow files).
4. Encrypts admin's PAT under the recovery code → writes `auth/admin.recovery.json`.
5. Encrypts kashish's PAT under the recovery code → writes `auth/escrow/kashish.json`.
6. Round-trip verifies both.

**Critical:** the recovery code is printed exactly once. Stash it in 1Password / paper / wherever you keep break-glass codes BEFORE you close the terminal. There is no other copy.

You then commit + push:

```bash
cd ~/Documents/DevPlatform/auth
git add escrow/ admin.recovery.json
git commit -m "Build recovery artifacts"
git push
```

**Notes:**
- The script only handles kashish's escrow today. When the team has more users, extend the script to iterate through every entry in `auth/users/` and build an escrow for each (using each user's PAT plaintext, which admin has from when they generated it).
- Rotating the recovery code rebuilds *every* escrow file under the new code — admin must commit + push the entire `escrow/` directory.

---

## `reset_admin.py`

**When to run:** **emergency fallback only.** Admin lost their password AND lost the recovery code AND can't use the dashboard's "Forgot password?" flow, but the laptop still has `~/Documents/DevPlatform/config.json` intact (with `platform.admin_token` in plaintext).

**What it does:**
1. Reads the admin PAT from `config.json`.
2. Prompts for a new admin password (twice; policy-enforced).
3. Re-encrypts the admin PAT under the new password → rewrites `auth/users/admin.json`.
4. Round-trip verifies.
5. Prints the exact git commit/push commands.

**What it does NOT do:**
- Doesn't touch escrow files. If the recovery code is also lost, escrow remains orphaned — run `build_recovery_artifacts.py` afterward to mint a fresh code + rebuild escrow.
- Doesn't call git. You commit + push yourself.

After running:

```bash
cd ~/Documents/DevPlatform/auth
git add users/admin.json
git commit -m "Local admin password reset (recovery script)"
git push
```

Then sign in to the dashboard with the new password. Then run `build_recovery_artifacts.py` to mint a fresh recovery code so the dashboard's "Forgot password?" flow works again.

---

## What if the laptop is also gone?

Worst-case scenario: no laptop, no recovery code, no password. You can still recover everything, but it's a full bootstrap:

1. On GitHub Developer Settings, generate a fresh admin PAT with the same scopes (`Contents: Read+Write` on `worktrace-auth` + every `worktrace-data-*` repo).
2. Set up `~/Documents/DevPlatform/` on the new laptop (clone `worktrace-auth` into `auth/`, populate `config.json` with the fresh PAT).
3. Run `build_initial_user_records.py` to rebuild `admin.json` under a new admin password.
4. Run `build_recovery_artifacts.py` to mint a fresh recovery code.
5. For each existing user: since their old escrow file was encrypted under the *old* recovery code (which is gone), and their old `users/<u>.json` is still encrypted under their password (which they presumably remember), the cleanest path is:
   - Each user signs in once with their existing password to confirm they still know it.
   - Admin then rebuilds their escrow under the new recovery code (this requires the user's PAT plaintext, which admin still has from when they generated it originally — store these somewhere durable if you want to preserve this option).
6. If a user's PAT is also lost, regenerate their PAT on GitHub + re-provision them via the Admin Console.

This is the irreducible cost of choosing a backend-free architecture. A cloud-hosted recovery service would shorten step 1-2; everything else would be the same.

## Mirror these constants exactly when porting

If you ever rewrite a script in another language, the JS `auth.js` is the canonical source. The constants that must match:

| Constant | Value |
|---|---|
| `KDF_NAME` | `PBKDF2-HMAC-SHA256` |
| `KDF_ITERATIONS` | `600_000` |
| `KDF_SALT_LEN` | `16` bytes |
| `AES_IV_LEN` | `12` bytes |
| `KDF_KEY_LEN` | `32` bytes (256-bit AES) |
| AAD | `null` (none) |
| `SCHEMA_VERSION` | `1` |
| Crockford alphabet (recovery code) | `0123456789ABCDEFGHJKMNPQRSTVWXYZ` (no I/L/O/U) |
| Recovery code length | 24 chars (formatted as 6 hyphenated groups of 4) |
| Normalization on input | strip hyphens/spaces, uppercase, map I→1, L→1, O→0, U→V |

The round-trip test inside each script catches drift on these — don't disable it.
