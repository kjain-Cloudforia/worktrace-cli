#!/usr/bin/env python3
"""
wt_timesheet_coverage.py — audit completed shifts for timesheet gaps.

Why: the shift gate only checked whether a `### Day N — <Weekday> YYYY-MM-DD` header
existed. A header written mid-shift by one session (with `*(in progress)*`) made
the whole day look "done", so other projects worked on that shift were never
logged (Airstreams + Petrodev on 2026-11-18).

For every completed shift in the last N days this re-compiles the evidence and
reports:
  • MISSING DAY        — evidence exists but no day header
  • STILL IN PROGRESS  — shift is over but the day still carries *(in progress)*
  • MISSING PROJECT    — a project has real session messages / CLI events /
                         commits that shift, but no `**Project: …**` sub-header
  • MISSING MEETING    — a calendar meeting title not found on that day

Usage:
  python3 wt_timesheet_coverage.py [--days 7] [--no-compile]
Exit code 0 always; prints nothing when there are no gaps.
"""
import argparse
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone as tz_mod
from pathlib import Path
from zoneinfo import ZoneInfo

DP = Path.home() / "Documents" / "DevPlatform"
CONFIG = DP / "config.json"
TIMESHEET = DP / "modules" / "timesheet" / "timesheet.md"
EVIDENCE_DIR = DP / ".shift_evidence"
COMPILER = DP / "scripts" / "wt_compile_shift.py"
COMMITS_SCRIPT = Path.home() / ".claude" / "scripts" / "project_commits_sync.py"

# Session messages that say nothing about work done.
TRIVIAL_MESSAGE_PATTERN = re.compile(
    r"^(hi|hello|hey|ok|k|resume|done|see|status\.?|check now|got this\.?)$"
    r"|^\[Request interrupted|^\[Image|^<task-notification|^Your response above was cut off",
    re.IGNORECASE,
)
MIN_SUBSTANTIVE_MESSAGES = 2   # a project with this many real asks counts as worked on
GENERIC_NAME_TOKENS = {"partial", "project", "projects", "sandbox", "prod", "demo", "llc", "inc", "company"}


def nameTokenSet(nameText):
    return {token for token in re.findall(r"[a-z0-9]+", nameText.lower())
            if len(token) >= 4 and token not in GENERIC_NAME_TOKENS}


def projectIsLogged(projectName, projectHeaderList):
    projectTokenSet = nameTokenSet(projectName)
    for projectHeader in projectHeaderList:
        headerLower = projectHeader.lower()
        if projectName.lower() in headerLower or projectTokenSet & nameTokenSet(projectHeader):
            return True
    return False


def daySection(timesheetContent, dateLabel):
    headerMatch = re.search(rf"^### Day \d+ — \w+ {re.escape(dateLabel)}\b.*$",
                            timesheetContent, re.MULTILINE)
    if not headerMatch:
        return None
    nextMatch = re.search(r"^(### Day |## Week)", timesheetContent[headerMatch.end():], re.MULTILINE)
    sectionEnd = headerMatch.end() + nextMatch.start() if nextMatch else len(timesheetContent)
    return timesheetContent[headerMatch.start():sectionEnd]


def parseEvidence(evidenceText):
    """Return (projectNameVsSubstantiveCountMap, projectNameVsCliCountMap, meetingList)."""
    projectNameVsSubstantiveCountMap, projectNameVsCliCountMap, meetingList = {}, {}, []
    currentProject, currentBlock = None, None
    for evidenceLine in evidenceText.splitlines():
        if evidenceLine.startswith("## Meetings"):
            currentProject, currentBlock = None, "meetings"
            continue
        projectMatch = re.match(r"^## Project: (.+)$", evidenceLine)
        if projectMatch:
            currentProject, currentBlock = projectMatch.group(1).strip(), None
            projectNameVsSubstantiveCountMap.setdefault(currentProject, 0)
            projectNameVsCliCountMap.setdefault(currentProject, 0)
            continue
        if evidenceLine.startswith("### User messages"):
            currentBlock = "messages"
            continue
        if evidenceLine.startswith("### CLI events"):
            currentBlock = "cli"
            continue
        if currentBlock == "meetings":
            meetingMatch = re.match(r"^- \*\*[\d:–-]+\*\* \(\d+m\) (.+?) — .*?→ \*\*(.+?)\*\*", evidenceLine)
            if meetingMatch:
                meetingList.append((meetingMatch.group(1).strip(), meetingMatch.group(2).strip()))
        elif currentBlock == "messages" and currentProject:
            messageMatch = re.match(r"^- \*\*[\d:]+\*\* \[[0-9a-f]+\] (.*)$", evidenceLine)
            if messageMatch:
                messageText = messageMatch.group(1).strip()
                if messageText.startswith("[META?]") or TRIVIAL_MESSAGE_PATTERN.search(messageText):
                    continue
                projectNameVsSubstantiveCountMap[currentProject] += 1
        elif currentBlock == "cli" and currentProject and evidenceLine.startswith("- **"):
            projectNameVsCliCountMap[currentProject] += 1
    return projectNameVsSubstantiveCountMap, projectNameVsCliCountMap, meetingList


def commitCountsByRepo(startUtc, endUtc):
    if not COMMITS_SCRIPT.exists():
        return {}
    try:
        commitsResult = subprocess.run(
            ["python3", str(COMMITS_SCRIPT), "--since", startUtc.isoformat(), "--until", endUtc.isoformat()],
            capture_output=True, text=True, timeout=90,
        )
    except Exception:
        return {}
    repoNameVsCommitCountMap, currentRepoName = {}, None
    for outputLine in commitsResult.stdout.splitlines():
        repoMatch = re.match(r"^== (.+?)\s{2}\(", outputLine)
        if repoMatch:
            currentRepoName = repoMatch.group(1).strip()
            continue
        countMatch = re.match(r"^\s*commits: (\d+)", outputLine)
        if countMatch and currentRepoName and int(countMatch.group(1)) > 0:
            repoNameVsCommitCountMap[currentRepoName] = int(countMatch.group(1))
    return repoNameVsCommitCountMap


def main():
    argumentParser = argparse.ArgumentParser()
    argumentParser.add_argument("--days", type=int, default=7)
    argumentParser.add_argument("--no-compile", action="store_true")
    parsedArguments = argumentParser.parse_args()

    if not CONFIG.exists() or not TIMESHEET.exists():
        return
    config = json.loads(CONFIG.read_text())
    timezoneName = config.get("platform", {}).get("timezone") or "UTC"
    workHours = config.get("modules", {}).get("timesheet", {}).get("work_hours", {})
    if not (workHours.get("start") and workHours.get("end")):
        return
    userTimezone = ZoneInfo(timezoneName)
    startHour, startMinute = map(int, workHours["start"].split(":"))
    endHour, endMinute = map(int, workHours["end"].split(":"))
    crossesMidnight = (endHour, endMinute) < (startHour, startMinute)
    nowLocal = datetime.now(tz_mod.utc).astimezone(userTimezone)
    timesheetContent = TIMESHEET.read_text()

    gapLineList = []
    for daysBack in range(parsedArguments.days, -1, -1):
        shiftDate = nowLocal.date() - timedelta(days=daysBack)
        shiftStartLocal = datetime(shiftDate.year, shiftDate.month, shiftDate.day,
                                   startHour, startMinute, tzinfo=userTimezone)
        shiftEndLocal = shiftStartLocal.replace(hour=endHour, minute=endMinute) + (
            timedelta(days=1) if crossesMidnight else timedelta(0))
        if nowLocal < shiftEndLocal:
            continue  # shift not finished — in-progress days are allowed to be partial
        dateLabel = shiftDate.isoformat()

        evidencePath = EVIDENCE_DIR / f"{dateLabel}.md"
        if not parsedArguments.no_compile and COMPILER.exists():
            subprocess.run(["python3", str(COMPILER), dateLabel], capture_output=True, text=True, timeout=60)
        evidenceText = evidencePath.read_text() if evidencePath.exists() else ""
        projectNameVsSubstantiveCountMap, projectNameVsCliCountMap, meetingList = parseEvidence(evidenceText)
        repoNameVsCommitCountMap = commitCountsByRepo(
            shiftStartLocal.astimezone(tz_mod.utc), shiftEndLocal.astimezone(tz_mod.utc))

        workedProjectVsReasonMap = {}
        for projectName, substantiveCount in projectNameVsSubstantiveCountMap.items():
            cliCount = projectNameVsCliCountMap.get(projectName, 0)
            if substantiveCount >= MIN_SUBSTANTIVE_MESSAGES or cliCount:
                workedProjectVsReasonMap[projectName] = f"{substantiveCount} messages, {cliCount} CLI events"
        for repoName, commitCount in repoNameVsCommitCountMap.items():
            if not any(nameTokenSet(repoName) & nameTokenSet(projectName) for projectName in workedProjectVsReasonMap):
                workedProjectVsReasonMap[repoName] = f"{commitCount} commits"

        dayText = daySection(timesheetContent, dateLabel)
        dayGapLineList = []
        if dayText is None:
            if workedProjectVsReasonMap:
                dayGapLineList.append("MISSING DAY — no header in timesheet")
            elif meetingList:
                meetingTitleText = ", ".join(meetingTitle for meetingTitle, _ in meetingList)
                dayGapLineList.append(f"MEETINGS ONLY, no work evidence ({meetingTitleText}) — ask the user: "
                                      "log them, mark PTO, or `wt_calendar.py skip` if personal")
        else:
            if "*(in progress)*" in dayText:
                dayGapLineList.append("STILL IN PROGRESS — shift ended but marker still there; re-check the whole day")
            projectHeaderList = re.findall(r"^\*\*Project: (.+?)\*\*", dayText, re.MULTILINE)
            for projectName, reasonText in workedProjectVsReasonMap.items():
                if not projectIsLogged(projectName, projectHeaderList):
                    dayGapLineList.append(f"MISSING PROJECT — {projectName} ({reasonText})")
            for meetingTitle, meetingProject in meetingList:
                if meetingTitle.lower() not in dayText.lower():
                    dayGapLineList.append(f"MISSING MEETING — {meetingTitle} (→ {meetingProject})")
        for dayGapLine in dayGapLineList:
            gapLineList.append(f"{dateLabel}: {dayGapLine}")

    if gapLineList:
        print("🔍 Timesheet coverage gaps (completed shifts) — read each day's evidence at "
              f"{EVIDENCE_DIR}/<date>.md, add the missing bullets, then sync:")
        for gapLine in gapLineList:
            print(f"  • {gapLine}")


if __name__ == "__main__":
    main()
