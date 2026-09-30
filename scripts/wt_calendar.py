"""Google Calendar reader for shift evidence (stdlib only — no pip installs).

Reads the user's PRIMARY Google Calendar (read-only scope) and returns the
meetings that overlap a shift window, so wt_compile_shift.py can list them in
the evidence file alongside session messages + CLI events.

One-time setup per laptop:
  1. Google Cloud project → enable Google Calendar API → OAuth client
     (type "Desktop app") → download JSON to
     ~/Documents/DevPlatform/.secrets/google_client.json
  2. python3 ~/Documents/DevPlatform/scripts/wt_calendar.py auth
     (opens the browser once; stores a refresh token in .secrets/google_token.json)

Usage:
  wt_calendar.py auth                 # one-time browser consent
  wt_calendar.py list YYYY-MM-DD      # print meetings for that shift (debug)
  wt_calendar.py map "<title keyword>" <Project|Internal>   # remember a meeting's project
  wt_calendar.py map-domain <client.com> <Project>          # attendee domain → project
  wt_calendar.py skip "<title keyword>"                     # never log this meeting

If either secrets file is missing, fetchShiftMeetings() returns None and the
evidence compile simply skips the Meetings section — teammates who never set
this up see no change.
"""

import sys, json, time, secrets, hashlib, base64, webbrowser
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone as tz_mod
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

DP = Path.home() / "Documents" / "DevPlatform"
SECRETS_DIR = DP / ".secrets"
CLIENT_FILE = SECRETS_DIR / "google_client.json"
TOKEN_FILE = SECRETS_DIR / "google_token.json"
# Meetings the user adds by hand in the dashboard Calendar module (Slack huddles,
# calls, …). Written by the browser into the data repo; pulled down by dpsync.
MANUAL_MEETINGS_FILE = DP / "sync" / "modules" / "calendar" / "manual.json"

SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
HTTP_TIMEOUT_SECONDS = 15

# Calendar entries that are not meetings.
NON_MEETING_EVENT_TYPE_SET = {"focusTime", "outOfOffice", "workingLocation", "birthday"}


CONFIG_FILE = DP / "config.json"
INTERNAL_PROJECT = "Internal"
UNASSIGNED_PROJECT = "UNASSIGNED"


def loadConfigMap() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def calendarSettingsOf(configMap: dict) -> dict:
    """config.json modules.timesheet.calendar — per-user, never synced:
      exclude_title_keywords   : [keyword]          meetings to drop entirely
      title_keyword_to_project : {keyword: project}  learned / user-given mappings
      domain_to_project        : {domain: project}   client attendee domains
    Keyword matching is case-insensitive substring on the event title.
    """
    return configMap.get("modules", {}).get("timesheet", {}).get("calendar", {})


def loadExcludedTitleKeywordList() -> list:
    keywordList = calendarSettingsOf(loadConfigMap()).get("exclude_title_keywords", [])
    return [keyword.lower() for keyword in keywordList if keyword]


def knownProjectNameList(configMap: dict) -> list:
    projectDetectionMap = configMap.get("project_detection", {})
    projectNameSet = set(projectDetectionMap.get("org_id_to_project", {}).values())
    projectNameSet |= set(projectDetectionMap.get("company_to_project", {}).values())
    return sorted(projectNameSet)


def resolveMeetingProject(meetingTitle: str, attendeeDomainSet: set, configMap: dict):
    """Return (project, reason). Order: user's keyword map (longest keyword wins)
    → project name in title → client attendee domain → UNASSIGNED (ask the user)."""
    calendarSettings = calendarSettingsOf(configMap)
    titleLower = meetingTitle.lower()

    keywordVsProjectMap = {keyword.lower(): projectName for keyword, projectName
                           in calendarSettings.get("title_keyword_to_project", {}).items() if keyword}
    for projectName in knownProjectNameList(configMap):
        keywordVsProjectMap.setdefault(projectName.lower(), projectName)
    for keyword in sorted(keywordVsProjectMap, key=len, reverse=True):
        if keyword in titleLower:
            return keywordVsProjectMap[keyword], f'title has "{keyword}"'

    domainVsProjectMap = {domain.lower(): projectName for domain, projectName
                          in calendarSettings.get("domain_to_project", {}).items()}
    for attendeeDomain in sorted(attendeeDomainSet):
        if attendeeDomain in domainVsProjectMap:
            return domainVsProjectMap[attendeeDomain], f"attendee from {attendeeDomain}"

    return UNASSIGNED_PROJECT, "no match"


def saveCalendarSetting(settingKey: str, entryKey: str, entryValue=None):
    """Persist a keyword mapping (dict setting) or skip keyword (list setting)."""
    configMap = json.loads(CONFIG_FILE.read_text())
    calendarSettings = (configMap.setdefault("modules", {}).setdefault("timesheet", {})
                        .setdefault("calendar", {}))
    if entryValue is None:
        keywordList = calendarSettings.setdefault(settingKey, [])
        if entryKey not in keywordList:
            keywordList.append(entryKey)
    else:
        calendarSettings.setdefault(settingKey, {})[entryKey] = entryValue
    CONFIG_FILE.write_text(json.dumps(configMap, indent=2) + "\n")


def loadClientConfig() -> dict:
    clientJson = json.loads(CLIENT_FILE.read_text())
    return clientJson.get("installed") or clientJson.get("web") or {}


def writePrivateJson(targetPath: Path, payloadDict: dict):
    SECRETS_DIR.mkdir(mode=0o700, exist_ok=True)
    targetPath.write_text(json.dumps(payloadDict, indent=2))
    targetPath.chmod(0o600)


def postForm(url: str, formFieldMap: dict) -> dict:
    requestBody = urllib.parse.urlencode(formFieldMap).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=requestBody),
                                timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


# ---------------------------------------------------------------- auth ----

def runConsentFlow():
    """Loopback OAuth (PKCE) — opens the browser, catches the redirect locally."""
    if not CLIENT_FILE.exists():
        print(f"missing {CLIENT_FILE} — download the Desktop OAuth client JSON first.")
        sys.exit(1)
    clientConfig = loadClientConfig()

    codeVerifier = secrets.token_urlsafe(64)
    codeChallenge = base64.urlsafe_b64encode(
        hashlib.sha256(codeVerifier.encode()).digest()).decode().rstrip("=")
    stateToken = secrets.token_urlsafe(16)
    callbackResultMap = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            queryParamMap = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" not in queryParamMap and "error" not in queryParamMap:
                self.send_response(404); self.end_headers(); return
            callbackResultMap.update(queryParamMap)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            resultMessage = ("WorkTrace calendar access granted — you can close this tab."
                             if "code" in queryParamMap else
                             f"Authorization failed: {queryParamMap.get('error')}")
            self.wfile.write(f"<h3>{resultMessage}</h3>".encode())

        def log_message(self, *args):
            pass

    callbackServer = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    redirectUri = f"http://127.0.0.1:{callbackServer.server_port}"
    consentUrl = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": clientConfig["client_id"],
        "redirect_uri": redirectUri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": stateToken,
        "code_challenge": codeChallenge,
        "code_challenge_method": "S256",
    })
    print("Opening browser for Google consent. If it doesn't open, visit:\n" + consentUrl)
    webbrowser.open(consentUrl)
    while not callbackResultMap:
        callbackServer.handle_request()
    callbackServer.server_close()

    if "error" in callbackResultMap:
        print(f"✗ Consent failed: {callbackResultMap['error']}")
        sys.exit(1)
    if callbackResultMap.get("state") != stateToken:
        print("✗ State mismatch — aborting.")
        sys.exit(1)

    tokenResponseMap = postForm(TOKEN_URL, {
        "client_id": clientConfig["client_id"],
        "client_secret": clientConfig["client_secret"],
        "code": callbackResultMap["code"],
        "code_verifier": codeVerifier,
        "redirect_uri": redirectUri,
        "grant_type": "authorization_code",
    })
    if "refresh_token" not in tokenResponseMap:
        print("✗ Google returned no refresh token — re-run auth.")
        sys.exit(1)
    tokenResponseMap["expires_at"] = time.time() + tokenResponseMap.get("expires_in", 3600) - 60
    writePrivateJson(TOKEN_FILE, tokenResponseMap)
    print(f"✓ Calendar access saved to {TOKEN_FILE}")


def getAccessToken() -> Optional[str]:
    if not (CLIENT_FILE.exists() and TOKEN_FILE.exists()):
        return None
    tokenMap = json.loads(TOKEN_FILE.read_text())
    if tokenMap.get("access_token") and tokenMap.get("expires_at", 0) > time.time():
        return tokenMap["access_token"]
    clientConfig = loadClientConfig()
    refreshedTokenMap = postForm(TOKEN_URL, {
        "client_id": clientConfig["client_id"],
        "client_secret": clientConfig["client_secret"],
        "refresh_token": tokenMap["refresh_token"],
        "grant_type": "refresh_token",
    })
    tokenMap["access_token"] = refreshedTokenMap["access_token"]
    tokenMap["expires_at"] = time.time() + refreshedTokenMap.get("expires_in", 3600) - 60
    writePrivateJson(TOKEN_FILE, tokenMap)
    return tokenMap["access_token"]


# ------------------------------------------------------------- meetings ----

def parseEventTime(eventTimeMap: dict) -> datetime:
    return datetime.fromisoformat(eventTimeMap["dateTime"].replace("Z", "+00:00"))


def fetchCalendarMeetings(startUtc: datetime, endUtc: datetime) -> Optional[list]:
    """Every meeting-like event on the primary calendar overlapping [startUtc, endUtc],
    including ones the timesheet leaves out — those carry an `excluded_reason`
    ("Declined" / "On your skip list") instead of None.

    Returns None when calendar access isn't set up (caller skips the section).
    Raises on network/API errors — caller decides how to surface them.

    Never returned at all (not meetings): all-day entries, focus time / OOO /
    working-location blocks, cancelled events, solo blocks with no other attendee.
    """
    accessToken = getAccessToken()
    if accessToken is None:
        return None

    rawEventList = []
    pageToken = None
    while True:
        queryParamMap = {
            "timeMin": startUtc.isoformat(),
            "timeMax": endUtc.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "250",
        }
        if pageToken:
            queryParamMap["pageToken"] = pageToken
        eventsRequest = urllib.request.Request(
            EVENTS_URL + "?" + urllib.parse.urlencode(queryParamMap),
            headers={"Authorization": f"Bearer {accessToken}"})
        with urllib.request.urlopen(eventsRequest, timeout=HTTP_TIMEOUT_SECONDS) as response:
            eventsPageMap = json.loads(response.read())
        rawEventList.extend(eventsPageMap.get("items", []))
        pageToken = eventsPageMap.get("nextPageToken")
        if not pageToken:
            break

    excludedTitleKeywordList = loadExcludedTitleKeywordList()
    configMap = loadConfigMap()
    meetingList = []
    for calendarEvent in rawEventList:
        if calendarEvent.get("status") == "cancelled":
            continue
        if calendarEvent.get("eventType", "default") in NON_MEETING_EVENT_TYPE_SET:
            continue
        if "dateTime" not in calendarEvent.get("start", {}):
            continue  # all-day entry

        attendeeList = calendarEvent.get("attendees", [])
        selfAttendee = next((attendee for attendee in attendeeList if attendee.get("self")), None)
        otherAttendeeList = [attendee for attendee in attendeeList
                             if not attendee.get("self") and not attendee.get("resource")]
        if not otherAttendeeList:
            continue  # solo block, not a meeting
        responseStatus = selfAttendee.get("responseStatus") if selfAttendee else "accepted"

        meetingTitle = calendarEvent.get("summary", "(no title)")
        excludedReason = None
        if any(keyword in meetingTitle.lower() for keyword in excludedTitleKeywordList):
            excludedReason = "On your skip list"
        elif responseStatus == "declined":
            excludedReason = "Declined"

        eventStart = parseEventTime(calendarEvent["start"])
        eventEnd = parseEventTime(calendarEvent["end"])
        attendeeDomainSet = {attendee["email"].split("@")[-1].lower()
                             for attendee in otherAttendeeList if "@" in attendee.get("email", "")}
        meetingProject, projectReason = resolveMeetingProject(meetingTitle, attendeeDomainSet, configMap)
        meetingList.append({
            "event_id": calendarEvent.get("id", ""),
            "source": "google",
            "excluded_reason": excludedReason,
            "project": meetingProject,
            "project_reason": projectReason,
            "recurring": bool(calendarEvent.get("recurringEventId")),
            "start": eventStart,
            "end": eventEnd,
            "minutes": int((eventEnd - eventStart).total_seconds() // 60),
            "title": meetingTitle,
            "response": responseStatus,
            "organizer_is_self": bool(calendarEvent.get("organizer", {}).get("self")),
            "attendee_count": len(otherAttendeeList),
            "attendee_domains": sorted(attendeeDomainSet),
        })
    return meetingList


def loadManualMeetings(startUtc: datetime, endUtc: datetime) -> list:
    """Dashboard-added meetings overlapping [startUtc, endUtc], in the same shape
    as fetchCalendarMeetings entries (source="manual", project already chosen)."""
    try:
        manualMeetingRecordList = json.loads(MANUAL_MEETINGS_FILE.read_text()).get("meetings", [])
    except (OSError, ValueError):
        return []
    meetingList = []
    for manualMeetingRecord in manualMeetingRecordList:
        try:
            meetingStart = datetime.fromisoformat(manualMeetingRecord["start"].replace("Z", "+00:00"))
            meetingEnd = datetime.fromisoformat(manualMeetingRecord["end"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if meetingEnd <= startUtc or meetingStart >= endUtc:
            continue
        meetingMedium = manualMeetingRecord.get("medium") or "Other"
        meetingList.append({
            "event_id": manualMeetingRecord.get("id", ""),
            "source": "manual",
            "medium": meetingMedium,
            "notes": manualMeetingRecord.get("notes", ""),
            "excluded_reason": None,
            "project": manualMeetingRecord.get("project") or UNASSIGNED_PROJECT,
            "project_reason": f"added by you ({meetingMedium})",
            "recurring": False,
            "start": meetingStart,
            "end": meetingEnd,
            "minutes": int((meetingEnd - meetingStart).total_seconds() // 60),
            "title": manualMeetingRecord.get("title") or "(no title)",
            "response": "accepted",
            "organizer_is_self": True,
            "attendee_count": 0,
            "attendee_domains": [],
        })
    return meetingList


def fetchShiftMeetings(startUtc: datetime, endUtc: datetime) -> Optional[list]:
    """Meetings that belong in the timesheet: Google meetings minus declined
    invites and the user's skip list, plus meetings added by hand in the
    dashboard. None only when there is neither calendar access nor any manual one."""
    googleMeetingList = fetchCalendarMeetings(startUtc, endUtc)
    manualMeetingList = loadManualMeetings(startUtc, endUtc)
    if googleMeetingList is None and not manualMeetingList:
        return None
    meetingList = [meeting for meeting in (googleMeetingList or []) if not meeting["excluded_reason"]]
    return sorted(meetingList + manualMeetingList, key=lambda meeting: meeting["start"])


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "auth":
        runConsentFlow()
        return
    if len(sys.argv) == 3 and sys.argv[1] == "list":
        sys.path.insert(0, str(Path(__file__).parent))
        from wt_compile_shift import shiftWindowUtc, CONFIG
        from zoneinfo import ZoneInfo
        configMap = json.loads(CONFIG.read_text())
        startUtc, endUtc, timezoneName = shiftWindowUtc(sys.argv[2], configMap)
        meetingList = fetchShiftMeetings(startUtc, endUtc)
        if meetingList is None:
            print("Calendar not set up — run: wt_calendar.py auth")
            return
        userTimezone = ZoneInfo(timezoneName)
        print(f"{len(meetingList)} meeting(s) in shift {sys.argv[2]}:")
        for meeting in meetingList:
            print(f"  {meeting['start'].astimezone(userTimezone):%H:%M}–"
                  f"{meeting['end'].astimezone(userTimezone):%H:%M} ({meeting['minutes']}m) "
                  f"{meeting['title']} [{meeting['response']}, "
                  f"{meeting['attendee_count']} others: {', '.join(meeting['attendee_domains'])}]"
                  f" → {meeting['project']} ({meeting['project_reason']})"
                  f"{' [recurring]' if meeting['recurring'] else ''}")
        return
    if len(sys.argv) == 4 and sys.argv[1] == "map":
        saveCalendarSetting("title_keyword_to_project", sys.argv[2], sys.argv[3])
        print(f'✓ Meetings with "{sys.argv[2]}" in the title → {sys.argv[3]}')
        return
    if len(sys.argv) == 4 and sys.argv[1] == "map-domain":
        saveCalendarSetting("domain_to_project", sys.argv[2].lower(), sys.argv[3])
        print(f"✓ Meetings with attendees from {sys.argv[2]} → {sys.argv[3]}")
        return
    if len(sys.argv) == 3 and sys.argv[1] == "skip":
        saveCalendarSetting("exclude_title_keywords", sys.argv[2])
        print(f'✓ Meetings with "{sys.argv[2]}" in the title will be skipped')
        return
    print(__doc__)


if __name__ == "__main__":
    main()
