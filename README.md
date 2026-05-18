# worktrace-cli

**Local laptop layer for the [WorkTrace](https://github.com/kjain-Cloudforia/worktrace-app) platform.** Holds the sync scripts that push your local timesheet to GitHub, the shared CLAUDE.md prompt rules that Claude Code loads on every laptop, the marker-injection helper that distributes those rules, and the recovery / admin helper scripts.

This is one of four repos in the WorkTrace architecture:

| Repo | Visibility | Holds |
|---|---|---|
| [`worktrace-app`](https://github.com/kjain-Cloudforia/worktrace-app) | public | Dashboard code (HTML/JS/CSS) served by GitHub Pages |
| [`worktrace-auth`](https://github.com/kjain-Cloudforia/worktrace-auth) | public | Encrypted user credentials (AES-GCM ciphertext) |
| `worktrace-data-<username>` | **private** (one per user) | Each user's actual data files (timesheet, future modules) |
| **`worktrace-cli`** *(this repo)* | **public** | Laptop-side scripts + shared Claude prompt rules + the install script |

Public is safe here because nothing in this repo is secret — it's all code + documentation. Your personal config (PAT, timesheet entries) lives in your own gitignored files on your laptop.

---

## Onboarding a new teammate

**See [`NEW-TEAMMATE.md`](./NEW-TEAMMATE.md) — the single canonical doc for the full admin → teammate flow.** TL;DR: admin spends ~5 min on GitHub + the dashboard's Admin Console, teammate spends ~3 min running the install one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/kjain-Cloudforia/worktrace-cli/main/install.sh | bash
```

---

## What lives in this repo (gets pushed/pulled)

```
worktrace-cli/                          ← cloned to ~/Documents/DevPlatform/ on each laptop
├── README.md                           ← this file (architecture pointer)
├── NEW-TEAMMATE.md                     ← canonical onboarding playbook
├── CLAUDE.shared.md                    ← canonical platform rules for Claude Code
├── install.sh                          ← one-command teammate onboarding
├── .gitignore                          ← excludes per-user content
├── dpsync.py                           ← pushes timesheet → user's data repo
├── shell-hook.zsh                      ← zsh hooks that passively log CLI commands
├── modules/
│   └── timesheet/
│       ├── sync.py                     ← Timesheet module's data extractor
│       └── parser.py                   ← parses timesheet.md → structured entries
└── scripts/
    ├── README.md                       ← when to run each script
    ├── sync_pull.py                    ← pulls dashboard work_shift → local config.json
    ├── sync_claude_md.py               ← injects CLAUDE.shared.md → ~/.claude/CLAUDE.md
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

## How shared rules flow to every teammate's laptop

This is the Phase 5l mechanism that lets admin's prompt updates propagate automatically.

```
Admin (Kashish) edits CLAUDE.shared.md     →   git commit && git push
                                                       │
                                                       ▼
                                          worktrace-cli on GitHub (canonical source)
                                                       │
                                                       │  Next teammate's shift-start...
                                                       │  Auto-sync routine runs:
                                                       ▼
                                          git pull on their local ~/Documents/DevPlatform/
                                                       │
                                                       ▼
                                          scripts/sync_claude_md.py injects
                                          the updated CLAUDE.shared.md content
                                          between markers in ~/.claude/CLAUDE.md
                                                       │
                                                       ▼
                                          Their NEXT Claude session uses the new rule.
```

**Two storage layers:**
- **`CLAUDE.shared.md` in this repo** — the canonical text everyone gets.
- **Each teammate's `~/.claude/CLAUDE.md`** — has a managed block between marker lines that `sync_claude_md.py` keeps in sync with the canonical source. Anything outside the markers is the teammate's personal config (overrides, stack-specific rules, communication preferences) and is never touched.

**For admin: publishing a rules update**

```bash
cd ~/Documents/DevPlatform
# edit CLAUDE.shared.md — add/remove/change anything
# test on your own machine: the auto-sync re-injects on your next shift,
#   or run scripts/sync_claude_md.py now to see the change in your CLAUDE.md
#   immediately + reload Claude

git add CLAUDE.shared.md
git commit -m "Tweak: <what you changed>"
git push
```

Teammates see the update on their next shift-start auto-sync — automatically.

**Experimenting without affecting teammates**: just don't commit. Uncommitted edits to `CLAUDE.shared.md` are visible only on your machine. `git checkout CLAUDE.shared.md` discards an experiment. Teammates only ever see what you've pushed.

**Permanent personal-only overrides**: add them to your own `~/.claude/CLAUDE.md` OUTSIDE the managed-block markers. The auto-sync never touches anything outside the markers.

---

## Where to read more

| Question | File |
|---|---|
| How do I onboard a new teammate? | [`NEW-TEAMMATE.md`](./NEW-TEAMMATE.md) |
| Dashboard architecture, modules, threat model | [`worktrace-app/README.md`](https://github.com/kjain-Cloudforia/worktrace-app/blob/main/README.md) |
| Plain-language tour for non-tech readers | [`worktrace-app/HOW-IT-WORKS.md`](https://github.com/kjain-Cloudforia/worktrace-app/blob/main/HOW-IT-WORKS.md) |
| Encryption / recovery design | [`worktrace-auth/README.md`](https://github.com/kjain-Cloudforia/worktrace-auth/blob/main/README.md) |
| Admin operations (create / reset / revoke users) | [`worktrace-auth/CONTRIBUTING.md`](https://github.com/kjain-Cloudforia/worktrace-auth/blob/main/CONTRIBUTING.md) |
| When to run which script in `scripts/` | [`scripts/README.md`](./scripts/README.md) |
