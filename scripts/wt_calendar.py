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

SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
HTTP_TIMEOUT_SECONDS = 15

# Calendar entries that are not meetings.
NON_MEETING_EVENT_TYPE_SET = {"focusTime", "outOfOffice", "workingLocation", "birthday"}


def loadExcludedTitleKeywordList() -> list:
    """Per-user skip list: config.json modules.timesheet.calendar.exclude_title_keywords.

    Case-insensitive substring match on the event title — for recurring invites
    the user never attends, or "reminder" events that aren't real meetings.
    """
    try:
        configMap = json.loads((DP / "config.json").read_text())
    except Exception:
        return []
    keywordList = (configMap.get("modules", {}).get("timesheet", {})
                   .get("calendar", {}).get("exclude_title_keywords", []))
    return [keyword.lower() for keyword in keywordList if keyword]


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


def fetchShiftMeetings(startUtc: datetime, endUtc: datetime) -> Optional[list]:
    """Meetings on the primary calendar overlapping [startUtc, endUtc].

    Returns None when calendar access isn't set up (caller skips the section).
    Raises on network/API errors — caller decides how to surface them.

    Kept: timed events with at least one other attendee that the user did not
    decline. Dropped: all-day entries, focus time / OOO / working-location
    blocks, cancelled events, solo blocks, declined invites, and titles on the
    user's exclude_title_keywords list.
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
    meetingList = []
    for calendarEvent in rawEventList:
        eventTitleLower = calendarEvent.get("summary", "").lower()
        if any(keyword in eventTitleLower for keyword in excludedTitleKeywordList):
            continue  # user's personal skip list
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
        if responseStatus == "declined":
            continue

        eventStart = parseEventTime(calendarEvent["start"])
        eventEnd = parseEventTime(calendarEvent["end"])
        attendeeDomainSet = {attendee["email"].split("@")[-1].lower()
                             for attendee in otherAttendeeList if "@" in attendee.get("email", "")}
        meetingList.append({
            "start": eventStart,
            "end": eventEnd,
            "minutes": int((eventEnd - eventStart).total_seconds() // 60),
            "title": calendarEvent.get("summary", "(no title)"),
            "response": responseStatus,
            "organizer_is_self": bool(calendarEvent.get("organizer", {}).get("self")),
            "attendee_count": len(otherAttendeeList),
            "attendee_domains": sorted(attendeeDomainSet),
        })
    return meetingList


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
                  f"{meeting['attendee_count']} others: {', '.join(meeting['attendee_domains'])}]")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
