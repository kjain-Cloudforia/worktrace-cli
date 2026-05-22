#!/usr/bin/env python3
"""
sync_claude_md.py — Phase 5l: inject canonical WorkTrace platform config
into each teammate's per-machine `~/.claude/` directory.

Manages two things:

  1. CLAUDE.md content
     - Source: `~/Documents/DevPlatform/CLAUDE.shared.md` (git-tracked
       in worktrace-cli).
     - Target: `~/.claude/CLAUDE.md`.
     - Method: replaces everything between
         <!-- BEGIN WORKTRACE RULES ... -->
         <!-- END WORKTRACE RULES -->
       Anything outside the markers (personal rules, stack-specific
       queries, etc.) is left completely untouched.

  2. ~/.claude/settings.json hooks
     - Ensures the three WorkTrace hooks are registered for every
       teammate (Phase 5l moved the auto-sync gate from LLM-procedure
       to deterministic hooks):
         * UserPromptSubmit → wt_shift_gate.py
         * PostToolUse(Bash) → claude_bash_hook.py
         * PostToolUse(Write|Edit) → wt_timesheet_lint.py
     - Identifies "managed" hooks by the path substring
       `Documents/DevPlatform/scripts/` in the command — anything else
       (user-added hooks, other tools' hooks) is preserved.
     - Preserves all other settings (effortLevel, etc.).

Both operations are idempotent — safe to run repeatedly. The auto-sync
routine invokes this on every shift-start, after `git pull` brings down
any rule/script updates from the worktrace-cli repo.

Exit codes:
    0   success (no-op if already in sync)
    1   CLAUDE.shared.md missing (source of truth not on disk — weird
        state, probably the repo isn't cloned right)
    2   couldn't read/write `~/.claude/CLAUDE.md` or settings.json
"""

import json
import sys
from pathlib import Path


HOME = Path.home()
SHARED = HOME / "Documents" / "DevPlatform" / "CLAUDE.shared.md"
USER_LEVEL = HOME / ".claude" / "CLAUDE.md"
SETTINGS = HOME / ".claude" / "settings.json"

# Markers — keep these exactly stable across versions of the script.
# Changing them would orphan existing managed blocks. Treated as literal
# strings (no regex escaping needed since they have no special chars).
BEGIN = "<!-- BEGIN WORKTRACE RULES (managed by sync_claude_md.py — do not edit) -->"
END   = "<!-- END WORKTRACE RULES -->"

# Substring used to identify a hook entry as "managed by WorkTrace".
# Any hook command containing this gets replaced on each sync run; other
# hooks are preserved untouched. Keep this stable.
WORKTRACE_HOOK_MARKER = "Documents/DevPlatform/scripts/"

# Canonical hook set — keep in sync with the scripts shipped in this repo.
# Each entry is (event_name, matcher_or_None, command).
CANONICAL_HOOKS = [
    ("UserPromptSubmit", None, "python3 $HOME/Documents/DevPlatform/scripts/wt_shift_gate.py"),
    ("PostToolUse", "Bash", "python3 $HOME/Documents/DevPlatform/scripts/claude_bash_hook.py"),
    ("PostToolUse", "Write|Edit", "python3 $HOME/Documents/DevPlatform/scripts/wt_timesheet_lint.py"),
]


def main() -> int:
    md_status = sync_claude_md()
    hooks_status = ensure_worktrace_hooks()
    # Highest of the two is the overall exit (we want to surface any failure).
    return max(md_status, hooks_status)


def sync_claude_md() -> int:
    if not SHARED.exists():
        print(f"sync_claude_md: source missing at {SHARED} — is worktrace-cli cloned?", file=sys.stderr)
        return 1

    shared_content = SHARED.read_text()

    # Build the managed block. We strip a leading "# WorkTrace platform rules"
    # heading if present — it's useful in the standalone file but redundant
    # when injected into a doc that already has its own H1.
    if shared_content.startswith("# "):
        # Drop the first heading line + its trailing blank line.
        lines = shared_content.split("\n", 2)
        shared_content = lines[2] if len(lines) > 2 else shared_content
        shared_content = shared_content.lstrip("\n")

    managed_block = f"{BEGIN}\n\n{shared_content.rstrip()}\n\n{END}\n"

    # Ensure ~/.claude/ exists (very fresh laptop case).
    USER_LEVEL.parent.mkdir(parents=True, exist_ok=True)

    try:
        existing = USER_LEVEL.read_text() if USER_LEVEL.exists() else ""
    except OSError as e:
        print(f"sync_claude_md: cannot read {USER_LEVEL}: {e}", file=sys.stderr)
        return 2

    new_text = inject_or_append(existing, managed_block)

    if new_text == existing:
        # Already in sync. Silent no-op so the auto-sync routine doesn't
        # spam "✓ rules unchanged" on every run.
        return 0

    try:
        USER_LEVEL.write_text(new_text)
    except OSError as e:
        print(f"sync_claude_md: cannot write {USER_LEVEL}: {e}", file=sys.stderr)
        return 2

    print(f"  ↳ updated managed block in {USER_LEVEL.relative_to(HOME)}")
    return 0


def inject_or_append(existing: str, managed_block: str) -> str:
    """
    Replace the BEGIN..END block in `existing` with `managed_block`, or
    append it if no markers are present.

    The replace path is greedy on the END marker — it matches everything
    up to and including the FIRST end-marker after the begin-marker, so
    a user who accidentally pastes a similar-looking line inside their
    personal section above the managed block doesn't break the injection.
    """
    begin_at = existing.find(BEGIN)
    if begin_at == -1:
        # No managed block yet. Append with a clear separator so the
        # user's existing content stays intact above ours.
        separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        return existing + separator + managed_block

    end_at = existing.find(END, begin_at + len(BEGIN))
    if end_at == -1:
        # Begin marker present but no end — file got corrupted. Re-append
        # a fresh managed block and rely on the user to clean up.
        # Don't try to be clever about half-broken state.
        print("sync_claude_md: WARNING — found BEGIN marker but no END; appending a fresh block.",
              file=sys.stderr)
        return existing + "\n\n" + managed_block

    end_at_full = end_at + len(END)
    # Eat any trailing newline that was part of the previous managed
    # block's tail, so we don't leave a growing run of blank lines on
    # every re-injection.
    while end_at_full < len(existing) and existing[end_at_full] == "\n":
        end_at_full += 1

    before = existing[:begin_at].rstrip() + "\n\n"
    after  = existing[end_at_full:]
    if after and not after.startswith("\n"):
        after = "\n" + after

    return before + managed_block + after


def ensure_worktrace_hooks() -> int:
    """Register the WorkTrace hooks in `~/.claude/settings.json` idempotently.

    On every run:
      - Reads the user's existing settings.json (preserving every field).
      - Strips any hook entries pointing at WORKTRACE_HOOK_MARKER (these
        are ours; we own their lifecycle).
      - Adds back the canonical set defined in CANONICAL_HOOKS.
      - Preserves all non-WorkTrace hooks (other tools, user-added).
      - Writes back only if something changed.

    Safe to run before settings.json exists — creates a minimal file.
    """
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)

    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"sync_claude_md: cannot parse {SETTINGS} ({e}) — skipping hook sync. "
                f"Fix or delete the file, then re-run.",
                file=sys.stderr,
            )
            return 2
        if not isinstance(settings, dict):
            print(
                f"sync_claude_md: {SETTINGS} is not a JSON object — skipping hook sync.",
                file=sys.stderr,
            )
            return 2
    else:
        settings = {}

    original_serialized = json.dumps(settings, sort_keys=True)

    hooks_root = settings.setdefault("hooks", {})
    if not isinstance(hooks_root, dict):
        print(
            f"sync_claude_md: 'hooks' in {SETTINGS} is not an object — skipping hook sync.",
            file=sys.stderr,
        )
        return 2

    # Strip every existing WorkTrace-managed hook entry while preserving
    # other entries the user (or other tools) may have added.
    for event_name in list(hooks_root.keys()):
        existing_groups = hooks_root.get(event_name)
        if not isinstance(existing_groups, list):
            continue
        new_groups = []
        for group in existing_groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            inner_hooks = group.get("hooks", [])
            if not isinstance(inner_hooks, list):
                new_groups.append(group)
                continue
            non_managed = [
                h for h in inner_hooks
                if not (isinstance(h, dict) and WORKTRACE_HOOK_MARKER in str(h.get("command", "")))
            ]
            if non_managed:
                # Keep the group but with only the non-managed hooks.
                rewritten = dict(group)
                rewritten["hooks"] = non_managed
                new_groups.append(rewritten)
            # else: the whole group was WorkTrace-only; drop it.
        if new_groups:
            hooks_root[event_name] = new_groups
        else:
            del hooks_root[event_name]

    # Add the canonical WorkTrace hooks back, one group per (event, matcher).
    for event_name, matcher, command in CANONICAL_HOOKS:
        groups_for_event = hooks_root.setdefault(event_name, [])
        group_entry = {"hooks": [{"type": "command", "command": command}]}
        if matcher is not None:
            group_entry["matcher"] = matcher
        groups_for_event.append(group_entry)

    final_serialized = json.dumps(settings, sort_keys=True)
    if final_serialized == original_serialized:
        return 0

    try:
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    except OSError as e:
        print(f"sync_claude_md: cannot write {SETTINGS}: {e}", file=sys.stderr)
        return 2

    print(f"  ↳ registered WorkTrace hooks in {SETTINGS.relative_to(HOME)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
