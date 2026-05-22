#!/usr/bin/env python3
"""PostToolUse hook on Write/Edit — when the target file is the timesheet,
   surfaces evidence-file paths for each day section so the LLM can verify
   bullets against deterministic evidence before committing.

This is a soft linter — it doesn't reject edits, it surfaces what to cross-check.
The expectation is the LLM sees the evidence path in the hook output, reads the
evidence file, and double-checks that each bullet has supporting timestamps in
that day's shift window.

Why not a hard linter?
  - Bullet prose is free-form; hard parsing is brittle
  - The deterministic compiler already locks bucketing/attribution
  - A reminder + accessible evidence file is enough to catch fabrication
"""
import json, re, sys
from pathlib import Path
from datetime import datetime

DP = Path.home() / "Documents" / "DevPlatform"
TIMESHEET_PATHS = {
    str(DP / "modules" / "timesheet" / "timesheet.md"),
}
EVIDENCE_DIR = DP / ".shift_evidence"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    if payload.get("tool_name") not in ("Write", "Edit"):
        return

    toolInput = payload.get("tool_input", {})
    filePath = toolInput.get("file_path", "")
    if filePath not in TIMESHEET_PATHS:
        return

    # Read the file post-write and find day headers
    try:
        timesheetContent = Path(filePath).read_text()
    except Exception:
        return

    # Match headers like "### Day N — Weekday YYYY-MM-DD ..."
    dayHeaderPattern = re.compile(
        r"^### Day \d+ — \w+ (\d{4}-\d{2}-\d{2})\b", re.MULTILINE
    )
    matchedDates = dayHeaderPattern.findall(timesheetContent)
    if not matchedDates:
        return

    # For each unique day in the timesheet, list the evidence file (if any)
    evidenceLines = []
    sortedDates = sorted(set(matchedDates))
    # Only surface evidence for the LAST 3 day headers (most likely the focus
    # of the edit). Older days clutter the output.
    recentDates = sortedDates[-3:]
    for dateLabel in recentDates:
        evidencePath = EVIDENCE_DIR / f"{dateLabel}.md"
        if evidencePath.exists():
            evidenceLines.append(f"  • {dateLabel} → {evidencePath}")
        else:
            evidenceLines.append(
                f"  • {dateLabel} → (not compiled; "
                f"run `python3 {DP / 'scripts' / 'wt_compile_shift.py'} {dateLabel}`)"
            )

    if not evidenceLines:
        return

    print(
        "📋 Timesheet edited. Cross-check each bullet against the per-shift "
        "evidence:"
    )
    print("\n".join(evidenceLines))


if __name__ == "__main__":
    main()
