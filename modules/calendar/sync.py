"""
modules/calendar/sync.py — Calendar module sync layer.

Reads:
    ~/Documents/DevPlatform/config.json                      (platform + module config)
    Google Calendar (primary, read-only) via scripts/wt_calendar.py
    ~/Documents/DevPlatform/modules/timesheet/timesheet.md    (what's actually logged)

Writes (or prints on --dry-run):
    ~/Documents/DevPlatform/sync/modules/calendar/data.json

A meeting is "in scope" only when the timesheet logs it: its exact calendar
title appears on a `Meetings:` / `Internal meetings:` bullet of the same work
day. The project comes from the timesheet section that bullet sits under, so
one-off meetings the user assigned by hand show under the right project.

Every entry carries a status:
    logged    — in the timesheet; `project` is where it was logged
    excluded  — declined, or on the user's skip list (`reason` says which)
    missing   — the work day is logged but this meeting isn't on it
    pending   — the work day isn't logged yet (current shift); `project` is
                the auto-matched suggestion or null

Enable per laptop in config.json:
    "modules": { "calendar": { "enabled": true, "start_date": "YYYY-MM-DD" } }
Needs `python3 scripts/wt_calendar.py auth` to have been run once.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DP_DIR = Path.home() / "Documents" / "DevPlatform"
CONFIG_PATH = DP_DIR / "config.json"
TIMESHEET_PATH = DP_DIR / "modules" / "timesheet" / "timesheet.md"
OUTPUT_PATH = DP_DIR / "sync" / "modules" / "calendar" / "data.json"

sys.path.insert(0, str(DP_DIR / "scripts"))
sys.path.insert(0, str(DP_DIR / "modules" / "timesheet"))
from wt_calendar import fetchCalendarMeetings, INTERNAL_PROJECT, UNASSIGNED_PROJECT  # noqa: E402
from parser import parse_timesheet  # noqa: E402

RE_MEETING_BULLET = re.compile(r"^\s*(internal\s+)?meetings\s*:", re.IGNORECASE)


def log(message: str):
    print(message, file=sys.stderr)


def normalizeTitle(titleText: str) -> str:
    """Lowercase, dashes/punctuation → spaces, collapse whitespace — so
    'Sync - Genserve' in the calendar matches 'Sync – Genserve' in the timesheet."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", titleText.lower()).split())


def workDateOf(meetingStartUtc: datetime, userTimezone: ZoneInfo, shiftEndHourMinute: tuple,
               crossesMidnight: bool) -> str:
    """The shift (work day) a meeting belongs to — labelled by the shift's start date."""
    meetingStartLocal = meetingStartUtc.astimezone(userTimezone)
    if crossesMidnight and (meetingStartLocal.hour, meetingStartLocal.minute) < shiftEndHourMinute:
        return (meetingStartLocal.date() - timedelta(days=1)).isoformat()
    return meetingStartLocal.date().isoformat()


def buildLoggedMeetingIndex(timesheetText: str):
    """Return (loggedWorkDateSet, workDateVsMeetingBulletListMap).

    Each meeting bullet is (normalizedBulletText, projectFriendlyName)."""
    loggedWorkDateSet = set()
    workDateVsMeetingBulletListMap = {}
    for timesheetEntry in parse_timesheet(timesheetText):
        workDate = timesheetEntry["work_date"]
        loggedWorkDateSet.add(workDate)
        projectMap = timesheetEntry.get("project") or {}
        sectionProject = projectMap.get("friendly_name") or projectMap.get("company_name")
        for bulletText in timesheetEntry.get("bullets", []):
            bulletMatch = RE_MEETING_BULLET.match(bulletText.replace("**", ""))
            if not bulletMatch:
                continue
            bulletProject = INTERNAL_PROJECT if bulletMatch.group(1) else sectionProject
            workDateVsMeetingBulletListMap.setdefault(workDate, []).append(
                (normalizeTitle(bulletText), bulletProject))
    return loggedWorkDateSet, workDateVsMeetingBulletListMap


def buildPayload(configMap: dict, meetingList: list, timesheetText: str,
                 rangeStartDate: str, rangeEndUtc: datetime) -> dict:
    platformMap = configMap["platform"]
    workHoursMap = configMap["modules"]["timesheet"]["work_hours"]
    userTimezone = ZoneInfo(platformMap.get("timezone") or "UTC")
    shiftStartHourMinute = tuple(map(int, workHoursMap["start"].split(":")))
    shiftEndHourMinute = tuple(map(int, workHoursMap["end"].split(":")))
    crossesMidnight = shiftEndHourMinute < shiftStartHourMinute

    loggedWorkDateSet, workDateVsMeetingBulletListMap = buildLoggedMeetingIndex(timesheetText)

    entryList = []
    for meeting in meetingList:
        workDate = workDateOf(meeting["start"], userTimezone, shiftEndHourMinute, crossesMidnight)
        if workDate < rangeStartDate:
            continue
        normalizedMeetingTitle = normalizeTitle(meeting["title"])
        loggedProject = next((bulletProject for normalizedBullet, bulletProject
                              in workDateVsMeetingBulletListMap.get(workDate, [])
                              if normalizedMeetingTitle and normalizedMeetingTitle in normalizedBullet),
                             None)
        suggestedProject = None if meeting["project"] == UNASSIGNED_PROJECT else meeting["project"]

        if loggedProject:
            meetingStatus, statusReason, meetingProject = "logged", None, loggedProject
        elif meeting["excluded_reason"]:
            meetingStatus, statusReason, meetingProject = "excluded", meeting["excluded_reason"], suggestedProject
        elif workDate in loggedWorkDateSet:
            meetingStatus, statusReason, meetingProject = "missing", "Not on this day's timesheet", suggestedProject
        else:
            meetingStatus, meetingProject = "pending", suggestedProject
            statusReason = ("Shift not logged yet — suggested: " + suggestedProject
                            if suggestedProject else "Shift not logged yet — project to be confirmed")

        entryList.append({
            "id": "sha256:" + hashlib.sha256(
                (meeting["event_id"] + meeting["start"].isoformat()).encode()).hexdigest()[:16],
            "work_date": workDate,
            "start": meeting["start"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": meeting["end"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "minutes": meeting["minutes"],
            "title": meeting["title"],
            "project": meetingProject,
            "status": meetingStatus,
            "reason": statusReason,
            "response": meeting["response"],
            "organizer_is_self": meeting["organizer_is_self"],
            "attendee_count": meeting["attendee_count"],
            "attendee_domains": meeting["attendee_domains"],
            "recurring": meeting["recurring"],
        })
    entryList.sort(key=lambda calendarEntry: calendarEntry["start"])

    return {
        "schema_version": 1,
        "user_id": platformMap["user_id"],
        "display_name": platformMap.get("display_name", ""),
        "timezone": platformMap.get("timezone", ""),
        "work_shift": {"start": workHoursMap["start"], "end": workHoursMap["end"]},
        "range": {"start_date": rangeStartDate,
                  "end": rangeEndUtc.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "entries": entryList,
    }


def contentHash(payloadMap: dict) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps({key: value for key, value in payloadMap.items()
                    if key not in ("last_synced_at", "content_hash")},
                   sort_keys=True).encode()).hexdigest()


def currentShiftEndUtc(configMap: dict) -> datetime:
    """End of the shift that's running now (or the next one to end)."""
    workHoursMap = configMap["modules"]["timesheet"]["work_hours"]
    userTimezone = ZoneInfo(configMap["platform"].get("timezone") or "UTC")
    shiftEndHour, shiftEndMinute = map(int, workHoursMap["end"].split(":"))
    nowLocal = datetime.now(timezone.utc).astimezone(userTimezone)
    shiftEndLocal = nowLocal.replace(hour=shiftEndHour, minute=shiftEndMinute, second=0, microsecond=0)
    if shiftEndLocal <= nowLocal:
        shiftEndLocal += timedelta(days=1)
    return shiftEndLocal.astimezone(timezone.utc)


def main() -> int:
    argumentParser = argparse.ArgumentParser(
        description="Sync the Calendar module — Google Calendar meetings cross-checked "
                    "against timesheet.md, written into sync/.")
    argumentParser.add_argument("--dry-run", action="store_true",
                                help="Print the JSON to stdout instead of writing to disk.")
    parsedArguments = argumentParser.parse_args()

    configMap = json.loads(CONFIG_PATH.read_text())
    calendarModuleConfig = configMap.get("modules", {}).get("calendar", {})
    if not calendarModuleConfig.get("enabled"):
        log("Calendar module disabled in config.json — nothing to do.")
        return 0
    rangeStartDate = calendarModuleConfig.get("start_date") or datetime.now().date().isoformat()
    userTimezone = ZoneInfo(configMap["platform"].get("timezone") or "UTC")
    shiftStartHour, shiftStartMinute = map(int, configMap["modules"]["timesheet"]["work_hours"]["start"].split(":"))
    rangeStartUtc = datetime.fromisoformat(rangeStartDate).replace(
        hour=shiftStartHour, minute=shiftStartMinute, tzinfo=userTimezone).astimezone(timezone.utc)
    rangeEndUtc = currentShiftEndUtc(configMap)

    try:
        meetingList = fetchCalendarMeetings(rangeStartUtc, rangeEndUtc)
    except Exception as calendarException:
        # Never fail the whole dpsync over the calendar — keep last good file.
        log(f"⚠ calendar fetch failed ({type(calendarException).__name__}: {calendarException}) "
            f"— keeping previous data.json")
        if not OUTPUT_PATH.exists() and not parsedArguments.dry_run:
            OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT_PATH.write_text(json.dumps({"schema_version": 1, "entries": []}, indent=2) + "\n")
        return 0
    if meetingList is None:
        log("⚠ calendar not connected — run: python3 scripts/wt_calendar.py auth")
        meetingList = []

    timesheetText = TIMESHEET_PATH.read_text() if TIMESHEET_PATH.exists() else ""
    payloadMap = buildPayload(configMap, meetingList, timesheetText, rangeStartDate, rangeEndUtc)
    payloadMap["content_hash"] = contentHash(payloadMap)
    payloadMap["last_synced_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if parsedArguments.dry_run:
        print(json.dumps(payloadMap, indent=2))
        return 0

    if OUTPUT_PATH.exists():
        try:
            if json.loads(OUTPUT_PATH.read_text()).get("content_hash") == payloadMap["content_hash"]:
                log("(no-op: calendar content unchanged)")
                return 0
        except ValueError:
            pass
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payloadMap, indent=2) + "\n")
    statusVsCountMap = {}
    for calendarEntry in payloadMap["entries"]:
        statusVsCountMap[calendarEntry["status"]] = statusVsCountMap.get(calendarEntry["status"], 0) + 1
    log(f"✓ wrote {OUTPUT_PATH.relative_to(DP_DIR)} ({len(payloadMap['entries'])} meetings: "
        + ", ".join(f"{count} {status}" for status, count in sorted(statusVsCountMap.items())) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
