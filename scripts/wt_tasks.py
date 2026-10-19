#!/usr/bin/env python3
"""
wt_tasks.py — laptop-side reader/writer for the WorkTrace Task Board module.

The Task Board lives at modules/tasks/data.json in the user's private data
repo. The dashboard edits it in the browser; this script lets Claude (or
you) do the same from the terminal. It talks to the GitHub contents API
directly — NOT the local sync/ clone — and every write is a compare-and-swap
on the blob SHA: read → apply edit → PUT with that SHA, retried on conflict.
So a write here never clobbers a dashboard edit, and vice-versa (the
dashboard re-reads fresh before each save too).

Usage:
    python3 wt_tasks.py list [--board GEN] [--all]      # open tasks (all = include done)
    python3 wt_tasks.py show GEN-12
    python3 wt_tasks.py boards                          # boards + their columns
    python3 wt_tasks.py add GEN "Title" [--desc "..."] [--column "In Progress"]
    python3 wt_tasks.py move GEN-12 "Done"              # column name (case-insensitive) or "done"
    python3 wt_tasks.py edit GEN-12 [--title "..."] [--desc "..."]
    python3 wt_tasks.py delete GEN-12
    python3 wt_tasks.py seed                            # one board per config.json project + Internal
    python3 wt_tasks.py add-board "Name" [--key ABC]    # board for a project not in config.json
    python3 wt_tasks.py import tasks.json               # bulk add, one write, skips duplicate titles
    python3 wt_tasks.py summary                         # one-line-per-board open counts (shift notice)

A board can be named by key (GEN), name (Genserve) or project, any case.
"""

import argparse
import base64
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DP_DIR = Path.home() / "Documents" / "DevPlatform"
CONFIG_PATH = DP_DIR / "config.json"
DATA_PATH_IN_REPO = "modules/tasks/data.json"
GITHUB_API = "https://api.github.com"
INTERNAL_BOARD_NAME = "Internal"


class TaskBoardError(Exception):
    pass


# ---------------------------------------------------------------- config + API

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise TaskBoardError(f"No config at {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def data_repo_and_token(config: dict):
    platformConfig = config.get("platform", {})
    remoteUrl = platformConfig.get("remote_data_repo") or ""
    repoMatch = re.search(r"github\.com[/:]([^/]+/[^/.]+?)(?:\.git)?$", remoteUrl)
    if not repoMatch:
        raise TaskBoardError("config.json platform.remote_data_repo is missing — no data repo to write to.")
    githubToken = platformConfig.get("github_token")
    if not githubToken:
        raise TaskBoardError("config.json platform.github_token is missing.")
    return repoMatch.group(1), githubToken


def github_request(method: str, url: str, githubToken: str, body: dict = None):
    requestHeaderMap = {
        "Authorization": f"Bearer {githubToken}",
        "Accept": "application/vnd.github+json",
        "Cache-Control": "no-cache",
        "User-Agent": "worktrace-cli-wt_tasks",
    }
    payloadBytes = None
    if body is not None:
        payloadBytes = json.dumps(body).encode()
        requestHeaderMap["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payloadBytes, method=method, headers=requestHeaderMap)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as httpError:
        errorBodyText = httpError.read().decode(errors="replace")
        return httpError.code, {"message": errorBodyText[:300]}


def empty_model(config: dict) -> dict:
    platformConfig = config.get("platform", {})
    return {
        "schema_version": 1,
        "user_id": platformConfig.get("user_id", "unknown"),
        "display_name": platformConfig.get("display_name", ""),
        "updated_at": now_iso(),
        "boards": [],
    }


def read_board_file(config: dict):
    """Return (dataModel, blobSha or None)."""
    dataRepo, githubToken = data_repo_and_token(config)
    url = f"{GITHUB_API}/repos/{dataRepo}/contents/{DATA_PATH_IN_REPO}?ref=main&_={int(time.time() * 1000)}"
    statusCode, responseBody = github_request("GET", url, githubToken)
    if statusCode == 404:
        return empty_model(config), None
    if statusCode != 200:
        raise TaskBoardError(f"GitHub read failed: HTTP {statusCode} {responseBody.get('message', '')}")
    dataModel = json.loads(base64.b64decode(responseBody["content"]).decode())
    return normalize_model(dataModel), responseBody["sha"]


def apply_and_save(config: dict, mutationFn, commitMessage: str, maxAttemptCount: int = 5):
    """Compare-and-swap loop. mutationFn(dataModel) edits in place and returns
    a result value (shown to the user); it is re-run on the fresh copy after
    any conflict, so it must be id/key-based."""
    dataRepo, githubToken = data_repo_and_token(config)
    url = f"{GITHUB_API}/repos/{dataRepo}/contents/{DATA_PATH_IN_REPO}"
    for attemptNumber in range(1, maxAttemptCount + 1):
        dataModel, blobSha = read_board_file(config)
        mutationResult = mutationFn(dataModel)
        dataModel["updated_at"] = now_iso()
        contentText = json.dumps(dataModel, indent=2, ensure_ascii=False) + "\n"
        requestBody = {
            "message": commitMessage,
            "content": base64.b64encode(contentText.encode()).decode(),
            "branch": "main",
        }
        if blobSha:
            requestBody["sha"] = blobSha
        statusCode, responseBody = github_request("PUT", url, githubToken, requestBody)
        if statusCode in (200, 201):
            return mutationResult
        if statusCode in (409, 422) and attemptNumber < maxAttemptCount:
            time.sleep(0.6 * attemptNumber)   # someone else wrote in between — re-read and retry
            continue
        raise TaskBoardError(f"GitHub write failed: HTTP {statusCode} {responseBody.get('message', '')}")
    raise TaskBoardError("Gave up after repeated write conflicts.")


# ---------------------------------------------------------------- model helpers

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def default_column_list() -> list:
    return [
        {"id": "col_todo", "name": "To Do"},
        {"id": "col_inprogress", "name": "In Progress"},
        {"id": "col_blocked", "name": "Blocked"},
        {"id": "col_done", "name": "Done", "is_done": True},
    ]


def normalize_model(dataModel: dict) -> dict:
    dataModel.setdefault("boards", [])
    for board in dataModel["boards"]:
        if not board.get("columns"):
            board["columns"] = default_column_list()
        board.setdefault("tasks", [])
        if not isinstance(board.get("next_task_number"), int):
            board["next_task_number"] = len(board["tasks"]) + 1
    return dataModel


def suggest_board_key(boardName: str, takenKeySet: set) -> str:
    # Split on spaces/punctuation AND camelCase ("MountainWest" → MW).
    spacedName = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", boardName or "")
    wordList = [word for word in re.sub(r"[^A-Za-z0-9]", " ", spacedName).upper().split() if word]
    baseKey = "".join(word[0] for word in wordList)[:5] if len(wordList) > 1 else (wordList[0] if wordList else "TASK")[:3]
    if not re.match(r"[A-Z]", baseKey):
        baseKey = "T" + baseKey
    baseKey = (baseKey + "X")[:2] if len(baseKey) < 2 else baseKey
    candidateKey, suffixNumber = baseKey, 2
    while candidateKey in takenKeySet:
        candidateKey = f"{baseKey}{suffixNumber}"
        suffixNumber += 1
    return candidateKey


def done_column_id_set(board: dict) -> set:
    return {column["id"] for column in board["columns"] if column.get("is_done")}


def open_task_list(board: dict) -> list:
    doneIdSet = done_column_id_set(board)
    return [task for task in board["tasks"] if task["column_id"] not in doneIdSet]


def resolve_board(dataModel: dict, boardReference: str) -> dict:
    lookupValue = (boardReference or "").strip().lower()
    for board in dataModel["boards"]:
        if lookupValue in {board["key"].lower(), board["name"].lower(), (board.get("project") or "").lower()}:
            return board
    boardNameList = ", ".join(f"{board['name']} ({board['key']})" for board in dataModel["boards"]) or "none"
    raise TaskBoardError(f"No board '{boardReference}'. Boards: {boardNameList}")


def resolve_task(dataModel: dict, taskReference: str):
    lookupValue = (taskReference or "").strip().lower()
    for board in dataModel["boards"]:
        for task in board["tasks"]:
            if lookupValue in {task["key"].lower(), task["id"].lower()}:
                return board, task
    raise TaskBoardError(f"No task '{taskReference}'.")


def resolve_column(board: dict, columnReference: str) -> dict:
    lookupValue = (columnReference or "").strip().lower()
    for column in board["columns"]:
        if column["name"].lower() == lookupValue:
            return column
    if lookupValue in {"done", "complete", "completed", "finished", "closed"}:
        doneColumnList = [column for column in board["columns"] if column.get("is_done")]
        if doneColumnList:
            return doneColumnList[0]
    prefixMatchList = [column for column in board["columns"] if column["name"].lower().startswith(lookupValue)]
    if len(prefixMatchList) == 1:
        return prefixMatchList[0]
    columnNameList = ", ".join(column["name"] for column in board["columns"])
    raise TaskBoardError(f"No column '{columnReference}' on {board['name']}. Columns: {columnNameList}")


def column_name(board: dict, columnId: str) -> str:
    return next((column["name"] for column in board["columns"] if column["id"] == columnId), "?")


def set_task_column(board: dict, task: dict, targetColumn: dict):
    wasDone = task["column_id"] in done_column_id_set(board)
    task["column_id"] = targetColumn["id"]
    if targetColumn.get("is_done") and not wasDone:
        task["completed_at"] = now_iso()
    if not targetColumn.get("is_done"):
        task.pop("completed_at", None)
    task["updated_at"] = now_iso()


# ---------------------------------------------------------------- commands

def command_list(config: dict, arguments):
    dataModel, _ = read_board_file(config)
    boardList = [resolve_board(dataModel, arguments.board)] if arguments.board else \
        [board for board in dataModel["boards"] if not board.get("archived")]
    if not boardList:
        print("No boards yet. Run: python3 wt_tasks.py seed")
        return
    for board in boardList:
        shownTaskList = board["tasks"] if arguments.all else open_task_list(board)
        print(f"## {board['name']} ({board['key']}) — {len(open_task_list(board))} open")
        for column in board["columns"]:
            columnTaskList = [task for task in shownTaskList if task["column_id"] == column["id"]]
            for task in columnTaskList:
                print(f"  [{column['name']}] {task['key']}  {task['title']}")
        if not shownTaskList:
            print("  (nothing open)")


def command_show(config: dict, arguments):
    dataModel, _ = read_board_file(config)
    board, task = resolve_task(dataModel, arguments.task)
    print(f"{task['key']}  {task['title']}")
    print(f"Board: {board['name']} · Status: {column_name(board, task['column_id'])}")
    print(f"Created {task.get('created_at', '?')} by {task.get('created_by', '?')}"
          + (f" · done {task['completed_at']}" if task.get("completed_at") else ""))
    if task.get("description"):
        print("\n" + task["description"])


def command_boards(config: dict, _arguments):
    dataModel, _ = read_board_file(config)
    for board in dataModel["boards"]:
        columnLabelList = [column["name"] + (" ✓" if column.get("is_done") else "") for column in board["columns"]]
        archivedLabel = " [archived]" if board.get("archived") else ""
        print(f"{board['key']:<6} {board['name']}{archivedLabel} — {len(open_task_list(board))} open "
              f"· columns: {' → '.join(columnLabelList)}")


def command_add(config: dict, arguments):
    newTaskId = make_id("tsk")

    def mutation(dataModel):
        board = resolve_board(dataModel, arguments.board)
        existingTask = next((task for task in board["tasks"] if task["id"] == newTaskId), None)
        if existingTask:
            return board, existingTask
        targetColumn = resolve_column(board, arguments.column) if arguments.column else board["columns"][0]
        timestamp = now_iso()
        newTask = {
            "id": newTaskId,
            "key": f"{board['key']}-{board['next_task_number']}",
            "title": arguments.title.strip(),
            "description": (arguments.desc or "").strip(),
            "column_id": targetColumn["id"],
            "created_at": timestamp,
            "updated_at": timestamp,
            "created_by": "claude",
        }
        if targetColumn.get("is_done"):
            newTask["completed_at"] = timestamp
        board["next_task_number"] += 1
        board["tasks"].append(newTask)
        return board, newTask

    board, newTask = apply_and_save(config, mutation, f"Add task to \"{arguments.board}\": {arguments.title[:60]}")
    print(f"Added {newTask['key']} \"{newTask['title']}\" to {board['name']} → {column_name(board, newTask['column_id'])}")


def command_move(config: dict, arguments):
    def mutation(dataModel):
        board, task = resolve_task(dataModel, arguments.task)
        targetColumn = resolve_column(board, arguments.column)
        previousColumnName = column_name(board, task["column_id"])
        if task["column_id"] != targetColumn["id"]:
            # Moved cards go to the bottom of their new column.
            board["tasks"].remove(task)
            set_task_column(board, task, targetColumn)
            board["tasks"].append(task)
        return task, previousColumnName, targetColumn["name"]

    task, previousColumnName, targetColumnName = apply_and_save(
        config, mutation, f"Move {arguments.task.upper()} to {arguments.column}")
    print(f"Moved {task['key']} \"{task['title']}\": {previousColumnName} → {targetColumnName}")


def command_edit(config: dict, arguments):
    if arguments.title is None and arguments.desc is None:
        raise TaskBoardError("Nothing to change — pass --title and/or --desc.")

    def mutation(dataModel):
        _board, task = resolve_task(dataModel, arguments.task)
        if arguments.title is not None:
            task["title"] = arguments.title.strip()
        if arguments.desc is not None:
            task["description"] = arguments.desc.strip()
        task["updated_at"] = now_iso()
        return task

    task = apply_and_save(config, mutation, f"Edit task {arguments.task.upper()}")
    print(f"Updated {task['key']} \"{task['title']}\"")


def command_delete(config: dict, arguments):
    def mutation(dataModel):
        board, task = resolve_task(dataModel, arguments.task)
        board["tasks"].remove(task)
        return task

    task = apply_and_save(config, mutation, f"Delete task {arguments.task.upper()}")
    print(f"Deleted {task['key']} \"{task['title']}\"")


def configured_project_name_list(config: dict) -> list:
    companyVsProjectMap = config.get("project_detection", {}).get("company_to_project", {}) or {}
    projectNameList = []
    for projectName in companyVsProjectMap.values():
        if projectName and projectName not in projectNameList:
            projectNameList.append(projectName)
    return projectNameList + [INTERNAL_BOARD_NAME]


def command_seed(config: dict, _arguments):
    projectNameList = configured_project_name_list(config)

    def mutation(dataModel):
        createdBoardList = []
        for projectName in projectNameList:
            lookupValue = projectName.lower()
            if any(lookupValue in {board["name"].lower(), (board.get("project") or "").lower()}
                   for board in dataModel["boards"]):
                continue
            takenKeySet = {board["key"] for board in dataModel["boards"]}
            newBoard = {
                "id": make_id("brd"),
                "name": projectName,
                "key": suggest_board_key(projectName, takenKeySet),
                "project": None if projectName == INTERNAL_BOARD_NAME else projectName,
                "source": "auto",
                "archived": False,
                "created_at": now_iso(),
                "columns": default_column_list(),
                "next_task_number": 1,
                "tasks": [],
            }
            dataModel["boards"].append(newBoard)
            createdBoardList.append(newBoard)
        return createdBoardList

    # Skip the write entirely if every project already has a board.
    dataModel, _ = read_board_file(config)
    existingNameSet = {board["name"].lower() for board in dataModel["boards"]} | \
        {(board.get("project") or "").lower() for board in dataModel["boards"]}
    if all(projectName.lower() in existingNameSet for projectName in projectNameList):
        print("All project boards already exist.")
        return
    createdBoardList = apply_and_save(config, mutation, "Seed task boards from config.json projects")
    for board in createdBoardList:
        print(f"Created board {board['name']} ({board['key']})")


def command_add_board(config: dict, arguments):
    def mutation(dataModel):
        boardName = arguments.name.strip()
        existingBoard = next((board for board in dataModel["boards"] if board["name"].lower() == boardName.lower()), None)
        if existingBoard:
            return existingBoard, False
        takenKeySet = {board["key"] for board in dataModel["boards"]}
        boardKey = (arguments.key or "").upper() or suggest_board_key(boardName, takenKeySet)
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", boardKey) or boardKey in takenKeySet:
            raise TaskBoardError(f"Key '{boardKey}' is invalid or already used.")
        newBoard = {
            "id": make_id("brd"), "name": boardName, "key": boardKey, "project": None,
            "source": "manual", "archived": False, "created_at": now_iso(),
            "columns": default_column_list(), "next_task_number": 1, "tasks": [],
        }
        dataModel["boards"].append(newBoard)
        return newBoard, True

    board, wasCreated = apply_and_save(config, mutation, f"Add board \"{arguments.name}\"")
    print(f"{'Created' if wasCreated else 'Already exists:'} board {board['name']} ({board['key']})")


def command_import(config: dict, arguments):
    """Bulk-add tasks from a JSON list in ONE write. Each item:
    {"board", "title", "desc"?, "column"?, "completed_on"? (YYYY-MM-DD)}.
    A title already on that board (any case) is skipped, so re-running is safe."""
    importItemList = json.loads(Path(arguments.file).read_text())
    preparedItemList = [dict(importItem, task_id=make_id("tsk")) for importItem in importItemList]

    def mutation(dataModel):
        addedCount, skippedTitleList = 0, []
        for importItem in preparedItemList:
            board = resolve_board(dataModel, importItem["board"])
            if any(task["title"].lower() == importItem["title"].strip().lower() for task in board["tasks"]):
                skippedTitleList.append(f"{board['key']}: {importItem['title']}")
                continue
            targetColumn = resolve_column(board, importItem["column"]) if importItem.get("column") else board["columns"][0]
            timestamp = now_iso()
            newTask = {
                "id": importItem["task_id"],
                "key": f"{board['key']}-{board['next_task_number']}",
                "title": importItem["title"].strip(),
                "description": (importItem.get("desc") or "").strip(),
                "column_id": targetColumn["id"],
                "created_at": timestamp,
                "updated_at": timestamp,
                "created_by": "claude",
            }
            if targetColumn.get("is_done"):
                completedOn = importItem.get("completed_on")
                newTask["completed_at"] = f"{completedOn}T12:00:00.000Z" if completedOn else timestamp
            board["next_task_number"] += 1
            board["tasks"].append(newTask)
            addedCount += 1
        return addedCount, skippedTitleList

    addedCount, skippedTitleList = apply_and_save(
        config, mutation, f"Import {len(preparedItemList)} task(s) from {Path(arguments.file).name}")
    print(f"Imported {addedCount} task(s); skipped {len(skippedTitleList)} already on their board.")
    for skippedTitle in skippedTitleList:
        print(f"  skipped {skippedTitle}")


def command_summary(config: dict, _arguments):
    """Compact open-task digest for the shift-start notice. Silent when empty."""
    dataModel, _ = read_board_file(config)
    for board in dataModel["boards"]:
        if board.get("archived"):
            continue
        openTaskList = open_task_list(board)
        if not openTaskList:
            continue
        columnCountPartList = []
        for column in board["columns"]:
            columnTaskCount = sum(1 for task in openTaskList if task["column_id"] == column["id"])
            if columnTaskCount:
                columnCountPartList.append(f"{columnTaskCount} {column['name']}")
        print(f"{board['name']} ({board['key']}): {len(openTaskList)} open — {', '.join(columnCountPartList)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkTrace Task Board CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    listParser = subparsers.add_parser("list", help="List open tasks")
    listParser.add_argument("--board")
    listParser.add_argument("--all", action="store_true", help="Include tasks in Done columns")
    showParser = subparsers.add_parser("show", help="Show one task")
    showParser.add_argument("task")
    subparsers.add_parser("boards", help="List boards and their columns")
    addParser = subparsers.add_parser("add", help="Add a task")
    addParser.add_argument("board")
    addParser.add_argument("title")
    addParser.add_argument("--desc")
    addParser.add_argument("--column")
    moveParser = subparsers.add_parser("move", help="Move a task to a column")
    moveParser.add_argument("task")
    moveParser.add_argument("column")
    editParser = subparsers.add_parser("edit", help="Edit a task's title/description")
    editParser.add_argument("task")
    editParser.add_argument("--title")
    editParser.add_argument("--desc")
    deleteParser = subparsers.add_parser("delete", help="Delete a task")
    deleteParser.add_argument("task")
    subparsers.add_parser("seed", help="Create a board per config.json project + Internal")
    addBoardParser = subparsers.add_parser("add-board", help="Create a board by hand")
    addBoardParser.add_argument("name")
    addBoardParser.add_argument("--key")
    importParser = subparsers.add_parser("import", help="Bulk-add tasks from a JSON file (one write)")
    importParser.add_argument("file")
    subparsers.add_parser("summary", help="Open-task digest (shift-start notice)")

    arguments = parser.parse_args()
    commandVsHandlerMap = {
        "list": command_list, "show": command_show, "boards": command_boards, "add": command_add,
        "move": command_move, "edit": command_edit, "delete": command_delete,
        "seed": command_seed, "summary": command_summary,
        "add-board": command_add_board, "import": command_import,
    }
    try:
        commandVsHandlerMap[arguments.command](load_config(), arguments)
    except TaskBoardError as taskBoardError:
        print(f"wt_tasks: {taskBoardError}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
