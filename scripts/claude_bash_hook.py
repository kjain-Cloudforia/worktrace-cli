#!/usr/bin/env python3
"""
Claude Code PostToolUse hook for the Bash tool.

Mirrors the logic in shell-hook.zsh for the non-interactive shell that Claude
Code's Bash tool uses. The shell hook only fires for interactive zsh sessions,
so anything Claude runs via its Bash tool was previously invisible to
cli-log.jsonl. This hook closes that gap.

Registered in ~/.claude/settings.json under hooks.PostToolUse with
matcher='Bash'. Reads the same config.json (cli_tracking.patterns) the shell
hook reads, so the two stay in sync — only the trigger surface differs.

Receives a JSON payload on stdin with shape:
  {
    "tool_name": "Bash",
    "tool_input": {"command": "...", "description": "..."},
    "tool_response": {"interrupted": bool, ...},
    "cwd": "...",
    ...
  }

Appends one JSON line per tracked invocation to cli-log.jsonl in the same
format the shell hook uses, plus a "source" field so we can distinguish hook
sources during debugging.

Never fails loudly — silent return on any error so the user's tool flow is
never disrupted. Exit code is always 0 from the hook's perspective.
"""
import sys, json, fnmatch
from pathlib import Path
from datetime import datetime, timezone

DP_DIR = Path.home() / "Documents" / "DevPlatform"
CONFIG = DP_DIR / "config.json"
CLI_LOG = DP_DIR / "cli-log.jsonl"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    if payload.get("tool_name") != "Bash":
        return

    toolInput = payload.get("tool_input", {})
    commandLine = toolInput.get("command", "")
    if not commandLine or not commandLine.lstrip().startswith("sf"):
        return

    try:
        configDict = json.loads(CONFIG.read_text())
    except Exception:
        return

    trackingConfig = configDict.get("cli_tracking", {})
    if not trackingConfig.get("enabled", False):
        return

    patternList = trackingConfig.get("patterns", [])
    if not any(fnmatch.fnmatch(commandLine, p) for p in patternList):
        return

    workingDirectory = payload.get("cwd", "") or ""
    overrideMap = configDict.get("project_detection", {}).get("overrides", {}) or {}
    projectName = overrideMap.get(workingDirectory) or (
        Path(workingDirectory).name if workingDirectory else "?"
    )

    toolResponse = payload.get("tool_response", {})
    isErrorFlag = bool(
        toolResponse.get("isError", False) or toolResponse.get("interrupted", False)
    )
    exitCode = 1 if isErrorFlag else 0

    entryDict = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": projectName,
        "cwd": workingDirectory,
        "cmd": commandLine,
        "exit_code": exitCode,
        "duration_sec": -1,  # not available from the PostToolUse payload
        "source": "claude-bash-hook",
    }

    try:
        with open(CLI_LOG, "a") as logFileHandle:
            logFileHandle.write(json.dumps(entryDict) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
