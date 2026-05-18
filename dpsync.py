#!/usr/bin/env python3
"""
dpsync.py — DevPlatform top-level sync orchestrator.

Runs each enabled module's sync.py, validates the output against the
module's JSON Schema, and commits + pushes to the central data repo.

Usage:
    python3 dpsync.py                # full sync: pull → run modules → commit → push
    python3 dpsync.py --dry-run      # run module syncs in dry-run mode; no commits
    python3 dpsync.py --module timesheet     # run only one specific module
    python3 dpsync.py --no-push      # commit but don't push (test mode)

Flow:
    1. Load ~/Documents/DevPlatform/config.json
    2. Acquire flock on ~/Documents/DevPlatform/.dpsync.lock (30s wait, then fail)
    3. cd sync/ && git pull --rebase (auth via PAT, never stored in .git/config)
    4. For each module in config.modules where enabled=true:
         a. Run modules/<module>/sync.py
         b. Validate output JSON against sync/schema/<module>/v<n>.json
    5. If no changes: exit 0 silently
    6. git add → git commit → git push (auth via PAT inline, never stored)
    7. Release flock

Authentication model:
    The PAT lives in config.json (chmod 600). For each git operation that
    needs network, we construct a one-off auth URL of the form
        https://x-access-token:<PAT>@github.com/<owner>/<repo>.git
    pass it on the command line, and never persist it. The repo's stored
    `origin` URL stays clean (no token embedded). macOS git's osxkeychain
    helper is disabled per-command so it can't intercept and reject.

Stdlib only — no external deps. `jsonschema` is used if installed (for
proper schema validation), otherwise we fall back to a minimal required-
fields check so this still works on a vanilla Python install.
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# --- Paths ----------------------------------------------------------------

DP_DIR = Path.home() / "Documents" / "DevPlatform"
CONFIG_PATH = DP_DIR / "config.json"
SYNC_DIR = DP_DIR / "sync"
LOCK_PATH = DP_DIR / ".dpsync.lock"


# --- Logging helpers (write to stderr; stdout is reserved for --dry-run JSON) ---

def log(msg: str):
    print(msg, file=sys.stderr)


def section(title: str):
    log("")
    log(f"━━ {title} ━━")


# --- Config + lock --------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"config.json not found at {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def acquire_lock(timeout_seconds: int = 30):
    """Acquire an exclusive flock on .dpsync.lock; wait up to N seconds."""
    LOCK_PATH.touch(mode=0o600, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    deadline = time.time() + timeout_seconds
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.time() > deadline:
                fh.close()
                raise SystemExit(
                    f"Another dpsync appears to be running (lock held > {timeout_seconds}s). "
                    "Aborting to avoid stomping on it."
                )
            time.sleep(0.5)


# --- Git helpers ----------------------------------------------------------

def _redact(text: str, secret: str) -> str:
    """Replace any occurrence of `secret` in `text` with <redacted>."""
    if not secret:
        return text
    return text.replace(secret, "<redacted>")


def run(cmd, cwd=None, secret: str = "", check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess. If `secret` is given, scrub it from any error output."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        out = _redact(result.stderr or result.stdout or "", secret)
        raise SystemExit(
            f"Command failed (exit {result.returncode}):\n"
            f"  {' '.join(c if 'x-access-token' not in c else '<auth-url-redacted>' for c in cmd)}\n"
            f"  → {out.strip()}"
        )
    return result


def auth_url(plain_https_url: str, token: str) -> str:
    """Convert https://github.com/x/y.git → https://x-access-token:<TOKEN>@github.com/x/y.git"""
    return plain_https_url.replace("https://", f"https://x-access-token:{token}@", 1)


def git_pull(token: str, remote_url: str):
    """Fetch + fast-forward from origin. PAT used only for this command.

    Uses an explicit refspec (`main:refs/remotes/origin/main`) so the local
    `origin/main` tracking ref stays in sync — without this, a subsequent
    `git reset --hard origin/main` would point at a stale local snapshot.
    """
    a = auth_url(remote_url, token)
    # Disable any cached credential helper for this command so osxkeychain
    # can't intercept and feed wrong credentials.
    run(["git", "-c", "credential.helper=", "fetch", a,
         "main:refs/remotes/origin/main"],
        cwd=SYNC_DIR, secret=token)
    run(["git", "merge", "--ff-only", "origin/main"], cwd=SYNC_DIR, secret=token)


def git_push(token: str, remote_url: str):
    a = auth_url(remote_url, token)
    run(["git", "-c", "credential.helper=", "push", a, "main:main"],
        cwd=SYNC_DIR, secret=token)


def has_changes() -> bool:
    """True if there's anything staged or unstaged in sync/."""
    r = run(["git", "status", "--porcelain"], cwd=SYNC_DIR, check=False)
    return bool(r.stdout.strip())


# --- Schema validation ----------------------------------------------------

def validate_against_schema(payload: dict, schema_path: Path):
    """Validate `payload` against the JSON Schema at `schema_path`.

    Uses `jsonschema` if installed (proper full validation). Falls back to a
    minimal check (top-level required keys present) if not — sufficient to
    catch typos/missing fields, less thorough than the real validator.
    """
    schema = json.loads(schema_path.read_text())
    try:
        import jsonschema
        jsonschema.validate(payload, schema)
        return
    except ImportError:
        pass
    # Fallback: shallow required-keys check
    required = schema.get("required", [])
    missing = [k for k in required if k not in payload]
    if missing:
        raise SystemExit(
            f"Schema validation failed (fallback): missing required keys {missing}"
        )


# --- Per-module sync ------------------------------------------------------

def run_module_sync(module_id: str, user_id: str, dry_run: bool):
    """Invoke modules/<module_id>/sync.py and validate the resulting JSON."""
    module_dir = DP_DIR / "modules" / module_id
    sync_script = module_dir / "sync.py"
    if not sync_script.exists():
        log(f"  ⚠ {module_id}: no sync.py at {sync_script} — skipping")
        return False

    cmd = ["python3", str(sync_script)]
    if dry_run:
        cmd.append("--dry-run")

    result = run(cmd, cwd=module_dir)

    # Forward the module's stderr to dpsync's stderr so its progress
    # messages (e.g. "(no-op: source_hash unchanged)" or "✓ wrote ...") are
    # visible to the user. The orchestrator doesn't know whether the module
    # wrote or not — that's the module's call.
    if result.stderr:
        for line in result.stderr.rstrip().splitlines():
            log(f"  {line}")

    if dry_run:
        log(f"\n--- {module_id} (dry-run JSON below) ---")
        # The module's stdout IS its JSON payload. Show it.
        print(result.stdout, end="")
        return False

    # Verify the file landed where we expect.
    # Per-user-repo layout (Phase 5a+): one file per repo at
    # `modules/<module_id>/data.json`. No per-user subdirectory needed since
    # each user's data repo is dedicated to that one user.
    out_path = SYNC_DIR / "modules" / module_id / "data.json"
    if not out_path.exists():
        raise SystemExit(
            f"{module_id}: sync.py exited 0 but no output at {out_path}"
        )
    # Schema validation is performed inside the module's sync.py against
    # the in-process schema. The data repo no longer carries schemas
    # (they moved to worktrace-app/docs/schema/ in Phase 5a); the
    # dashboard does its own validation when rendering. One less file
    # to keep in sync across two repos.
    payload = json.loads(out_path.read_text())
    log(f"  ✓ {module_id}: {len(payload.get('entries', []))} entries on disk")
    return True


# --- Main -----------------------------------------------------------------

def main() -> int:
    argp = argparse.ArgumentParser(
        description="DevPlatform top-level sync orchestrator. "
                    "Runs each enabled module's sync.py, validates the output, "
                    "commits + pushes to the central data repo."
    )
    argp.add_argument("--dry-run", action="store_true",
                     help="Run module syncs in dry-run mode (print JSON, no writes, no commits, no push).")
    argp.add_argument("--module",
                     help="Run only one specific module (default: all enabled).")
    argp.add_argument("--no-push", action="store_true",
                     help="Run sync + commit but don't push to origin.")
    args = argp.parse_args()

    config = load_config()
    platform = config["platform"]
    user_id = platform["user_id"]
    token = platform.get("github_token", "")
    remote_url = platform.get("remote_data_repo", "")

    # Admin / role-less accounts have no personal data repo — they manage
    # other users via the dashboard but don't produce timesheet data of
    # their own. Detect this and exit cleanly (rather than failing) so
    # the auto-sync hook in CLAUDE.md can run dpsync unconditionally on
    # every laptop without special-casing roles.
    if not remote_url:
        log(f"dpsync — no remote_data_repo configured for user='{user_id}'; "
            f"nothing to push (typical for admin accounts).")
        return 0

    if not args.dry_run:
        if not token:
            raise SystemExit("No platform.github_token in config.json. Run dpsetup first.")
        if not SYNC_DIR.exists() or not (SYNC_DIR / ".git").exists():
            raise SystemExit(
                f"sync/ clone not found at {SYNC_DIR}. "
                "Run `git clone <remote> sync` inside ~/Documents/DevPlatform first."
            )

    enabled = [m for m, c in config.get("modules", {}).items() if c.get("enabled")]
    if args.module:
        if args.module not in enabled:
            raise SystemExit(
                f"Module '{args.module}' not in enabled list: {enabled}"
            )
        enabled = [args.module]
    if not enabled:
        log("No enabled modules — nothing to sync.")
        return 0

    log(f"dpsync — user={user_id}, modules={enabled}, "
        f"dry_run={args.dry_run}, no_push={args.no_push}")

    lock = acquire_lock()
    try:
        if not args.dry_run:
            section("Pulling latest from origin")
            git_pull(token, remote_url)
            log("  ✓ fast-forward complete")

        section("Running module sync scripts")
        for module_id in enabled:
            run_module_sync(module_id, user_id, args.dry_run)

        if args.dry_run:
            section("Dry run complete (no commits, no push)")
            return 0

        if not has_changes():
            section("No changes to commit (timesheet unchanged since last sync)")
            return 0

        section("Committing changes")
        run(["git", "add", "-A"], cwd=SYNC_DIR)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commit_msg = f"Sync [{user_id}] @ {ts}"
        run(["git", "commit", "-m", commit_msg], cwd=SYNC_DIR)
        log(f"  ✓ committed: {commit_msg}")

        if args.no_push:
            section("--no-push set — leaving commit local, not pushing")
            return 0

        section("Pushing to origin")
        git_push(token, remote_url)
        log(f"  ✓ pushed → {remote_url.rsplit('/', 1)[-1].replace('.git', '')}")

        section("Done")
        log(f"  Dashboard: {platform.get('dashboard_url', '(not configured)')}")
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
