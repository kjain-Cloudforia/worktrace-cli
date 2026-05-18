# WorkTrace platform rules (shared)

**Source of truth:** this file in `worktrace-cli/CLAUDE.shared.md`. Every teammate's `~/.claude/CLAUDE.md` carries an auto-injected copy of these rules inside the WORKTRACE-managed block (a pair of HTML-comment marker lines bracketing this content). Admin (Kashish) edits this file in the repo, commits, pushes — teammates' next auto-sync pulls and re-injects automatically.

Personal rules + stack-specific stuff (e.g. Salesforce verification queries, individual communication style) stay outside the managed block in each teammate's user-level CLAUDE.md and are never touched by this sync.

---

## Work hours

**Read the shift from `~/Documents/DevPlatform/config.json` on every relevant request — never hardcode a timezone.** Phase 5j made this per-user so teammates in different timezones / on different schedules coexist cleanly under one shared Claude account.

- **Authoritative source:** `~/Documents/DevPlatform/config.json`:
  - `modules.timesheet.work_hours.start` and `.end` — HH:MM (24-hour), local to the user's timezone.
  - `platform.timezone` — IANA name (e.g. `Asia/Kolkata`, `America/Los_Angeles`, `Europe/London`, `UTC`).
- **Cross-midnight shifts:** when `end < start`, the shift spans midnight (e.g. `14:00 → 05:00`). The work day's calendar label is the start-day, not the end-day.
- **Whenever computing "today", "yesterday", "this week"** for timesheets, status reports, or "what did we do" recaps — always use the shift boundaries from `config.json`. Never UTC midnight, never calendar midnight, never assume a timezone.
- **Fallback:** if `config.json` is missing or doesn't have these fields, abort the calculation and ask the user to confirm their shift. Don't guess.
- **Dashboard-side copy:** each user's `work_shift` also lives on their `worktrace-auth/users/<u>.json` record (set via the dashboard's "Edit shift" modal). `config.json` is the laptop's source of truth; the auto-sync pulls dashboard changes down. If they look out of sync, surface that to the user.

## Timesheet — canonical location + format

- **Canonical path:** `~/Documents/DevPlatform/modules/timesheet/timesheet.md`. Never create local-project copies.
- **Format:** weekly sections (Mon–Sun), one entry per work day, project sub-headers, bullet points.
- **Project sub-header format:** `**Project: <Company Name> (<Friendly Name>)**` — the official company/org name, followed by the friendly alias from `config.json`'s `project_detection.company_to_project` in parens. If no friendly alias exists, drop the parens. This makes a non-technical reader recognise both the legal entity and the internal nickname at a glance.

## Timesheet — writing style

**Non-technical, expansive, outcome-focused.** Each entry should read like an internal weekly status the user could forward to a client without editing.

- **Audience is a non-coder reader** (PM, team lead, client, future-self skimming). Stack-domain terms common in the user's daily work are fine; deeper platform jargon (class names, internal field API names, component/handler names, framework specifics) is not.
- **Each bullet expands to 2–4 lines** (more when warranted) explaining *what changed and why* in plain language — not just *what* was touched.
- **Numbers when they matter** (record counts, user counts, deploy counts).
- **Include rationale where non-obvious** (e.g. "previously the field was hardcoded, so any rename in Setup required a redeploy — now it reads from the org at page-load time").
- **Inclusion test ("did we actually change anything?"):** an item belongs in the timesheet only if the work day produced a **change with a deliverable** — a deploy, an admin action that mutated production state (user/perm-set provisioning, debug traces, picklist edits, etc.), or a tangible artifact (decision recorded, finished investigation that informed a deploy). **Do NOT include items just because we looked at them, retrieved them, or referenced them.** Touching a local file isn't a change; landing a modification in the system is. If unsure — ask the user, don't speculate.
- **Verb accuracy ("built/created" vs "modified/updated"):** reserve *built/created/new* for artifacts genuinely created during this work day; everything else is *updated, modified, enhanced, refactored, added X to existing Y, fixed*. **How to verify:** check the artifact's creation timestamp in the system of record (not the local repo — local files can be retrieved any time and don't reflect production lineage). If created-time falls inside the current work-day window → genuinely new. Otherwise pre-existing → modification. Specific verification queries are stack-specific and live in each teammate's user-level CLAUDE.md.
- **Exclude Claude-meta work from the timesheet:** edits to `~/.claude/CLAUDE.md`, this `CLAUDE.shared.md`, project-scoped memory files in `~/.claude/projects/*/memory/`, MEMORY.md indices, `~/Documents/DevPlatform/PROJECT_NOTES.md`, or other AI-assistant config/workflow rules are NOT timesheet entries. They're internal to the assistant's behavior, not client-deliverable work. Same applies to rewriting the timesheet itself for clarity — that's editorial, not work-product. Mention them only if explicitly asked.

## Three pre-flight checks before writing any timesheet entry

Do these every time, no shortcuts:

1. **Check current date/time** — run `date` and read the actual current local time. Don't rely on stale timestamps from earlier in the conversation. Compute which shift window applies *using the shift values from `config.json`* before assigning a work day:
   - If `now-time-in-user-tz < shift.end` (and shift crosses midnight) → previous calendar day's work day.
   - If `now-time-in-user-tz >= shift.start` → current calendar day's work day.
   - Between `shift.end` and `shift.start` → off-shift; flag this to the user and let them decide which day to bucket the work under.

2. **Check project attribution** — every work item must be tied to an explicit project. The rules for *how* to attribute (e.g. parsing CLI flags, resolving org IDs, looking up friendly names) are stack-specific and live in each teammate's user-level CLAUDE.md. Generic principle: never assume project from the workspace folder name alone.

3. **Check that the work landed in the system of record, not just locally** — an item only counts if the work was deployed/committed/persisted somewhere a stakeholder can see it. Verification path:
   - **For new artifacts ("created/new" claim):** query the system of record for the artifact's creation time. If creation-time falls inside the work-day window → counts. If the artifact doesn't exist in the system → it was never deployed → does NOT count.
   - **For modifications:** confirm a deploy/commit/push actually happened (via CLI logs, session evidence, or LastModified timestamps falling inside the window).
   - **Local-only changes don't count.** Saving a file to disk, scaffolding a class, retrieving for review — none of these are work-product.

---

## When user asks for a status report ("status report", "timesheet", "what did I work on today/yesterday/this week", similar)

1. Read `~/Documents/DevPlatform/modules/timesheet/timesheet.md`.
2. Cross-reference with what was actually done in this session AND with prior-day session JSONLs at `~/.claude/projects/*/*.jsonl`. Filter session files whose `mtime` falls inside the target shift window(s). Parse each candidate session with Python: read events where `type == 'user'` and content is text (not a `tool_result` string starting with `<`) to reconstruct what was worked on. The session's project is its parent-directory name (encoded path); resolve to a friendly name via `~/Documents/DevPlatform/config.json` `project_detection` (org-based / stack-specific attribution — see user-level CLAUDE.md for the stack rules).
3. Cross-reference with `~/Documents/DevPlatform/cli-log.jsonl` — filter events whose `ts` falls inside the relevant shift window(s); group by `project`. This surfaces deploys/DML the user ran outside any Claude session.
4. Bucket entries by the shift boundaries above.
5. Present a **consolidated report across all projects** — even if invoked from one project's workspace.
6. **AUTO-APPEND any reconstructed-but-unlogged work-day entries to `~/Documents/DevPlatform/modules/timesheet/timesheet.md`** — do NOT just offer. Idempotent: if a `### Day N — <Weekday> YYYY-MM-DD ...` header already exists for that work day, append bullets under existing project sub-headers (or add new project sub-headers) instead of creating a duplicate day section. For days with **no** session/cli-log evidence, leave a short placeholder line asking the user to confirm if anything happened — don't fabricate.
7. **ALWAYS ask the user whether to sync to GitHub.** Non-negotiable. After showing the report and the auto-append, call `AskUserQuestion` with a single Yes/No question: *"Sync this to GitHub now? Runs `dpsync.py` — pushes the latest `timesheet.md` to your data repo so the dashboard reflects it."* If Yes → run `python3 ~/Documents/DevPlatform/dpsync.py`. If No → do nothing; local stays updated, GitHub stays at last push. Ask must happen even if no reconstructions were appended (the user may have manually edited the file). **ALWAYS ASK.**

## When user asks to "log today's work" / "save to timesheet" / similar

1. Append a brief bullet-pointed entry to `~/Documents/DevPlatform/modules/timesheet/timesheet.md` under the current week / current work day / this project's sub-header.
2. Identify the project per the user-level CLAUDE.md's stack-specific attribution rules (the platform principle is "tied to an explicit system, not the folder name").
3. Don't duplicate entries — if an entry for the same work day + project already exists, append bullets to that section instead of creating a new section.

## Auto-sync routine (Phase 5k)

**On the first user message of every Claude session, BEFORE responding to the user's actual request, run the auto-sync check.** This keeps each teammate's laptop aligned with the dashboard AND with the shared `worktrace-cli` repo (the source of these rules) without anyone having to run scripts manually.

The check is gated by `~/Documents/DevPlatform/.last_synced_at`. If today's most-recent shift-start has fired since that timestamp (or the file doesn't exist), the sync runs. Otherwise no-op silently.

**The check procedure (run as one Bash command):**

```bash
python3 << 'PY' 2>&1
import json, sys, subprocess
from datetime import datetime, timedelta, timezone as tz_mod
from pathlib import Path
from zoneinfo import ZoneInfo

DP = Path.home() / "Documents" / "DevPlatform"
CONFIG = DP / "config.json"
LAST_SYNCED = DP / ".last_synced_at"

if not CONFIG.exists():
    print("auto-sync: no config.json — first-run laptop or not a WorkTrace user, skipping.")
    sys.exit(0)

cfg = json.loads(CONFIG.read_text())
tz_name = cfg.get("platform", {}).get("timezone") or "UTC"
shift = cfg.get("modules", {}).get("timesheet", {}).get("work_hours", {})
shift_start = shift.get("start")
if not shift_start:
    print("auto-sync: no shift start configured — skipping.")
    sys.exit(0)

now_utc = datetime.now(tz_mod.utc)
user_tz = ZoneInfo(tz_name)
now_local = now_utc.astimezone(user_tz)
sh, sm = map(int, shift_start.split(":"))
today_shift_start_local = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
today_shift_start_utc = today_shift_start_local.astimezone(tz_mod.utc)

# Most-recent shift_start that has actually fired (handles cross-midnight).
if today_shift_start_utc <= now_utc:
    most_recent_shift_start_utc = today_shift_start_utc
else:
    most_recent_shift_start_utc = today_shift_start_utc - timedelta(days=1)

last_synced = None
if LAST_SYNCED.exists():
    try:
        last_synced = datetime.fromisoformat(LAST_SYNCED.read_text().strip().replace("Z", "+00:00"))
    except ValueError:
        pass

should_sync = last_synced is None or last_synced < most_recent_shift_start_utc
if not should_sync:
    sys.exit(0)

# Pull the worktrace-cli repo first — this is what brings down updated rules
# (this file!) + updated scripts. If the directory isn't a git checkout
# (first-run setup, manual install) just skip the git pull and continue.
if (DP / ".git").exists():
    subprocess.run(["git", "-C", str(DP), "pull", "--rebase", "--autostash", "--quiet"],
                   capture_output=True, text=True)

# Re-inject CLAUDE.shared.md into ~/.claude/CLAUDE.md (between markers).
# Pulls latest rules into the per-machine user-level file. Personal sections
# above/below the markers are not touched.
sync_md = subprocess.run(["python3", str(DP / "scripts" / "sync_claude_md.py")],
                         capture_output=True, text=True)

# Pull dashboard → local config.json (Phase 5k Down direction).
pull = subprocess.run(["python3", str(DP / "scripts" / "sync_pull.py")],
                     capture_output=True, text=True)

# Push local timesheet → user's data repo (Phase 5k Up direction).
# dpsync no-ops cleanly for admin accounts (no remote_data_repo).
push = subprocess.run(["python3", str(DP / "dpsync.py")],
                     capture_output=True, text=True)

failed = [s for s in [(sync_md, "rules"), (pull, "pull"), (push, "push")] if s[0].returncode != 0]
if failed:
    print("auto-sync: partial failure — " + ", ".join(name for _, name in failed))
    for proc, name in failed:
        if proc.stderr.strip(): print(f"  {name} stderr:", proc.stderr.strip()[:300])
    # Don't update .last_synced_at — next session retries.
    sys.exit(0)

LAST_SYNCED.write_text(now_utc.isoformat() + "\n")
shift_label = now_local.strftime("%H:%M")
print(f"✓ Auto-synced WorkTrace at {shift_label} {tz_name} (rules + dashboard + timesheet).")
PY
```

**Rules for the rest of the session:**
- Run this check exactly once per session — the `.last_synced_at` file gates re-runs.
- If the check prints the `✓ Auto-synced…` line, echo it to the user before doing anything else. Don't suppress.
- If the check prints an error, mention it briefly but don't let it block the user's actual request.
- If nothing printed, proceed silently. No "checked, nothing to do" noise.
- Don't run the sync if the user explicitly asks for something destructive (force-push, file deletion, etc.) — defer to next session.

## Project trigger phrases (WorkTrace-platform-related only)

When the user says one of these phrases (alone or as the opener of a message), load `~/Documents/DevPlatform/PROJECT_NOTES.md` and pick up from its "Open items" section.

| Phrase | File to load |
| --- | --- |
| **WorkTrace Project** | `~/Documents/DevPlatform/PROJECT_NOTES.md` |
| **DevPlatform Project** | `~/Documents/DevPlatform/PROJECT_NOTES.md` |
| **Timesheet Project** | `~/Documents/DevPlatform/PROJECT_NOTES.md` |

Non-platform project trigger phrases (e.g. for a specific client engagement) live in each teammate's user-level CLAUDE.md.
