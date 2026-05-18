# ~/Documents/DevPlatform/shell-hook.zsh
# Passive `sf` CLI tracker — uses zsh preexec/precmd. Does NOT redefine `sf`.
# Sourced from ~/.zshrc. Any error here must not affect interactive shells.
#
# preexec: fires *before* a command runs. We grab the command text + start time.
# precmd:  fires *after* the command finishes, before the next prompt. We grab $?.
#
# Output: one JSON event per tracked invocation appended to cli-log.jsonl.
#
# Shared across all DevPlatform modules — any module that wants CLI evidence
# (deploys, DML, etc.) reads from the same cli-log.jsonl.

DEVPLATFORM_DIR="${DEVPLATFORM_DIR:-$HOME/Documents/DevPlatform}"
DEVPLATFORM_CONFIG="$DEVPLATFORM_DIR/config.json"
DEVPLATFORM_LOG="$DEVPLATFORM_DIR/cli-log.jsonl"

# Bail out quietly if config or jq is missing — we never want to break the prompt.
if [[ ! -f "$DEVPLATFORM_CONFIG" ]] || ! command -v jq >/dev/null 2>&1; then
  return 0 2>/dev/null || true
fi

# Resolve the project name for a given cwd.
#  - Check project_detection.overrides for an exact path match first.
#  - Fall back to the basename of cwd (strategy=basename).
# Note: org-based attribution (parsing `-o "<alias>"`) is a richer signal but
# happens at report-time in Claude, not in this hook. Keeping the hook simple.
_devplatform_project_name() {
  local cwd="$1"
  local override
  override=$(jq -r --arg p "$cwd" '.project_detection.overrides[$p] // empty' "$DEVPLATFORM_CONFIG" 2>/dev/null)
  if [[ -n "$override" ]]; then
    print -r -- "$override"
  else
    print -r -- "${cwd:t}"
  fi
}

# Glob-match the full `sf <args>` invocation against the configured patterns.
# Returns 0 if it matches a tracked pattern, 1 otherwise.
_devplatform_should_track() {
  local invocation="$1"
  local enabled
  enabled=$(jq -r '.cli_tracking.enabled // false' "$DEVPLATFORM_CONFIG" 2>/dev/null)
  [[ "$enabled" != "true" ]] && return 1

  local patterns
  patterns=$(jq -r '.cli_tracking.patterns[]?' "$DEVPLATFORM_CONFIG" 2>/dev/null)
  [[ -z "$patterns" ]] && return 1

  local p
  while IFS= read -r p; do
    # ${~p} enables glob expansion of the pattern string.
    if [[ "$invocation" == ${~p} ]]; then
      return 0
    fi
  done <<< "$patterns"
  return 1
}

# State shared between preexec and precmd. Reset on every command so a
# non-tracked command between two tracked ones doesn't leak state.
typeset -g _DEVPLATFORM_PENDING_CMD=""
typeset -g _DEVPLATFORM_PENDING_CWD=""
typeset -g _DEVPLATFORM_PENDING_START=""

_devplatform_preexec() {
  # $1 is the command line as typed (after alias expansion, before execution).
  local cmd_line="$1"

  # Only care about commands whose first word is `sf`. Cheap guard before jq.
  [[ "${cmd_line%% *}" != "sf" ]] && return 0

  # Apply the tracked-pattern filter so non-mutating reads (`sf data query`,
  # `sf org list`, etc.) stay out of the log.
  _devplatform_should_track "$cmd_line" || return 0

  _DEVPLATFORM_PENDING_CMD="$cmd_line"
  _DEVPLATFORM_PENDING_CWD="$PWD"
  _DEVPLATFORM_PENDING_START=$(date -u +%s)
}

_devplatform_precmd() {
  # Capture $? FIRST — anything else clobbers it.
  local exit_code=$?

  [[ -z "$_DEVPLATFORM_PENDING_CMD" ]] && return 0

  local end_epoch duration project ts
  end_epoch=$(date -u +%s)
  duration=$(( end_epoch - _DEVPLATFORM_PENDING_START ))
  project=$(_devplatform_project_name "$_DEVPLATFORM_PENDING_CWD")
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  jq -cn \
    --arg ts "$ts" \
    --arg cwd "$_DEVPLATFORM_PENDING_CWD" \
    --arg project "$project" \
    --arg cmd "$_DEVPLATFORM_PENDING_CMD" \
    --argjson exit_code "$exit_code" \
    --argjson duration_sec "$duration" \
    '{ts:$ts, project:$project, cwd:$cwd, cmd:$cmd, exit_code:$exit_code, duration_sec:$duration_sec}' \
    >> "$DEVPLATFORM_LOG" 2>/dev/null

  _DEVPLATFORM_PENDING_CMD=""
  _DEVPLATFORM_PENDING_CWD=""
  _DEVPLATFORM_PENDING_START=""
}

# Register with zsh's hook array system. Guarded so re-sourcing the file
# (e.g. after editing ~/.zshrc) doesn't stack duplicate handlers.
autoload -Uz add-zsh-hook 2>/dev/null
if (( $+functions[add-zsh-hook] )); then
  add-zsh-hook preexec _devplatform_preexec
  add-zsh-hook precmd  _devplatform_precmd
fi
