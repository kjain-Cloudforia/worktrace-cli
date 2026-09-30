"""Keep the dashboard Calendar fresh: run dpsync every N minutes in the background.

dpsync is what refreshes the Calendar module (Google meetings + dashboard-added
meetings cross-checked against the timesheet) and pushes it to the data repo.
It's lock-protected and no-ops when nothing changed, so running it on a timer
only creates a commit when a meeting actually changed.

Why a tiny app and not a plain background job: WorkTrace lives in ~/Documents,
which macOS protects. A windowless job (python3 under launchd) can't ask for
access, so it would need Full Disk Access. Wrapping the run in a small
"WorkTrace Calendar Sync" app lets macOS show its normal one-time prompt —
"… would like to access files in your Documents folder" — so only this app
gets Documents-only access. The LaunchAgent just opens the app on a timer.

Usage:
  wt_calendar_autosync.py install [--minutes 30]   # build the app + load the LaunchAgent
  wt_calendar_autosync.py status                   # loaded? last run from the log
  wt_calendar_autosync.py uninstall                # unload + delete agent and app

Runs only while the laptop is awake and logged in.
Log: ~/Library/Logs/worktrace-calendar-sync.log
Permission: System Settings → Privacy & Security → Files and Folders → WorkTrace Calendar Sync.
"""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

AGENT_LABEL = "com.worktrace.calendar-sync"
APP_NAME = "WorkTrace Calendar Sync"
APP_PATH = Path.home() / "Applications" / f"{APP_NAME}.app"
AGENT_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "worktrace-calendar-sync.log"
DPSYNC_PATH = Path.home() / "Documents" / "DevPlatform" / "dpsync.py"
LAUNCHD_DOMAIN = f"gui/{os.getuid()}"


def runCommand(*argumentList: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argumentList), capture_output=True, text=True)


def buildSyncApp():
    """Compile the AppleScript applet, hide it from the Dock, ad-hoc sign it."""
    shellCommand = (f"/bin/date '+=== %Y-%m-%d %H:%M:%S ===' >> '{LOG_PATH}'; "
                    f"/usr/bin/python3 '{DPSYNC_PATH}' >> '{LOG_PATH}' 2>&1 || true")
    appleScriptSource = (
        "try\n"
        f"  do shell script \"{shellCommand}\"\n"
        "end try\n"   # never pop an error dialog from a background run
    )
    APP_PATH.parent.mkdir(parents=True, exist_ok=True)
    if APP_PATH.exists():
        shutil.rmtree(APP_PATH)
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as scriptFile:
        scriptFile.write(appleScriptSource)
    compileResult = runCommand("osacompile", "-o", str(APP_PATH), scriptFile.name)
    os.unlink(scriptFile.name)
    if compileResult.returncode != 0:
        print(f"✗ osacompile failed: {compileResult.stderr.strip()}")
        sys.exit(1)

    infoPlistPath = APP_PATH / "Contents" / "Info.plist"
    with open(infoPlistPath, "rb") as infoFile:
        infoMap = plistlib.load(infoFile)
    infoMap.update({
        "CFBundleIdentifier": AGENT_LABEL,
        "CFBundleName": APP_NAME,
        "LSUIElement": True,   # no Dock icon / menu bar
        "NSDocumentsFolderUsageDescription":
            "WorkTrace keeps your timesheet and calendar sync files in Documents/DevPlatform.",
    })
    with open(infoPlistPath, "wb") as infoFile:
        plistlib.dump(infoMap, infoFile)
    signResult = runCommand("codesign", "--force", "--deep", "--sign", "-", str(APP_PATH))
    if signResult.returncode != 0:
        print(f"✗ codesign failed: {signResult.stderr.strip()}")
        sys.exit(1)


def installAgent(intervalMinutes: int):
    AGENT_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    buildSyncApp()
    agentDefinitionMap = {
        "Label": AGENT_LABEL,
        # -g: don't bring to front, -j: launch hidden.
        "ProgramArguments": ["/usr/bin/open", "-g", "-j", str(APP_PATH)],
        "StartInterval": intervalMinutes * 60,
        "RunAtLoad": True,
        "ProcessType": "Background",
    }
    runCommand("launchctl", "bootout", LAUNCHD_DOMAIN, str(AGENT_PLIST_PATH))  # replace if present
    with open(AGENT_PLIST_PATH, "wb") as plistFile:
        plistlib.dump(agentDefinitionMap, plistFile)
    bootstrapResult = runCommand("launchctl", "bootstrap", LAUNCHD_DOMAIN, str(AGENT_PLIST_PATH))
    if bootstrapResult.returncode != 0:
        print(f"✗ launchctl bootstrap failed: {bootstrapResult.stderr.strip()}")
        sys.exit(1)
    print(f"✓ Calendar auto-sync installed — runs now and every {intervalMinutes} min while the laptop is on.")
    print(f'  First run: macOS asks "{APP_NAME} would like to access files in your Documents folder" → Allow.')
    print(f"  Log: {LOG_PATH}")


def uninstallAgent():
    runCommand("launchctl", "bootout", LAUNCHD_DOMAIN, str(AGENT_PLIST_PATH))
    if AGENT_PLIST_PATH.exists():
        AGENT_PLIST_PATH.unlink()
    if APP_PATH.exists():
        shutil.rmtree(APP_PATH)
    print("✓ Calendar auto-sync removed (agent + app). Its Files and Folders entry can be "
          "cleared in System Settings → Privacy & Security.")


def showStatus():
    printResult = runCommand("launchctl", "print", f"{LAUNCHD_DOMAIN}/{AGENT_LABEL}")
    if printResult.returncode != 0:
        print("Calendar auto-sync: not installed.")
        return
    stateLineList = [line.strip() for line in printResult.stdout.splitlines()
                     if line.strip().startswith(("run interval", "runs ="))]
    print(f"Calendar auto-sync: installed ({APP_PATH})")
    for stateLine in stateLineList:
        print(f"  {stateLine}")
    if LOG_PATH.exists():
        logLineList = LOG_PATH.read_text(errors="replace").splitlines()
        runStartIndexList = [index for index, line in enumerate(logLineList) if line.startswith("=== ")]
        if runStartIndexList:
            print("  last run:")
            for logLine in logLineList[runStartIndexList[-1]:][:25]:
                print(f"    {logLine}")


def main():
    argumentParser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    argumentParser.add_argument("action", choices=["install", "uninstall", "status"])
    argumentParser.add_argument("--minutes", type=int, default=30)
    parsedArguments = argumentParser.parse_args()
    if parsedArguments.action == "install":
        installAgent(max(5, parsedArguments.minutes))
    elif parsedArguments.action == "uninstall":
        uninstallAgent()
    else:
        showStatus()


if __name__ == "__main__":
    main()
