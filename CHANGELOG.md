# CHANGELOG — WorkTrace platform

Chronological record of WorkTrace's evolution. Each entry covers a single shipped phase, the scope, key files touched, and the design decisions that came out of that phase. Read top-to-bottom for "how did we get here?"

For **current state** (open items, what's still pending), see [`~/Documents/DevPlatform/PROJECT_NOTES.md`](https://github.com/kjain-Cloudforia/worktrace-cli) — local file, admin-only. For the **verbatim conversation logs** that built this system, those live in `~/.claude/projects/-Users-kashishjain-Desktop-Projects-Genserve-Partial/*.jsonl` (chmod 600, per-machine).

---

## Phase 5m — One-command teammate onboarding (May 2026)

**Scope:** collapsed the new-teammate setup from a 7-step manual checklist (~20 min) to a single pasted command + 2 prompts (~3 min).

**What shipped:**
- `install.sh` in worktrace-cli — fetches via `curl | bash`. Asks the teammate for two things (username + PAT). Auto-discovers display name, timezone, shift, data-repo URL from the public auth record admin already provisioned. Clones the worktrace-cli repo, generates `config.json`, clones the user's private data repo with token-in-URL credentials, sources the shell hook into `.zshrc`, runs `sync_claude_md.py` to inject the platform rules.
- `NEW-TEAMMATE.md` — canonical onboarding playbook (admin's part + teammate's part + troubleshooting in one doc).

**Documentation reorg:** every other doc (worktrace-app/ONBOARDING.md, worktrace-cli/README.md, worktrace-auth/CONTRIBUTING.md) was simplified to defer to NEW-TEAMMATE.md instead of duplicating the steps. Single source of truth for the onboarding flow.

**Design decision:** the auth record (public, encrypted credentials) doubles as the source of truth for the teammate's display-side metadata. By the time install.sh runs, admin has already filled the dashboard form with display name, shift hours, timezone, data_repo. The script reads those values from `users/<username>.json` via the GitHub Contents API. Teammate only needs to type their username + paste their PAT.

---

## Phase 5l — Fourth repo + shared CLAUDE rules + auto-propagation (May 2026)

**Scope:** introduced a fourth public repo (`worktrace-cli`) to hold laptop-side scripts AND the canonical CLAUDE.md rules. Built the mechanism to propagate prompt updates from admin → every teammate's laptop automatically.

**What shipped:**
- `worktrace-cli` repo, holding: `dpsync.py`, `shell-hook.zsh`, `modules/timesheet/{sync,parser}.py`, `scripts/{sync_pull,sync_claude_md,build_initial_user_records,build_recovery_artifacts,reset_admin}.py`, `CLAUDE.shared.md`, and the bootstrap docs.
- `CLAUDE.shared.md` — canonical platform rules: work hours rule, timesheet writing style, status-report flow, the auto-sync routine itself, project trigger phrases. Source of truth.
- `scripts/sync_claude_md.py` — marker-based injector. Replaces the contents between `<!-- BEGIN WORKTRACE RULES -->` and `<!-- END WORKTRACE RULES -->` in each teammate's `~/.claude/CLAUDE.md`. Personal sections outside the markers are never touched.
- Auto-sync routine (defined in CLAUDE.shared.md) was extended: it now runs `git pull --rebase --autostash` on `~/Documents/DevPlatform/` (the worktrace-cli clone) before invoking the other scripts. So admin's `git push` of an updated rule reaches every teammate's `~/.claude/CLAUDE.md` on their next shift-start, without them doing anything.

**Design decisions:**
- **Project-level CLAUDE.md isn't enough** — only loads when Claude is opened in `~/Documents/DevPlatform/`. Teammates do daily timesheet work from any workspace (Salesforce repo, whatever). Rules need to be available everywhere → must live in user-level (`~/.claude/CLAUDE.md`).
- **Marker-injection over full-file replacement** — teammates may have personal rules (Salesforce-specific deploy queries, communication preferences, etc.) that should never get touched by upstream sync. Markers preserve those.
- **Admin's experiments stay private until pushed** — uncommitted edits to `CLAUDE.shared.md` are visible only on admin's machine. Standard git workflow gives "private staging" for free; no special tooling needed.

**Bugs caught + fixed during this phase** (worth remembering):
1. **`.gitignore` inline-comment bug** — git only honors `#` as comment when at column 0. Patterns like `sync/ # clone of worktrace-data-<u>` were treating the whole line as the pattern, so embedded clones weren't being ignored. Fixed by moving comments to separate lines.
2. **Marker-mention idempotency bug** — my first `CLAUDE.shared.md` referenced the literal marker strings in its intro paragraph (explaining what they did). On the second `sync_claude_md.py` run, `find()` matched the prose mention inside the managed block instead of the actual end marker, replacing from BEGIN to the prose-END — corrupting the file every run. Fixed by describing the markers conceptually instead of quoting them verbatim.

---

## Phase 5k — Auto-sync at shift-start + deferred shift changes (May 2026)

**Scope:** removed the need for manual `dpsync` runs + added "edit-shift-while-mid-shift" handling.

**What shipped:**
- **Auto-sync routine** in `~/.claude/CLAUDE.md` (later moved to `CLAUDE.shared.md` in Phase 5l). Runs on the first Claude message of each session. Gates via `~/Documents/DevPlatform/.last_synced_at` against today's most-recent shift-start. If stale, runs `sync_pull.py` (pulls dashboard → local config.json) + `dpsync.py` (pushes timesheet → user's data repo) + stamps `.last_synced_at`. Prints `✓ Auto-synced WorkTrace at HH:MM <tz>`.
- `scripts/sync_pull.py` — fetches the user's auth record, resolves the effective work_shift (Phase 5j logic), updates `platform.timezone` + `modules.timesheet.work_hours` in local config.json if they diverge. Conflict policy: dashboard wins.
- `dpsync.py` admin-account guard — exits cleanly with code 0 if no `remote_data_repo` configured. Admin (no personal data repo) can run the auto-sync without errors.

**Deferred shift changes (Phase 5j logic):**
- New optional `work_shift_pending` field on user records: `{start, end, timezone, effective_from}`.
- `auth.js` helpers: `resolveActiveShift()` (lazy-promotes pending → current when effective_from elapses), `isInShift()` (cross-midnight-aware), `computeShiftEndUTC()`.
- Edit-shift modal pre-flight: if user is currently mid-shift, the change writes to `work_shift_pending` with `effective_from = end of current shift`. Otherwise writes directly to `work_shift`.

**Bug caught during smoke test** (cross-midnight gate logic): the first version of the auto-sync gate computed "today's shift_start," which broke for cross-midnight shifts. At 04:00 IST on Day N during a 14:00→05:00 IST shift, "today's 14:00" was 10 hours in the future — but yesterday's 14:00 had fired 14 hours ago and the user was actually mid-shift. Fixed by using `today_shift_start if <= now else today_shift_start - 24h` as the most-recent-shift-start moment.

---

## Phase 5j — Per-user work shift + timezone (May 2026)

**Scope:** moved the work-shift definition from a hardcoded value in CLAUDE.md to a per-user field, so teammates in different timezones / on different schedules can coexist under one platform.

**What shipped:**
- Schema bump: `work_shift = {start, end, timezone}` (IANA timezone name) on the auth-user v1 schema. Optional for backward compat; new records always populate it.
- auth.js `validateWorkShift()` — accepts HH:MM strings + IANA timezone names. Rejects bogus shapes (25:00, etc.). For the timezone check, uses `Intl.DateTimeFormat` (accepts canonical names AND linked aliases) instead of `Intl.supportedValuesOf` (which on some platforms — notably macOS Safari — omits popular aliases like Asia/Kolkata, listing only Asia/Calcutta).
- Dashboard: Edit-shift modal in the header. Admin's create-user form has start/end/timezone fields. Admin Console roster cards show each user's shift.
- Migration: kashish.json + admin.json backfilled with `{14:00, 05:00, Asia/Kolkata}`.
- `~/.claude/CLAUDE.md` updated to read shift from `config.json` instead of hardcoding IST.

**Bug caught: `Asia/Kolkata` rejected by autocomplete dropdown.** macOS Chrome's ICU treats `Asia/Calcutta` as canonical and excludes `Asia/Kolkata` from `Intl.supportedValuesOf('timeZone')`, even though `Intl.DateTimeFormat({timeZone: 'Asia/Kolkata'})` accepts it. Fixed two ways: (1) supplemented the autocomplete datalist with common aliases the browser might omit, (2) switched the validator from membership-in-supportedValuesOf to DateTimeFormat-acceptance.

---

## Phase 5i — Custom password-reveal toggle (May 2026)

**Scope:** replaced the browser-native password-reveal eye with our own implementation.

**Why:** browser-native behavior is inconsistent. Chrome shows the eye while typing; Safari hides it on blur and doesn't bring it back; Firefox doesn't render one at all; password managers paint over the right edge anyway. User reported the icon "appearing once on first keystroke then never again" — Safari's blur-then-no-return behavior.

**What shipped:** `passwordField()` helper in shell.js + admin/module.js. Paints a one-eye-everywhere SVG toggle that flips between password and text modes. Tabindex=-1 so keyboard users still flow input → input. CSS pads the input's right edge to make room for the toggle. ~10 call sites updated to use the helper.

**Bug caught + fixed:** the initial CSS had `-webkit-text-security: disc;` on password inputs, intended to "hide the browser's native reveal control." It actually forces the text-as-dots display regardless of input type. So clicking the toggle flipped `input.type` to "text" but CSS kept showing dots. Removed the property; native reveal hiding is done correctly via `::-ms-reveal { display: none }`.

---

## Phase 5h — Password recovery: admin recovery code + reset-user via escrow (May 2026)

**Scope:** added the recovery mechanism for forgotten passwords. Two flows: admin-forgot-password (uses 24-char recovery code) and user-forgot-password (admin resets via escrow).

**What shipped:**
- `admin.recovery.json` — admin's PAT encrypted under a 24-char Crockford-base32 recovery code (~120 bits entropy). Code generated once at admin setup, shown to admin ONCE, stashed offline (paper + 1Password).
- `escrow/<u>.json` per non-admin user — that user's PAT encrypted under the recovery code. Lets admin reset any user's password without rotating PATs or contacting the user.
- auth.js: `buildRecoveryRecord`, `unlockRecoveryRecord`, `buildEscrowRecord`, `unlockEscrowRecord`, `generateRecoveryCode`, `normalizeRecoveryCode` (handles loose input: hyphens, spaces, lowercase, I→1, L→1, O→0, U→V).
- Dashboard: "Admin: forgot password?" link on login screen → recovery-code modal. Admin Console gains a per-user "Reset password" button.
- Python scripts: `build_recovery_artifacts.py` (mints recovery code + builds escrow), `reset_admin.py` (emergency local fallback when admin can't sign in AND lost recovery code).

**Critical design pivot (within the phase):**
- **First version: escrow encrypted under admin's password.** Worked for the happy path but had a fatal property: every admin password change silently broke all escrow files; admin password *recovery* made escrow permanently undecryptable (since the old password is lost).
- **Final version: escrow encrypted under the recovery code.** Trade-off: admin types the recovery code (from 1Password / paper) every time they reset a user. But escrow survives every password change AND every recovery, no re-key needed. Recovery code only changes when explicitly rotated.

**Bugs caught + fixed:**
1. **Asia/Kolkata-style: GitHub raw CDN serves stale records for minutes after a push** — `raw.githubusercontent.com` keys cache by path and ignores query-string busters. Caused "wrong password" symptoms after password changes. Fixed by switching every auth-file fetch to `api.github.com/contents` with `Accept: application/vnd.github.v3.raw` (no CDN lag, keyed by commit).
2. **Recovery code's PBKDF2 password is the *normalized* form (no hyphens, uppercase).** First version of the reset-user modal passed the raw user input (with hyphens) to `unlockEscrowRecord`, deriving a different key → AES-GCM rejected. Fixed by calling `normalizeRecoveryCode()` at the call site.
3. **`buildEscrowRecord` tripped the password-policy gate** because the normalized recovery code (uppercase Crockford) has no lowercase letters. Fixed by bypassing the policy check inside `buildEscrowRecord` (same bypass that `buildRecoveryRecord` already had).

---

## Phase 5g — Hardening + docs (May 2026)

**Scope:** comprehensive code audit + 5 docs written/updated for handoff-quality state.

**Docs shipped (in worktrace-app):** README.md (full rewrite for current architecture), ONBOARDING.md (new-teammate walkthrough), HOW-IT-WORKS.md (plain-language tour for non-tech readers). In worktrace-auth: README.md (threat-model table, recovery design rationale), CONTRIBUTING.md (admin ops). In `~/Documents/DevPlatform/`: PROJECT_NOTES.md (canonical state file, loaded by trigger phrases) and scripts/README.md.

**Audit findings + fixes** (real bugs caught, not just cleanup):
- Admin module's success-state "Done" button bypassed `closeModal()` (used direct `setAttribute('hidden', '')`), leaving Esc-key handler attached to document — listener leak on every success view. Fixed by threading ctx through `renderCreateSuccess` and `renderResetSuccess` to call `ctx.closeModal()`.
- Hoisting brittleness in shell.js: `PW_EYE_SVG` referenced 21 lines before its const declaration. Worked due to JS hoisting but a tripwire for future edits. Moved declarations above use + factored duplicate click-handler logic into shared `attachPasswordToggle()`.
- Several stale doc-strings + unused constants/functions removed.

---

## Phase 5f — Change-password UI in header (May 2026)

**Scope:** users can rotate their own password from the dashboard header, without needing admin involvement.

**What shipped:** `openChangePasswordModal()` in shell.js. Three fields (old, new, confirm). Re-encrypts the user's PAT under the new password via `rekeyUserRecord` + commits the updated `users/<u>.json` to worktrace-auth. On success, signs the user out (so they sign back in with the new password — avoids state-drift bugs).

---

## Phase 5e — Admin write ops: create + revoke users from dashboard (May 2026)

**Scope:** admin can provision new users + revoke existing users directly from the dashboard, without manual JSON editing.

**What shipped:**
- Admin Console roster gains a "+ Add team member" button → modal with username, display name, data_repo, PAT, initial password, recovery code, work shift fields. Validates upfront: username slug shape, owner/repo shape, recovery-code shape, password policy, username uniqueness (Contents API 404 check), data_repo reachable with the supplied PAT, recovery code unlocks admin.recovery.json (catches typos before writing an escrow nobody could decrypt).
- Per-user "Revoke" button (subdued grey → red on hover). Type-the-username confirmation. Deletes `users/<u>.json` + `escrow/<u>.json` via Contents API. Data repo + PAT are not touched (admin handles those manually if they want full decommission).
- shell.js: `deleteFromAuthRepo()` helper + exposed via `ctx.deleteAuth()` so module code stays out of shell internals.

---

## Phase 5d — Admin module: read-only roster + view-as-X (May 2026)

**Scope:** admin can see every teammate's data without signing in as them.

**What shipped:**
- `modules/admin/module.js` — gated by `requiresAdmin: true` on the export.
- Tile: team aggregates (member count, admin count, data repo count).
- Detail: user cards. Click "View timesheet →" to drill into a teammate's Timesheet detail view (re-uses Timesheet module's `renderDetail()` with a shimmed `ctx` whose `fetchMyData` points at the target user's data repo, using admin's PAT which has org-wide read).
- Timesheet module gains `hideForAdmin: true` — admin doesn't have a personal data repo, so the Timesheet tile is hidden for admin (they access teammates' timesheets via this module instead).
- Module ctx surface gains `ghFetch(ownerRepo, path)` for cross-user reads.

---

## Phase 5c — Encrypted credentials + username/password login (replaced PAT-paste) (May 2026)

**Scope:** replaced the original PAT-paste login (Phase 5b) with a real username/password sign-in. Encrypted credentials live in a public repo because the *ciphertext* is safe to expose.

**What shipped:**
- `worktrace-auth` repo (public). `users/<username>.json` per user — AES-GCM-encrypted GitHub PAT + public metadata (display name, data_repo, is_admin).
- auth.js: `encryptSecret`, `decryptSecret`, `buildUserRecord`, `unlockUserRecord`, `rekeyUserRecord`, `checkPasswordStrength`. PBKDF2 600k iters (OWASP 2023 minimum), AES-GCM 256-bit, 16-byte salt per user, 12-byte IV per record.
- Dashboard login screen: username + password fields. Browser fetches the user's auth file, derives an AES key from the password, decrypts the PAT, caches in sessionStorage.
- `scripts/build_initial_user_records.py` — one-shot bootstrap that mirrors auth.js exactly (PBKDF2 constants, AES-GCM, no AAD). Round-trip verified locally before writing the files.

**Design decisions:**
- **Public repo OK because ciphertext only.** Password never leaves the browser. PBKDF2 600k iters makes offline brute-force computationally infeasible against passwords ≥12 chars mixed case + digit.
- **sessionStorage, not localStorage.** PAT cleared on tab close. Right blast radius for an auto-decrypted credential.

---

## Phase 5b — auth.js Web Crypto wrapper (May 2026)

**Scope:** the crypto layer that everything else stands on.

**What shipped:** `auth.js` with the PBKDF2 + AES-GCM primitives, password-policy enforcement, and a `__selfTest()` exportable function that round-trips through every code path. No external dependencies — pure Web Crypto.

---

## Phase 5a — Per-user data repos (May 2026)

**Scope:** moved from a single shared `worktrace-data` repo to per-user repos (`worktrace-data-<username>`).

**Why:** GitHub-level isolation. Each user only has Write access to their own repo; admin's PAT has Read access to all.

**What shipped:** `dpsync.py` rewrite. Reads `platform.user_id` + `platform.remote_data_repo` from config.json, pushes to that specific user's repo. Naturally extensible: new user = new private repo + new auth record, no central registry update needed.

---

## Phases 1–4 — Foundation (May 2026)

**Phase 1:** Timesheet sync MVP. `dpsync.py`, `modules/timesheet/sync.py`, `parser.py`. Reads local `timesheet.md`, generates structured JSON, pushes to a single shared `worktrace-data` repo. Manual `dpsync` invocation.

**Phase 2:** Dashboard shell + Timesheet tile. `app/docs/index.html`, `shell.js`, `modules/timesheet/module.js`. Vanilla JS, no framework, no build step. Initial login was PAT-paste (replaced in Phase 5c).

**Phase 3:** Skipped — Chart.js / filters / aggregations were optional polish; can roll in later.

**Phase 4:** Skipped — second module TBD whenever business need surfaces.

---

## Phase 0 — Setup (May 2026)

**Scope:** local migration from `~/Documents/Timesheets/` to `~/Documents/DevPlatform/`. Renamed env vars from `TIMESHEET_*` to `DEVPLATFORM_*`. Created the modules/ subtree layout. Restructured `config.json` to a versioned schema (platform + modules + cli_tracking + project_detection blocks). Updated `~/.claude/CLAUDE.md`'s 12 path references. Created `PROJECT_NOTES.md` as the canonical state file with trigger-phrase loading.

---

## What's been deliberately skipped (open items, see PROJECT_NOTES.md for details)

- **PAT rotation** — deferred end-of-project housekeeping. Current `platform.github_token` + `platform.admin_token` in config.json were pasted into chat during Phase 0/5c setup and exist in session JSONLs on disk.
- **Modules 2–4** — TBD whenever specific need surfaces.
- **Audit log of admin actions in dashboard** — currently relying on git history of worktrace-auth.
- **Multi-admin support** — data model supports it (`is_admin: true` on multiple records); dashboard partially does. Reset-self-password is disabled for admin rows; multi-admin would relax this.
- **Recovery-code rotation in the dashboard** — script-only today.
- **Cloudflare Worker / SMTP-backed email recovery** — explicitly deferred. The recovery-code design replaces it for the prototype.
