#!/usr/bin/env bash
#
# install.sh — one-command WorkTrace teammate onboarding.
#
# Usage (the only thing the new teammate has to type):
#
#   curl -fsSL https://raw.githubusercontent.com/kjain-Cloudforia/worktrace-cli/main/install.sh | bash
#
# What it does (in order):
#   1. Confirms python3 + git + curl are present.
#   2. Prompts for the teammate's username + PAT (the two things admin
#      sends them out-of-band). Nothing else needs typing — everything
#      else (display name, timezone, shift, data-repo URL) comes from
#      the auth record admin already provisioned in worktrace-auth.
#   3. Validates the PAT works against the target data repo.
#   4. Clones worktrace-cli into ~/Documents/DevPlatform/.
#   5. Generates config.json (chmod 600) with all the pulled-down values.
#   6. Clones the user's private data repo with token-in-URL credentials.
#   7. Sources shell-hook.zsh from ~/.zshrc.
#   8. Runs sync_claude_md.py to inject the WorkTrace rules into
#      ~/.claude/CLAUDE.md (between markers; personal sections untouched).
#   9. Prints the final step (sign in to dashboard + change password).
#
# Idempotent — safe to re-run. Existing config is reused unless --force.
#

set -euo pipefail

# --- Constants -------------------------------------------------------------

DP_DIR="${HOME}/Documents/DevPlatform"
WORKTRACE_CLI_REPO="https://github.com/kjain-Cloudforia/worktrace-cli.git"
AUTH_API="https://api.github.com/repos/kjain-Cloudforia/worktrace-auth/contents/users"
DASHBOARD_URL="https://kjain-Cloudforia.github.io/worktrace-app/"

# --- Output helpers --------------------------------------------------------

# Use ANSI escapes directly (no tput) because we may be in a pipe-from-curl
# context where TERM isn't always set up.
B='\033[1m'; G='\033[32m'; Y='\033[33m'; R='\033[31m'; D='\033[2m'; N='\033[0m'

say()   { printf "${G}»${N} %b\n" "$*"; }
note()  { printf "${D}  %b${N}\n" "$*"; }
warn()  { printf "${Y}!${N} %b\n" "$*"; }
die()   { printf "${R}✗ %b${N}\n" "$*" >&2; exit 1; }
ok()    { printf "${G}✓${N} %b\n" "$*"; }

# --- 1. Prerequisites -----------------------------------------------------

printf "\n${B}WorkTrace teammate setup${N}\n"
printf "${D}This will install scripts + rules under ${DP_DIR} and configure your laptop.${N}\n\n"

for cmd in python3 git curl; do
  command -v "$cmd" >/dev/null 2>&1 || die "Required command '$cmd' not found. Install it and re-run."
done

# zsh isn't strictly required — fall back to bash for the shell hook line.
SHELL_RC="${HOME}/.zshrc"
if [ ! -f "$SHELL_RC" ]; then
  SHELL_RC="${HOME}/.bashrc"
  [ -f "$SHELL_RC" ] || SHELL_RC="${HOME}/.bash_profile"
  warn "~/.zshrc not found — will source the shell hook from $(basename "$SHELL_RC") instead."
fi

# --- 2. Interactive prompts ----------------------------------------------

printf "\n${B}Step 1 of 2:${N} Your WorkTrace username\n"
printf "${D}  (admin assigned this to you — a short lowercase slug like 'kashish' or 'alice')${N}\n"
read -p "  Username: " USERNAME
USERNAME="$(echo "$USERNAME" | tr '[:upper:]' '[:lower:]' | xargs)"
[[ "$USERNAME" =~ ^[a-z][a-z0-9-]*$ ]] || die "Username must be lowercase letters + digits + hyphens, starting with a letter. Got: '$USERNAME'"

# Fetch the auth record's public fields. This is a PUBLIC repo, so no
# authentication is needed for the lookup — but it's only there if
# admin has already provisioned the user via the dashboard.
printf "\n${D}Looking up your account in worktrace-auth...${N}\n"
AUTH_URL="${AUTH_API}/${USERNAME}.json"
AUTH_JSON=$(curl -fsSL -H 'Accept: application/vnd.github.v3.raw' "$AUTH_URL" 2>/dev/null) || {
  die "No WorkTrace account found for username '${USERNAME}'.
   Make sure admin has provisioned you via the dashboard's Admin Console first."
}

# Parse the public fields with Python (universally available, jq isn't).
PUBLIC_DATA=$(python3 -c "
import json, sys
rec = json.loads('''$AUTH_JSON''')
ws = rec.get('work_shift') or {}
print(rec.get('display_name', ''))
print(rec.get('data_repo') or '')
print(ws.get('start') or '')
print(ws.get('end') or '')
print(ws.get('timezone') or 'UTC')
print('admin' if rec.get('is_admin') else 'user')
" 2>&1) || die "Couldn't parse the auth record:\n$PUBLIC_DATA"

DISPLAY_NAME=$(sed -n '1p' <<< "$PUBLIC_DATA")
DATA_REPO=$(sed -n '2p' <<< "$PUBLIC_DATA")
SHIFT_START=$(sed -n '3p' <<< "$PUBLIC_DATA")
SHIFT_END=$(sed -n '4p' <<< "$PUBLIC_DATA")
TIMEZONE=$(sed -n '5p' <<< "$PUBLIC_DATA")
ROLE=$(sed -n '6p' <<< "$PUBLIC_DATA")

ok "Found you:"
note "Display name:  ${DISPLAY_NAME}"
note "Role:          ${ROLE}"
note "Data repo:     ${DATA_REPO:-(none — admin account)}"
note "Work shift:    ${SHIFT_START}–${SHIFT_END} ${TIMEZONE}"

printf "\n${B}Step 2 of 2:${N} Your GitHub PAT (the long string admin sent you)\n"
printf "${D}  Starts with 'github_pat_'. Paste it; it won't be echoed.${N}\n"
read -s -p "  PAT: " PAT
echo
PAT="$(echo "$PAT" | xargs)"
[[ "$PAT" =~ ^github_pat_ ]] || warn "PAT doesn't start with 'github_pat_' — that's the fine-grained format. Continuing anyway."

# --- 3. Validate PAT against the data repo (or worktrace-auth for admins) -

# Admin accounts have data_repo=null. They still need PAT-against-something
# verification — use worktrace-auth as the target since admin always has
# write access to it.
PROBE_REPO="${DATA_REPO:-kjain-Cloudforia/worktrace-auth}"
say "Verifying PAT against ${PROBE_REPO}..."
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $PAT" \
    "https://api.github.com/repos/${PROBE_REPO}")

case "$HTTP_STATUS" in
  200) ok "PAT works.";;
  401|403) die "PAT cannot access ${PROBE_REPO} (HTTP $HTTP_STATUS).
   Wrong token, or it's missing 'Contents: Read+Write' on that repo.
   Ask admin to re-issue the PAT.";;
  404) die "Repo ${PROBE_REPO} not found by your PAT (HTTP 404).
   Either admin set the wrong repo, or your PAT doesn't have access.";;
  *)   die "Unexpected HTTP $HTTP_STATUS probing ${PROBE_REPO}.";;
esac

# --- 4. Clone worktrace-cli ----------------------------------------------

if [ -d "${DP_DIR}/.git" ]; then
  say "${DP_DIR} already a git repo — pulling latest..."
  git -C "$DP_DIR" pull --quiet --rebase --autostash
elif [ -d "$DP_DIR" ] && [ -n "$(ls -A "$DP_DIR" 2>/dev/null)" ]; then
  die "${DP_DIR} exists and is non-empty but not a git repo.
   Move or remove that directory and re-run."
else
  say "Cloning worktrace-cli into ${DP_DIR}..."
  git clone --quiet "$WORKTRACE_CLI_REPO" "$DP_DIR"
fi

# --- 5. Write config.json -------------------------------------------------

CONFIG_PATH="${DP_DIR}/config.json"
if [ -f "$CONFIG_PATH" ] && [ "${1:-}" != "--force" ]; then
  warn "${CONFIG_PATH} already exists — leaving it alone."
  note "Re-run with --force if you want to overwrite it."
else
  say "Writing ${CONFIG_PATH}..."
  # Build the repo URL with token embedded so dpsync's git push works
  # without prompting. The token ends up in sync/.git/config too — both
  # files are chmod 600 below.
  if [ -n "$DATA_REPO" ]; then
    REMOTE_URL="https://${PAT}@github.com/${DATA_REPO}.git"
  else
    REMOTE_URL=""
  fi
  python3 - "$USERNAME" "$DISPLAY_NAME" "$TIMEZONE" "$PAT" "$REMOTE_URL" "$SHIFT_START" "$SHIFT_END" <<'PY' > "$CONFIG_PATH"
import json, sys
u, d, tz, pat, repo, st, en = sys.argv[1:8]
cfg = {
  "platform": {
    "user_id":          u,
    "display_name":     d,
    "timezone":         tz,
    "github_token":     pat,
    "remote_data_repo": repo,
    "auto_sync":        True,
  },
  "modules": {
    "timesheet": {
      "enabled":    True,
      "work_hours": {"start": st, "end": en},
    }
  }
}
print(json.dumps(cfg, indent=2))
PY
  chmod 600 "$CONFIG_PATH"
  ok "config.json written (chmod 600)."
fi

# --- 6. Clone the user's data repo ---------------------------------------

# Skipped for admin accounts (no data_repo).
if [ -n "$DATA_REPO" ]; then
  SYNC_DIR="${DP_DIR}/sync"
  if [ -d "${SYNC_DIR}/.git" ]; then
    say "${SYNC_DIR} already cloned — pulling latest..."
    git -C "$SYNC_DIR" pull --quiet --rebase --autostash 2>/dev/null || \
      warn "Couldn't pull updates to data repo — empty repo or PAT issue. Continuing."
  else
    say "Cloning your private data repo (${DATA_REPO}) into ${SYNC_DIR}..."
    # Token-in-URL so future git push from dpsync works without auth prompts.
    if ! git clone --quiet "https://${PAT}@github.com/${DATA_REPO}.git" "$SYNC_DIR" 2>/dev/null; then
      warn "Initial clone failed (likely an empty repo). Initialising locally instead."
      mkdir -p "$SYNC_DIR"
      git -C "$SYNC_DIR" init --quiet --initial-branch=main
      git -C "$SYNC_DIR" remote add origin "https://${PAT}@github.com/${DATA_REPO}.git"
    fi
    # Lock the credential-bearing config down.
    [ -f "${SYNC_DIR}/.git/config" ] && chmod 600 "${SYNC_DIR}/.git/config"
  fi
else
  note "Admin account (no data_repo) — skipping data-repo clone."
fi

# --- 7. Wire shell hook into shell rc -----------------------------------

HOOK_LINE='source ~/Documents/DevPlatform/shell-hook.zsh'
if grep -Fq "shell-hook.zsh" "$SHELL_RC" 2>/dev/null; then
  note "Shell hook already wired in $(basename "$SHELL_RC")."
else
  say "Adding shell hook to $(basename "$SHELL_RC")..."
  printf "\n# WorkTrace passive CLI logger\n%s\n" "$HOOK_LINE" >> "$SHELL_RC"
fi

# --- 8. Inject WorkTrace rules into ~/.claude/CLAUDE.md ----------------

say "Injecting WorkTrace rules into ~/.claude/CLAUDE.md..."
python3 "${DP_DIR}/scripts/sync_claude_md.py"

# --- 9. Final summary ----------------------------------------------------

printf "\n${G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}\n"
printf "${G}✓ Setup complete — welcome to WorkTrace, ${DISPLAY_NAME}!${N}\n"
printf "${G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}\n\n"

printf "${B}Next:${N}\n"
echo "  1. Open ${DASHBOARD_URL}"
echo "  2. Sign in: username '${USERNAME}' / the initial password admin gave you"
echo "  3. Use the 'Change password' button in the header to rotate to your own"
echo "  4. Open a new terminal so the shell hook activates (or run: source ${SHELL_RC})"
echo
printf "${B}Daily flow:${N}\n"
echo "  • Tell Claude 'log today's work' when you finish something deliverable"
echo "  • Tell Claude 'give me my status report for today/this week' to review + push"
echo "  • Auto-sync runs once on your first Claude message after each shift-start"
echo "    (${SHIFT_START} ${TIMEZONE})"
echo
echo "Questions? Ping the admin (Kashish)."
