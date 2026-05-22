#!/usr/bin/env python3
"""Compile structured evidence for one work-shift, grouped by project.

Usage:
    wt_compile_shift.py YYYY-MM-DD

The date is the shift's *start* date in local timezone. The script:
  1. Reads config.json for the user's timezone + shift hours
  2. Computes the shift's UTC window (handles cross-midnight shifts)
  3. Iterates every Claude session JSONL under ~/.claude/projects/
  4. Filters to user messages with timestamps inside the window
  5. Attributes each session to a project via config.json's project_detection
  6. Reads cli-log.jsonl for tracked sf commands in the same window
  7. Writes a structured markdown evidence file at
     ~/Documents/DevPlatform/.shift_evidence/<date>.md

The LLM writes timesheet bullets from that evidence file instead of recalling
from session memory. This eliminates:
  - Cross-midnight bucketing errors (each event has a definitive UTC ts)
  - Fabricated entries (no evidence in window → bullet shouldn't exist)
  - Project misattribution (project is locked to session folder mapping)

Output is structured per project, with separate sections for session messages
and CLI events. Messages that look like WorkTrace meta-work are tagged so the
LLM can decide whether to include them.
"""
import sys, json, re
from datetime import datetime, timedelta, timezone as tz_mod
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import defaultdict

DP = Path.home() / "Documents" / "DevPlatform"
CONFIG = DP / "config.json"
CLI_LOG = DP / "cli-log.jsonl"
EVIDENCE_DIR = DP / ".shift_evidence"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

def buildMetaPattern(config: dict):
    """Build the META-keyword regex from config.json's keyword list."""
    metaPatterns = (
        config.get("modules", {})
        .get("timesheet", {})
        .get("meta_work_patterns", {})
    )
    keywordList = metaPatterns.get("keywords", [])
    if not keywordList:
        return None
    escapedKeywords = [re.escape(kw) for kw in keywordList]
    return re.compile(r"(" + "|".join(escapedKeywords) + r")", re.IGNORECASE)


def projectFromSessionFolder(folder_name: str, config: dict) -> str:
    """Map a ~/.claude/projects/ encoded folder name to a friendly project."""
    projectDetection = config.get("project_detection", {})
    aliasHints = projectDetection.get("alias_hints", {})
    # Match any alias hint as a substring of the folder name
    for hintName, friendlyName in aliasHints.items():
        if hintName.lower().replace(" ", "-") in folder_name.lower():
            return friendlyName
        if hintName.lower() in folder_name.lower():
            return friendlyName
    # Fallback — derive from the trailing path segment
    trailingSegment = folder_name.replace("-", "/").rsplit("/", 1)[-1]
    return trailingSegment or folder_name


def shiftWindowUtc(date_str: str, config: dict):
    """Return (start_utc, end_utc, tz_name) for the shift starting at date_str."""
    timezoneName = config.get("platform", {}).get("timezone") or "UTC"
    userTimezone = ZoneInfo(timezoneName)
    shiftConfig = config.get("modules", {}).get("timesheet", {}).get("work_hours", {})
    startHour, startMinute = map(int, shiftConfig.get("start", "14:00").split(":"))
    endHour, endMinute = map(int, shiftConfig.get("end", "05:00").split(":"))
    crossesMidnight = (endHour, endMinute) < (startHour, startMinute)
    shiftStartLocal = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=startHour, minute=startMinute, tzinfo=userTimezone
    )
    shiftEndLocal = shiftStartLocal.replace(hour=endHour, minute=endMinute) + (
        timedelta(days=1) if crossesMidnight else timedelta(0)
    )
    return (
        shiftStartLocal.astimezone(tz_mod.utc),
        shiftEndLocal.astimezone(tz_mod.utc),
        timezoneName,
    )


def collectSessionEvents(start_utc, end_utc, config):
    """Iterate user messages from all session JSONLs whose ts falls in window."""
    sessionEventList = []
    for jsonlFile in CLAUDE_PROJECTS.rglob("*.jsonl"):
        if "subagents" in jsonlFile.parts:
            continue
        projectName = projectFromSessionFolder(jsonlFile.parent.name, config)
        sessionIdShort = jsonlFile.name[:8]
        try:
            with open(jsonlFile) as fh:
                for line in fh:
                    try:
                        eventObject = json.loads(line)
                    except Exception:
                        continue
                    if eventObject.get("type") != "user":
                        continue
                    timestampString = eventObject.get("timestamp")
                    if not timestampString:
                        continue
                    try:
                        eventTimestamp = datetime.fromisoformat(
                            timestampString.replace("Z", "+00:00")
                        )
                    except Exception:
                        continue
                    if eventTimestamp < start_utc or eventTimestamp > end_utc:
                        continue
                    messageObject = eventObject.get("message", {})
                    contentField = messageObject.get("content", "")
                    if isinstance(contentField, list):
                        textParts = [
                            c.get("text", "")
                            for c in contentField
                            if isinstance(c, dict) and c.get("type") == "text"
                        ]
                        if not textParts:
                            continue
                        messageText = " ".join(textParts)
                    else:
                        messageText = contentField
                    if not isinstance(messageText, str) or not messageText.strip():
                        continue
                    if messageText.startswith("<system-reminder>"):
                        continue
                    if messageText.startswith("<command-name>"):
                        continue
                    if messageText.startswith("Caveat:"):
                        continue
                    messageText = re.sub(
                        r"<ide_[^>]+>.*?</[^>]+>", "", messageText, flags=re.DOTALL
                    ).strip()
                    if not messageText:
                        continue
                    sessionEventList.append(
                        (eventTimestamp, projectName, sessionIdShort, messageText)
                    )
        except Exception:
            continue
    return sessionEventList


def collectCliEvents(start_utc, end_utc):
    cliEventList = []
    if not CLI_LOG.exists():
        return cliEventList
    with open(CLI_LOG) as fh:
        for line in fh:
            try:
                cliObject = json.loads(line)
            except Exception:
                continue
            timestampString = cliObject.get("ts", "")
            try:
                eventTimestamp = datetime.fromisoformat(
                    timestampString.replace("Z", "+00:00")
                )
            except Exception:
                continue
            if eventTimestamp < start_utc or eventTimestamp > end_utc:
                continue
            cliEventList.append(
                (
                    eventTimestamp,
                    cliObject.get("project", "?"),
                    cliObject.get("cmd", ""),
                    cliObject.get("exit_code", 0),
                )
            )
    return cliEventList


def main():
    if len(sys.argv) != 2:
        print("usage: wt_compile_shift.py YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    shiftDate = sys.argv[1]
    try:
        datetime.strptime(shiftDate, "%Y-%m-%d")
    except ValueError:
        print(f"invalid date format (need YYYY-MM-DD): {shiftDate}", file=sys.stderr)
        sys.exit(1)

    try:
        config = json.loads(CONFIG.read_text())
    except Exception as e:
        print(f"cannot read config.json: {e}", file=sys.stderr)
        sys.exit(1)

    startUtc, endUtc, timezoneName = shiftWindowUtc(shiftDate, config)
    userTimezone = ZoneInfo(timezoneName)
    metaPattern = buildMetaPattern(config)

    sessionEventList = collectSessionEvents(startUtc, endUtc, config)
    cliEventList = collectCliEvents(startUtc, endUtc)

    EVIDENCE_DIR.mkdir(exist_ok=True)
    outputPath = EVIDENCE_DIR / f"{shiftDate}.md"

    eventsByProject = defaultdict(list)
    for ts, proj, sess, text in sessionEventList:
        eventsByProject[proj].append((ts, sess, text))

    cliByProject = defaultdict(list)
    for ts, proj, cmd, exitCode in cliEventList:
        cliByProject[proj].append((ts, cmd, exitCode))

    allProjects = sorted(
        set(list(eventsByProject.keys()) + list(cliByProject.keys()))
    )

    with open(outputPath, "w") as outFh:
        outFh.write(f"# Shift evidence for {shiftDate}\n\n")
        outFh.write(f"**Window (UTC):** {startUtc.isoformat()} → {endUtc.isoformat()}\n")
        outFh.write(
            f"**Window ({timezoneName}):** "
            f"{startUtc.astimezone(userTimezone).strftime('%Y-%m-%d %H:%M')} → "
            f"{endUtc.astimezone(userTimezone).strftime('%Y-%m-%d %H:%M')}\n\n"
        )
        outFh.write(
            f"**Sources:** {len(sessionEventList)} session user-messages, "
            f"{len(cliEventList)} tracked CLI commands across "
            f"{len(allProjects)} project(s)\n\n"
        )
        outFh.write(
            "_When writing timesheet bullets from this evidence, each bullet "
            "should cite (mentally or in writing) at least one timestamp here. "
            "Messages tagged `[META?]` are likely Claude/WorkTrace meta-work and "
            "should NOT appear in client-project timesheet entries._\n\n"
        )
        outFh.write("---\n\n")

        if not allProjects:
            outFh.write("_No evidence in this shift window._\n")

        for projectName in allProjects:
            outFh.write(f"## Project: {projectName}\n\n")

            sessionMessages = eventsByProject.get(projectName, [])
            outFh.write(f"### User messages ({len(sessionMessages)})\n\n")
            if not sessionMessages:
                outFh.write("_(none)_\n\n")
            else:
                sessionMessages.sort()
                for ts, sess, text in sessionMessages:
                    timestampLocal = ts.astimezone(userTimezone).strftime("%H:%M")
                    summary = text.replace("\n", " ")[:220]
                    metaTag = " [META?]" if (metaPattern and metaPattern.search(text)) else ""
                    outFh.write(f"- **{timestampLocal}** [{sess}]{metaTag} {summary}\n")
                outFh.write("\n")

            cliMessages = cliByProject.get(projectName, [])
            outFh.write(f"### CLI events ({len(cliMessages)})\n\n")
            if not cliMessages:
                outFh.write("_(none)_\n\n")
            else:
                cliMessages.sort()
                for ts, cmd, exitCode in cliMessages:
                    timestampLocal = ts.astimezone(userTimezone).strftime("%H:%M")
                    statusMark = "✓" if exitCode == 0 else "✗"
                    outFh.write(f"- **{timestampLocal}** {statusMark} `{cmd[:170]}`\n")
                outFh.write("\n")

    print(f"✓ Wrote evidence: {outputPath}")
    print(
        f"  Sessions: {len(sessionEventList)} user-msgs | CLI: {len(cliEventList)} events "
        f"| Projects: {', '.join(allProjects) if allProjects else '(none)'}"
    )


if __name__ == "__main__":
    main()
