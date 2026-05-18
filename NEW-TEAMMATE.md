# Onboarding a new teammate

**This is the one doc for the new-teammate flow.** It covers the admin's part (5 min on GitHub + dashboard) and the teammate's part (3 min on their laptop via `install.sh`). Other docs link here instead of duplicating the steps.

---

## TL;DR

| Who | Time | What |
|---|---|---|
| **Admin** | ~5 min | Create the teammate's data repo + PAT on GitHub, then provision them via the dashboard's Admin Console. Send them three values out-of-band. |
| **Teammate** | ~3 min | Paste one curl command. Answer two prompts (username + PAT). Sign in to the dashboard. Done. |

---

## Part 1 — Admin's checklist (~5 min)

### Step 1a — On GitHub (your org)

**Create the teammate's private data repo.** Settings on the new repo:
- **Name:** `worktrace-data-<their-username>` (e.g. `worktrace-data-xyz`)
- **Owner:** `kjain-Cloudforia` (the org)
- **Visibility:** Private
- **Initialise with a tiny README** (one click)
- **Add the teammate's GitHub account as a collaborator** with **Write** access. They need to accept the invitation before they can sync.

**Generate a fine-grained PAT for them** (under your account):
- GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → **Generate new**
- **Token name:** `worktrace-<their-username>`
- **Resource owner:** your org (`kjain-Cloudforia`)
- **Repository access:** **Only select repositories** → `worktrace-data-<their-username>` + `worktrace-auth`
- **Repository permissions:**
  - `worktrace-data-<their-username>` → **Contents: Read and write**
  - `worktrace-auth` → **Contents: Read and write** (so they can rotate their own password later)
- **Expiration:** 365 days (the maximum for fine-grained tokens)
- Copy the generated token string — it starts with `github_pat_`. You'll need it once in step 1b and once when you message the teammate.

### Step 1b — In the dashboard

- Open https://kjain-Cloudforia.github.io/worktrace-app/
- Sign in as admin → click the **Admin Console** tile → **+ Add team member**
- Fill the form:

| Field | What to enter |
|---|---|
| Username | The slug (`xyz`) |
| Display name | `XYZ Person` |
| Data repo | `kjain-Cloudforia/worktrace-data-<their-username>` (auto-fills from username) |
| User's GitHub PAT | Paste from step 1a |
| Initial password | Something memorable to dictate, min 12 chars, mixed case + digit |
| Recovery code | Your admin recovery code (the 24-char one in 1Password) |
| Work shift start / end / timezone | The teammate's actual schedule |

- Click **Create user**. The success screen shows the initial password with a Copy button.

### Step 1c — Message the teammate (out-of-band, NOT email)

Send via Slack DM, Signal, in person, etc. — wherever you trust the channel:

```
Welcome to WorkTrace! Three values for setup:

  Username:  xyz
  Password:  <initial password from the dashboard>
  PAT:       github_pat_11ABC...   (the long token string)

Setup is one command on your laptop:

  curl -fsSL https://raw.githubusercontent.com/kjain-Cloudforia/worktrace-cli/main/install.sh | bash

It'll ask you for your username and PAT — paste the values above when prompted.
After it finishes: open https://kjain-Cloudforia.github.io/worktrace-app/,
sign in with your password, and rotate it via "Change password" in the header.
```

If you're walking them through it on a screenshare, that's all the prep you need.

---

## Part 2 — Teammate's checklist (~3 min)

### Step 2a — Prerequisites

Your laptop needs:
- macOS or Linux (Windows works in WSL but isn't actively tested)
- Python 3.10 or newer
- `git` and `curl`
- [Claude Code](https://claude.com/claude-code) installed and signed in to the team's shared Anthropic account
- [zsh](https://en.wikipedia.org/wiki/Z_shell) is the default on modern macOS; on Linux it's optional (the script falls back to bash if zsh isn't there)

You already have most of these on a typical developer Mac. If `python3 --version` and `git --version` work in Terminal, you're set.

### Step 2b — Paste the install one-liner

Open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/kjain-Cloudforia/worktrace-cli/main/install.sh | bash
```

You'll see:

```
WorkTrace teammate setup
This will install scripts + rules under ~/Documents/DevPlatform and configure your laptop.

Step 1 of 2: Your WorkTrace username
  (admin assigned this to you — a short lowercase slug like 'kashish' or 'alice')
  Username:
```

Type your username (e.g. `xyz`) and hit Enter. The script fetches your account info from the worktrace-auth repo (public, no auth needed for the lookup) and shows what it found:

```
✓ Found you:
  Display name:  XYZ Person
  Role:          user
  Data repo:     kjain-Cloudforia/worktrace-data-xyz
  Work shift:    09:00–18:00 America/Los_Angeles

Step 2 of 2: Your GitHub PAT (the long string admin sent you)
  Starts with 'github_pat_'. Paste it; it won't be echoed.
  PAT:
```

Paste your PAT (input is hidden — that's normal, no asterisks). Hit Enter. The script:

- Verifies your PAT works against your data repo (catches typos before doing anything irreversible)
- Clones the worktrace-cli repo into `~/Documents/DevPlatform/`
- Writes `~/Documents/DevPlatform/config.json` with your auto-discovered values + PAT (`chmod 600`)
- Clones your private data repo into `~/Documents/DevPlatform/sync/` (with the PAT embedded so future pushes don't prompt)
- Adds the shell hook to your `~/.zshrc` (passively captures CLI activity for project attribution)
- Injects the WorkTrace platform rules into your `~/.claude/CLAUDE.md` (between markers — any personal rules you add later stay safe)

Total time: 30–60 seconds depending on network.

When it finishes you'll see:

```
✓ Setup complete — welcome to WorkTrace, XYZ Person!

Next:
  1. Open https://kjain-Cloudforia.github.io/worktrace-app/
  2. Sign in: username 'xyz' / the initial password admin gave you
  3. Use the 'Change password' button in the header to rotate to your own
  4. Open a new terminal so the shell hook activates
```

### Step 2c — Sign in and change your password

Open the dashboard URL, sign in with `xyz` + your initial password. You'll see your Timesheet tile with *"No timesheet pushed yet"* — that's expected, you haven't logged anything yet.

Click **Change password** in the top-right header. Enter:
- **Current password:** the initial one from admin
- **New password:** something only you know (min 12 chars, mixed case, at least one digit)

You're done. From now on, every time you open Claude Code on your laptop and start a new conversation, the auto-sync routine runs once per work-day to pull the latest rules and push your timesheet.

---

## What the script actually does (for the curious / for troubleshooting)

The full source is at [`install.sh`](./install.sh) — about 250 lines of well-commented bash. The flow:

1. **Sanity check** — verifies `python3`, `git`, `curl` are present.
2. **Prompt for username** — must be a valid slug (lowercase letters + digits + hyphens).
3. **Fetch the auth record** via `curl` from `api.github.com/repos/kjain-Cloudforia/worktrace-auth/contents/users/<username>.json` (Accept: vnd.github.v3.raw). This is a public read, no auth needed. Pulls down `display_name`, `data_repo`, `work_shift`, `is_admin`.
4. **Show what was found** — gives you a chance to abort if it looks wrong.
5. **Prompt for PAT** (hidden input).
6. **Probe the PAT** — calls `api.github.com/repos/<data_repo>` with `Authorization: Bearer <PAT>`. Expects HTTP 200. If 401/403, the PAT lacks access; if 404, repo doesn't exist. Both are fatal with clear error messages.
7. **Clone worktrace-cli** into `~/Documents/DevPlatform/`. If it's already there as a git repo, just `git pull` instead.
8. **Generate `config.json`** via Python (json.dumps) with all the values, set `chmod 600`.
9. **Clone the data repo** with token-in-URL (`https://<PAT>@github.com/<data_repo>.git`). The token ends up in `sync/.git/config` which the script also `chmod 600`s.
10. **Add shell-hook line** to `~/.zshrc` (or fallback rcfile) — skipped if already present.
11. **Run `sync_claude_md.py`** — injects the WorkTrace platform rules into the managed-block area of `~/.claude/CLAUDE.md`.
12. **Print the final summary** + next steps.

Idempotent: re-running is safe. Existing `config.json` is preserved unless you pass `--force`.

---

## Troubleshooting

### "No WorkTrace account found for username 'xyz'."

The auth-record lookup returned 404. Two likely causes:
- **Admin hasn't provisioned you yet.** Ping them — they need to complete Part 1b above.
- **Typo in the username.** Try again, exactly as admin spelled it.

### "PAT cannot access kjain-Cloudforia/worktrace-data-xyz (HTTP 403)."

The PAT is rejected by GitHub. Causes:
- **Wrong PAT** — you pasted a different token. Confirm with admin.
- **PAT scope is wrong** — admin issued it without `Contents: Read+Write` on your data repo. Admin re-issues, you re-run.
- **You haven't accepted the GitHub collaborator invitation** for your data repo. Check your GitHub notifications / email.

### "Repo kjain-Cloudforia/worktrace-data-xyz not found by your PAT (HTTP 404)."

Admin set the wrong repo on your auth record, or the repo doesn't exist. Admin re-creates / re-provisions.

### "~/Documents/DevPlatform exists and is non-empty but not a git repo."

You ran the install partially before and have some leftover files. Move or delete `~/Documents/DevPlatform/` and re-run. Your home dir contents elsewhere are unaffected.

### Script finished but I can't sign in to the dashboard

- Confirm you're typing your username (the slug) and the **initial password admin gave you**, not your PAT.
- Check that the file `~/Documents/DevPlatform/config.json` exists and `cat` shows your username + PAT.
- Open browser DevTools → Network tab → look for the failed `users/<your-username>.json` fetch. If it 404s, admin didn't provision you correctly.

### "Auto-sync at HH:MM" never appears in Claude

- The auto-sync fires on the **first message of a Claude session** after your shift-start has fired today. If you opened Claude before your shift_start, no sync.
- Check `~/Documents/DevPlatform/.last_synced_at` — if its timestamp is later than your today's shift_start, the gate intentionally skipped. Delete the file to force-sync next session.

---

## What happens after onboarding

Once your laptop is set up, your daily flow is:

- **Work normally.** Use Claude in any project workspace, Salesforce repo, wherever.
- **Log work**: tell Claude *"log today's work — I deployed X"* whenever you finish something deliverable. Claude appends a bullet to your local `~/Documents/DevPlatform/modules/timesheet/timesheet.md`.
- **Status report**: tell Claude *"give me my status report for today"* (or yesterday, this week, etc.). Claude reconstructs from your timesheet + Claude session history + CLI logs, auto-appends anything missing, presents the report, and asks if you want to push to GitHub. Click Yes → dashboard reflects within seconds.
- **Auto-sync**: on the first Claude message after each shift-start, an auto-sync runs once per day. It pulls the latest CLAUDE rules (so admin's prompt updates flow to you), pulls your shift settings from the dashboard (so admin-side edits propagate), and pushes your local timesheet to GitHub. Visible as `✓ Auto-synced WorkTrace at HH:MM <tz>` in your first response.
- **Edit your shift**: in the dashboard, **Edit shift** button in the header. Changes mid-shift get deferred to end-of-shift automatically.
- **Forgot password**: ping admin. They use Admin Console → Reset password (takes ~30 seconds).

## Related documents

For deeper context after you're set up:
- [`worktrace-app/README.md`](https://github.com/kjain-Cloudforia/worktrace-app/blob/main/README.md) — platform architecture, module contract
- [`worktrace-app/HOW-IT-WORKS.md`](https://github.com/kjain-Cloudforia/worktrace-app/blob/main/HOW-IT-WORKS.md) — plain-language tour for non-tech readers
- [`worktrace-auth/README.md`](https://github.com/kjain-Cloudforia/worktrace-auth/blob/main/README.md) — encryption + threat model
- [`worktrace-cli/README.md`](./README.md) — what's in this repo + admin workflow for publishing rule updates
