#!/usr/bin/env python3
"""UserPromptSubmit hook — fires when the user submits a prompt.

Detects "first prompt in a new work shift" (using config.json's per-user
shift hours + timezone — NOT session boundaries — so multi-day sessions still
trigger correctly), and on shift boundary:

  1. Pulls the worktrace-cli platform repo (latest rules + scripts)
  2. Re-injects CLAUDE.shared.md into ~/.claude/CLAUDE.md
  3. Pulls the user's data repo (dashboard → local)
  4. Pushes the user's local timesheet → data repo
  5. Compiles structured evidence for any completed-but-not-yet-reconstructed
     shifts via wt_compile_shift.py
  6. Surfaces a notice to Claude about which shifts need bullets written

The gate is keyed on .last_synced_at, so any subsequent prompt in the same
shift no-ops cleanly. This means it's safe to fire on every prompt — the cost
of a no-op is one file stat.

Output is written to stdout. Claude Code's UserPromptSubmit contract injects
that into the model's context before processing the user's actual prompt, so
the model becomes aware of any reconstruction work it needs to do.

Failures are non-blocking — the hook never exits with code 2 (which would
block the prompt). Partial failures (e.g. push fails) skip updating the marker
so the next prompt retries.
"""
import json
import sys
import re
import subprocess
from datetime import datetime, timedelta, timezone as tz_mod
from pathlib import Path
from zoneinfo import ZoneInfo

DP = Path.home() / "Documents" / "DevPlatform"
CONFIG = DP / "config.json"
LAST_SYNCED = DP / ".last_synced_at"
GATE = DP / ".last_reconstructed_shift"
TIMESHEET = DP / "modules" / "timesheet" / "timesheet.md"


def main() -> None:
    # Discard the hook payload (we don't need its contents)
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    if not CONFIG.exists():
        return

    try:
        configDict = json.loads(CONFIG.read_text())
    except Exception:
        return

    shiftConfig = configDict.get("modules", {}).get("timesheet", {}).get("work_hours", {})
    shiftStartHm = shiftConfig.get("start")
    shiftEndHm = shiftConfig.get("end")
    timezoneName = configDict.get("platform", {}).get("timezone") or "UTC"
    if not (shiftStartHm and shiftEndHm):
        return

    userTimezone = ZoneInfo(timezoneName)
    nowUtc = datetime.now(tz_mod.utc)
    nowLocal = nowUtc.astimezone(userTimezone)

    startHour, startMinute = map(int, shiftStartHm.split(":"))
    endHour, endMinute = map(int, shiftEndHm.split(":"))
    crossesMidnight = (endHour, endMinute) < (startHour, startMinute)

    todayShiftStartLocal = nowLocal.replace(
        hour=startHour, minute=startMinute, second=0, microsecond=0
    )
    todayShiftStartUtc = todayShiftStartLocal.astimezone(tz_mod.utc)

    if todayShiftStartUtc <= nowUtc:
        mostRecentShiftStartUtc = todayShiftStartUtc
    else:
        mostRecentShiftStartUtc = todayShiftStartUtc - timedelta(days=1)

    lastSynced = None
    if LAST_SYNCED.exists():
        try:
            lastSynced = datetime.fromisoformat(
                LAST_SYNCED.read_text().strip().replace("Z", "+00:00")
            )
        except Exception:
            pass

    if lastSynced is not None and lastSynced >= mostRecentShiftStartUtc:
        return  # already synced this shift — silent no-op

    # Sync routines
    if (DP / ".git").exists():
        subprocess.run(
            ["git", "-C", str(DP), "pull", "--rebase", "--autostash", "--quiet"],
            capture_output=True, text=True, timeout=30,
        )

    syncMdResult = subprocess.run(
        ["python3", str(DP / "scripts" / "sync_claude_md.py")],
        capture_output=True, text=True, timeout=30,
    )
    pullResult = subprocess.run(
        ["python3", str(DP / "scripts" / "sync_pull.py")],
        capture_output=True, text=True, timeout=60,
    )
    pushResult = subprocess.run(
        ["python3", str(DP / "dpsync.py")],
        capture_output=True, text=True, timeout=120,
    )

    failedSubprocesses = []
    for proc, name in [(syncMdResult, "rules"), (pullResult, "pull"), (pushResult, "push")]:
        if proc.returncode != 0:
            failedSubprocesses.append(name)

    if failedSubprocesses:
        print(
            f"⚠️ WorkTrace shift-start sync: partial failure — {', '.join(failedSubprocesses)}"
        )
        return

    LAST_SYNCED.write_text(nowUtc.isoformat() + "\n")

    # Check for completed shifts that haven't been reconstructed yet
    missingShiftDates = []
    if TIMESHEET.exists():
        try:
            timesheetContent = TIMESHEET.read_text()
        except Exception:
            timesheetContent = ""

        lastReconstructedDate = None
        if GATE.exists():
            try:
                lastReconstructedDate = datetime.fromisoformat(
                    GATE.read_text().strip()
                ).date()
            except Exception:
                pass

        def shiftEndOf(dateObject):
            startLocal = datetime(
                dateObject.year, dateObject.month, dateObject.day,
                startHour, startMinute, tzinfo=userTimezone,
            )
            return startLocal.replace(hour=endHour, minute=endMinute) + (
                timedelta(days=1) if crossesMidnight else timedelta(0)
            )

        for daysBack in range(7):
            candidateDate = nowLocal.date() - timedelta(days=daysBack)
            if lastReconstructedDate and candidateDate <= lastReconstructedDate:
                break
            if nowLocal < shiftEndOf(candidateDate):
                continue  # shift not yet completed
            dateLabel = candidateDate.isoformat()
            if re.search(
                rf"^### Day \d+ — \w+ {re.escape(dateLabel)}\b",
                timesheetContent,
                re.MULTILINE,
            ):
                continue  # already in timesheet
            missingShiftDates.append(dateLabel)

    # Auto-compile evidence for each missing shift (deterministic)
    compiledShiftPaths = []
    if missingShiftDates:
        compilerPath = DP / "scripts" / "wt_compile_shift.py"
        if compilerPath.exists():
            for shiftDateLabel in sorted(missingShiftDates):
                compileResult = subprocess.run(
                    ["python3", str(compilerPath), shiftDateLabel],
                    capture_output=True, text=True, timeout=30,
                )
                if compileResult.returncode == 0:
                    evidencePath = DP / ".shift_evidence" / f"{shiftDateLabel}.md"
                    if evidencePath.exists():
                        compiledShiftPaths.append((shiftDateLabel, str(evidencePath)))

    # Surface results to Claude via stdout (injected into context)
    shiftLabel = nowLocal.strftime("%H:%M")
    statusLines = [f"✓ WorkTrace shift-start sync at {shiftLabel} {timezoneName}"]

    if compiledShiftPaths:
        statusLines.append("")
        statusLines.append(
            "📋 Shifts needing timesheet bullets (evidence pre-compiled — read the file, "
            "write bullets, append to timesheet, then sync):"
        )
        for shiftDateLabel, evidencePath in compiledShiftPaths:
            statusLines.append(f"  • {shiftDateLabel} → {evidencePath}")
        statusLines.append("")
        statusLines.append(
            f"After appending, update the marker: "
            f"echo '{compiledShiftPaths[-1][0]}' > {GATE}"
        )
        statusLines.append("Then run: python3 ~/Documents/DevPlatform/dpsync.py")
    elif missingShiftDates:
        statusLines.append(
            f"⚠️ Missing shifts detected ({', '.join(sorted(missingShiftDates))}) "
            f"but compiler is not available."
        )

    print("\n".join(statusLines))


if __name__ == "__main__":
    main()
