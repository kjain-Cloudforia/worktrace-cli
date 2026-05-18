#!/usr/bin/env python3
"""
sync_claude_md.py — Phase 5l: inject the canonical WorkTrace platform
rules into the per-machine user-level CLAUDE.md.

What this does:
  - Reads `~/Documents/DevPlatform/CLAUDE.shared.md` (the source of truth,
    git-tracked in the worktrace-cli repo).
  - Opens `~/.claude/CLAUDE.md` (the per-machine personal config that
    Claude loads on every session, regardless of workspace).
  - Replaces everything between
      <!-- BEGIN WORKTRACE RULES (managed by sync_claude_md.py — do not edit) -->
      <!-- END WORKTRACE RULES -->
    with the current contents of CLAUDE.shared.md.
  - Anything outside the markers (your personal rules, stack-specific
    deploy verification queries, communication-style notes, etc.) is
    left completely untouched.

If the markers don't exist yet (first run / fresh laptop), the script
appends them — with the shared content already in place — to the end
of the existing CLAUDE.md. If the file itself doesn't exist, it creates
a minimal one with just the markers + content.

Idempotent — safe to run repeatedly. The auto-sync routine invokes
this on every shift-start, after `git pull` brings down any rule
updates from the worktrace-cli repo.

Exit codes:
    0   success (no-op if already in sync)
    1   CLAUDE.shared.md missing (source of truth not on disk — weird
        state, probably the repo isn't cloned right)
    2   couldn't read/write the user-level CLAUDE.md (permissions issue,
        symlink chaos, etc.)
"""

import sys
from pathlib import Path


HOME = Path.home()
SHARED = HOME / "Documents" / "DevPlatform" / "CLAUDE.shared.md"
USER_LEVEL = HOME / ".claude" / "CLAUDE.md"

# Markers — keep these exactly stable across versions of the script.
# Changing them would orphan existing managed blocks. Treated as literal
# strings (no regex escaping needed since they have no special chars).
BEGIN = "<!-- BEGIN WORKTRACE RULES (managed by sync_claude_md.py — do not edit) -->"
END   = "<!-- END WORKTRACE RULES -->"


def main() -> int:
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


if __name__ == "__main__":
    sys.exit(main())
