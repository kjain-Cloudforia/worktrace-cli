# worktrace-cli

**Local laptop layer for the [WorkTrace](https://github.com/kjain-Cloudforia/worktrace-app) platform.** Holds the sync scripts that push your local timesheet to GitHub, the shared CLAUDE.md prompt rules that Claude Code loads on every laptop, and the recovery / admin helper scripts.

This is one of four repos in the WorkTrace architecture:

| Repo | Visibility | Holds |
|---|---|---|
| [`worktrace-app`](https://github.com/kjain-Cloudforia/worktrace-app) | public | Dashboard code (HTML/JS/CSS) served by GitHub Pages |
| [`worktrace-auth`](https://github.com/kjain-Cloudforia/worktrace-auth) | public | Encrypted user credentials (AES-GCM ciphertext) |
| `worktrace-data-<username>` | **private** (one per user) | Each user's actual data files (timesheet, future modules) |
| **`worktrace-cli`** *(this repo)* | **public** | Laptop-side scripts + shared Claude prompt rules |

Public is safe here because nothing in this repo is secret — it's all code + documentation. Your personal config (PAT, timesheet entries) lives in your own gitignored files on your laptop.

---

## What lives in this repo (gets pushed/pulled)

```
worktrace-cli/                          ← cloned to ~/Documents/DevPlatform/ on each laptop
├── README.md                           ← this file
├── CLAUDE.shared.md                    ← canonical platform rules for Claude Code
├── .gitignore                          ← excludes per-user content
├── dpsync.py                           ← pushes timesheet → user's data repo
├── shell-hook.zsh                      ← zsh hooks that passively log CLI commands
├── modules/
│   └── timesheet/
│       ├── sync.py                     ← Timesheet module's data extractor
│       └── parser.py                   ← parses timesheet.md → structured entries
└── scripts/
    ├── README.md                       ← when to run each script
    ├── sync_pull.py                    ← pulls dashboard state → local config.json
    ├── sync_claude_md.py               ← injects CLAUDE.shared.md into ~/.claude/CLAUDE.md
    ├── build_initial_user_records.py   ← admin bootstrap (Phase 5c, one-off)
    ├── build_recovery_artifacts.py     ← admin recovery setup (Phase 5h)
    └── reset_admin.py                  ← admin password emergency reset
```

## What stays LOCAL on each laptop (gitignored, never pushed)

```
~/Documents/DevPlatform/
├── config.json                         ← your PAT, your shift, your project_detection
├── cli-log.jsonl                       ← runtime CLI command log
├── .last_synced_at                     ← auto-sync gate timestamp
├── modules/timesheet/
│   └── timesheet.md                    ← your canonical work-log (this is THE file)
├── sync/                               ← git clone of your worktrace-data-<you>
├── auth/                               ← admin-only: clone of worktrace-auth
├── app/                                ← admin-only: clone of worktrace-app
└── PROJECT_NOTES.md                    ← admin-only: platform state file
```

---

## Setting up a new laptop (new teammate)

**Pre-flight:** admin has already provisioned you via the Admin Console (see [`worktrace-app/ONBOARDING.md`](https://github.com/kjain-Cloudforia/worktrace-app/blob/main/ONBOARDING.md)). You have a username, an initial password, and a GitHub PAT from admin.

```bash
# 1. Clone this repo (becomes ~/Documents/DevPlatform/)
git clone https://github.com/kjain-Cloudforia/worktrace-cli.git ~/Documents/DevPlatform

# 2. Create your personal config.json
cat > ~/Documents/DevPlatform/config.json <<'EOF'
{
  "platform": {
    "user_id":          "<your-username>",
    "display_name":     "<Your Full Name>",
    "timezone":         "<your IANA timezone, e.g. Asia/Kolkata>",
    "github_token":     "<paste your PAT from admin>",
    "remote_data_repo": "git@github.com:<org>/worktrace-data-<your-username>.git",
    "auto_sync":        true
  },
  "modules": {
    "timesheet": {
      "enabled": true,
      "work_hours": { "start": "<HH:MM>", "end": "<HH:MM>" }
    }
  }
}
EOF
chmod 600 ~/Documents/DevPlatform/config.json    # has your PAT — keep readable only to you

# 3. Wire the shell hook (zsh users — auto-logs CLI commands for project attribution)
echo 'source ~/Documents/DevPlatform/shell-hook.zsh' >> ~/.zshrc
source ~/.zshrc

# 4. Clone your private data repo
cd ~/Documents/DevPlatform
git clone "git@github.com:<org>/worktrace-data-<your-username>.git" sync

# 5. Run the first injection of the shared CLAUDE.md rules into your user-level file
python3 scripts/sync_claude_md.py

# 6. (Optional) prime ~/.claude/CLAUDE.md with personal rules — see worktrace-app's
#    ONBOARDING.md for what stays personal vs what gets injected from this repo.

# 7. Open the dashboard, sign in with your initial password, change it.
#    https://kjain-Cloudforia.github.io/worktrace-app/
```

After that, your daily loop is:

- Work normally. When you finish something deliverable, tell Claude *"log today's work"*.
- Auto-sync fires on the first Claude message after each shift-start — pulls latest rules from this repo, pulls dashboard changes, pushes your timesheet.
- Ask Claude *"give me my status report for this week"* whenever you want a consolidated view; Claude will ask before pushing the rebuilt timesheet up to GitHub.

---

## Admin: publishing updates to the shared rules / scripts

When you want every teammate's Claude to behave differently — say, tweaking the timesheet writing style, or adding a step to the auto-sync routine — edit `CLAUDE.shared.md` in this repo:

```bash
cd ~/Documents/DevPlatform
# edit CLAUDE.shared.md (or any script under scripts/, modules/, etc.)
# test it locally — your own machine runs scripts/sync_claude_md.py on each
#   shift-start auto-sync, so your local ~/.claude/CLAUDE.md picks up the
#   edit immediately. Iterate in your Claude sessions until happy.

git add CLAUDE.shared.md
git commit -m "Tweak: <what you changed>"
git push
```

On each teammate's next shift-start, their auto-sync routine runs `git pull` on this repo + `scripts/sync_claude_md.py` to re-inject the latest rules into their user-level CLAUDE.md. Their next Claude session sees the new behavior.

**Experimenting without affecting teammates**: just don't commit. Uncommitted edits to `CLAUDE.shared.md` are visible only on your machine. `git checkout CLAUDE.shared.md` discards an experiment. Teammates only ever see what you've pushed.

**Permanent personal-only overrides**: add them to your own `~/.claude/CLAUDE.md` OUTSIDE the managed block. The auto-sync never touches anything outside the markers.

---

## Where to read more

| Topic | File |
|---|---|
| Dashboard architecture, modules, threat model | [`worktrace-app/README.md`](https://github.com/kjain-Cloudforia/worktrace-app/blob/main/README.md) |
| Plain-language tour for non-tech readers | [`worktrace-app/HOW-IT-WORKS.md`](https://github.com/kjain-Cloudforia/worktrace-app/blob/main/HOW-IT-WORKS.md) |
| New teammate onboarding (admin's side + user's side) | [`worktrace-app/ONBOARDING.md`](https://github.com/kjain-Cloudforia/worktrace-app/blob/main/ONBOARDING.md) |
| Encryption / recovery design | [`worktrace-auth/README.md`](https://github.com/kjain-Cloudforia/worktrace-auth/blob/main/README.md) |
| Admin operations (add/reset/revoke users) | [`worktrace-auth/CONTRIBUTING.md`](https://github.com/kjain-Cloudforia/worktrace-auth/blob/main/CONTRIBUTING.md) |
| When to run which script in `scripts/` | [`scripts/README.md`](./scripts/README.md) |
