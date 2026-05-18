#!/usr/bin/env python3
"""
sync_pull.py — Phase 5k: pull dashboard-side state down to local config.json.

The companion to dpsync.py:
  - dpsync.py    pushes local timesheet.md → user's data repo (Up direction)
  - sync_pull.py pulls the user's auth record → local config.json (Down direction)

What gets synced:
  • modules.timesheet.work_hours.start / .end   ←  effective work_shift from auth record
  • platform.timezone                            ←  effective work_shift.timezone
  • (everything else stays local — github_token, project_detection, etc.)

The "effective" shift handles Phase 5k's deferred-change logic:
  - If the auth record has a work_shift_pending whose effective_from has
    elapsed, that pending shift IS the active shift. We sync that.
  - Otherwise we sync work_shift directly.
  - If a pending change is still in the future, we ignore it — the local
    config keeps the current shift until the change actually takes effect.

Conflict policy (per design discussion): the auth record is canonical.
If local config.json's work_hours differ from the effective shift, the
local values get overwritten on next sync. Edit shift via the dashboard,
not by hand.

This script is normally invoked by the Claude session-start auto-sync
hook (see ~/.claude/CLAUDE.md). Safe to run manually anytime:

    python3 ~/Documents/DevPlatform/scripts/sync_pull.py

Exit codes:
    0   sync succeeded (no-op if nothing changed)
    1   network / GitHub error
    2   config.json missing or malformed
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone as tz_mod
from pathlib import Path


CONFIG_PATH = Path.home() / "Documents" / "DevPlatform" / "config.json"
AUTH_API_BASE = "https://api.github.com/repos/kjain-Cloudforia/worktrace-auth/contents"


def fail(msg, code=1):
    print(f"sync_pull: {msg}", file=sys.stderr)
    sys.exit(code)


def fetch_auth_record(username):
    """Fetch the user's auth record from worktrace-auth via the Contents API."""
    buster = f"?_={int(time.time())}"
    url = f"{AUTH_API_BASE}/users/{username}.json{buster}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.v3.raw",
        "Cache-Control": "no-cache",
        "User-Agent": "worktrace-sync_pull/1",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            fail(f"no auth record for username '{username}' — has admin provisioned you?")
        fail(f"HTTP {e.code} fetching auth record: {e}")
    except urllib.error.URLError as e:
        fail(f"network error fetching auth record: {e}")


def resolve_active_shift(record, now=None):
    """
    Python mirror of auth.js resolveActiveShift. Returns the effective
    work_shift (handles pending → current promotion), or None if neither
    is set on the record.
    """
    if now is None:
        now = datetime.now(tz_mod.utc)
    pending = record.get("work_shift_pending")
    if pending and pending.get("effective_from"):
        try:
            # Python's fromisoformat doesn't accept 'Z' suffix before 3.11.
            iso = pending["effective_from"].replace("Z", "+00:00")
            effective_at = datetime.fromisoformat(iso)
            if effective_at <= now:
                return {
                    "start":    pending["start"],
                    "end":      pending["end"],
                    "timezone": pending["timezone"],
                }
        except (ValueError, KeyError):
            pass  # malformed pending — ignore, fall through to work_shift
    return record.get("work_shift")


def main():
    if not CONFIG_PATH.exists():
        fail(f"no config.json at {CONFIG_PATH}", code=2)

    try:
        config = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        fail(f"config.json is malformed: {e}", code=2)

    username = config.get("platform", {}).get("user_id")
    if not username:
        fail("platform.user_id missing from config.json", code=2)

    record = fetch_auth_record(username)
    shift = resolve_active_shift(record)
    if not shift:
        # Record has no work_shift at all (pre-Phase-5j? extremely unlikely
        # post-migration). Nothing to sync down.
        print("  ↳ no work_shift on auth record — nothing to pull.")
        return 0

    # Compare with local values. Both sides may have:
    #   config.modules.timesheet.work_hours = {start, end}
    #   config.platform.timezone            = '<IANA name>'
    config.setdefault("modules", {}).setdefault("timesheet", {}).setdefault("work_hours", {})
    config.setdefault("platform", {})

    local_hours = config["modules"]["timesheet"]["work_hours"]
    local_tz    = config["platform"].get("timezone")

    changes = []
    if local_hours.get("start") != shift["start"]:
        changes.append(f"work_hours.start: {local_hours.get('start')!r} → {shift['start']!r}")
        local_hours["start"] = shift["start"]
    if local_hours.get("end") != shift["end"]:
        changes.append(f"work_hours.end: {local_hours.get('end')!r} → {shift['end']!r}")
        local_hours["end"] = shift["end"]
    if local_tz != shift["timezone"]:
        changes.append(f"platform.timezone: {local_tz!r} → {shift['timezone']!r}")
        config["platform"]["timezone"] = shift["timezone"]

    if not changes:
        print("  ↳ config.json already matches dashboard. No changes.")
        return 0

    # Write back. Preserve 0600 permissions on the config (it holds PATs).
    text = json.dumps(config, indent=2) + "\n"
    CONFIG_PATH.write_text(text)
    CONFIG_PATH.chmod(0o600)
    print(f"  ↳ Pulled {len(changes)} change{'' if len(changes) == 1 else 's'} from dashboard:")
    for c in changes:
        print(f"      • {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
