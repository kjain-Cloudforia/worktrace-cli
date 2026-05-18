"""
modules/timesheet/sync.py — Timesheet module sync layer.

Reads:
    ~/Documents/DevPlatform/config.json           (platform + module config)
    ~/Documents/DevPlatform/modules/timesheet/timesheet.md  (canonical, private)

Writes (or prints on --dry-run):
    ~/Documents/DevPlatform/sync/modules/timesheet/data.json
    (Each user's data repo holds only their own data — no per-user subdirectory needed.)

The output JSON conforms to ~/Documents/DevPlatform/sync/schema/timesheet/v1.json:
- schema_version, user_id, display_name, timezone, work_shift
- last_synced_at (now), source_hash (sha256 of timesheet.md)
- entries[] each with: work_date, project, headline, bullets[], counts{}, tags[], entry_hash

This script is invoked by the top-level `dpsync.py` orchestrator. It can also
run standalone for testing — `python3 sync.py --dry-run` prints the JSON
that would be written, without touching disk or git.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Local import — parser lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from parser import parse_timesheet


# --- Paths ----------------------------------------------------------------

DP_DIR = Path.home() / "Documents" / "DevPlatform"
CONFIG_PATH = DP_DIR / "config.json"
TIMESHEET_PATH = DP_DIR / "modules" / "timesheet" / "timesheet.md"
SYNC_DIR = DP_DIR / "sync"
OUTPUT_DIR = SYNC_DIR / "modules" / "timesheet"


# --- Heuristic patterns for counts + tags ---------------------------------
# These look at bullet text to derive {creates, modifications, deploys, etc.}.
# Phase 1 keeps them simple regex-based; Phase 2 may swap in LLM-derived tags.

RE_CREATE = re.compile(
    r"\*\*(Created|Built|New |Added a new |Bulk-loaded|Provisioned\s+\w+\s+(new\s+)?user)",
    re.IGNORECASE,
)
RE_MODIFY = re.compile(
    r"\*\*(Updated|Modified|Improved|Enhanced|Major rework|Major improvement|"
    r"Big overhaul|Refactored|Promoted|Added .*? to (the |an )?existing|"
    r"Made .*? dynamic|Added phone-number validation|Added auto-copy|Removed)",
    re.IGNORECASE,
)
RE_DEPLOY = re.compile(
    r"\b(deploy|deployed|deploys|promoted|synced)\b",
    re.IGNORECASE,
)
RE_INVESTIGATION = re.compile(
    r"\*\*(Wrote an investigation|Investigated|Diagnosed|Sales-manager .*? investigation|"
    r"Investigation:|Walked .* through)",
    re.IGNORECASE,
)
RE_FIX = re.compile(r"\*\*(Fixed|Bug)", re.IGNORECASE)
RE_USERS_NUM = re.compile(r"\b(\d+)\s+(?:more\s+)?users?\b", re.IGNORECASE)


# Tag heuristics: applied across the joined bullet text for the entry.
TAG_PATTERNS = [
    ("onboarding",   re.compile(r"\b(onboarded|provisioned|access package|permission set licenses)\b", re.IGNORECASE)),
    ("admin",        re.compile(r"\b(permission set|granted access|admin action|setup)\b", re.IGNORECASE)),
    ("code",         re.compile(r"\b(deploy|automation|component|pdf|screen|button|flow|picklist|field)\b", re.IGNORECASE)),
    ("fix",          re.compile(r"\b(fixed|error|bug)\b", re.IGNORECASE)),
    ("investigation",re.compile(r"\b(investigated|investigation|diagnosed|drift)\b", re.IGNORECASE)),
    ("docs",         re.compile(r"\b(wrote a change-log|wrote an investigation|change-log document)\b", re.IGNORECASE)),
    ("schema",       re.compile(r"\b(picklist|new field|new .*? field|custom field|global picklist)\b", re.IGNORECASE)),
]


# --- Helpers --------------------------------------------------------------

def _strip_md_bold(text: str) -> str:
    """Remove **bold** markers without breaking the inner text."""
    return re.sub(r"\*\*(.*?)\*\*", r"\1", text)


def first_sentence(text: str) -> str:
    """Pull the first sentence out of a bullet's text, markdown-stripped."""
    text = text.strip()
    text = _strip_md_bold(text)
    # Match up to and including the first ., !, or ? that's followed by whitespace
    # or end-of-string. Avoid stopping on abbreviations by also requiring a
    # capital letter or end-of-string after the punctuation (best-effort).
    match = re.search(r"^(.+?[.?!])(\s|$)", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # No sentence terminator found — return the first chunk truncated.
    return text[:200].strip()


def make_headline(bullets: list, max_chars: int = 500) -> str:
    """
    Phase 1 headline policy: take the first sentence of each bullet and join
    with ' · ', then truncate. Phase 2 can swap in LLM-summarized headlines.
    """
    sentences = []
    for b in bullets:
        s = first_sentence(b)
        if s:
            sentences.append(s)
    headline = " · ".join(sentences)
    if len(headline) > max_chars:
        headline = headline[: max_chars - 1].rstrip() + "…"
    return headline or "(no headline)"


def count_matching(bullets: list, pattern: "re.Pattern") -> int:
    """Number of bullets whose text matches `pattern` anywhere."""
    return sum(1 for b in bullets if pattern.search(b))


def derive_counts(bullets: list) -> dict:
    """Heuristic counts derived from bullet text."""
    counts = {
        "bullets":        len(bullets),
        "creates":        count_matching(bullets, RE_CREATE),
        "modifications":  count_matching(bullets, RE_MODIFY),
        "deploys":        count_matching(bullets, RE_DEPLOY),
        "investigations": count_matching(bullets, RE_INVESTIGATION),
        "fixes":          count_matching(bullets, RE_FIX),
    }
    # Sum up "N users" numbers across the bullets (handy for onboarding days).
    text = "\n".join(bullets)
    user_nums = [int(n) for n in RE_USERS_NUM.findall(text)]
    if user_nums:
        # The same bullet might have multiple "N users" references; sum them
        # but cap to a sane value to avoid double-counting noise.
        counts["users_provisioned"] = min(sum(user_nums), 999)
    return counts


def derive_tags(bullets: list) -> list:
    """Free-form tag list derived from bullet text."""
    text = "\n".join(bullets).lower()
    return [tag for tag, pat in TAG_PATTERNS if pat.search(text)]


def entry_hash(work_date: str, company_name: str, headline: str) -> str:
    """Stable idempotency key per entry."""
    h = hashlib.sha256()
    h.update(work_date.encode("utf-8"))
    h.update(b"\x00")
    h.update(company_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(headline.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def source_hash(content: str) -> str:
    """Stable sha256 of the raw timesheet content (for no-op detection)."""
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# --- Payload assembly -----------------------------------------------------

def build_payload(config: dict, content: str) -> dict:
    """Parse timesheet.md and assemble the JSON payload."""
    platform = config["platform"]
    ts_module = config["modules"]["timesheet"]

    parsed = parse_timesheet(content)
    entries = []
    for pe in parsed:
        bullets = pe["bullets"]
        project = pe["project"]
        # Schema requires `company_name`; `friendly_name` is optional.
        project_out = {"company_name": project["company_name"]}
        if project.get("friendly_name"):
            project_out["friendly_name"] = project["friendly_name"]

        headline = make_headline(bullets)
        entry = {
            "work_date": pe["work_date"],
            "project":   project_out,
            "headline":  headline,
            "bullets":   bullets,
            "counts":    derive_counts(bullets),
            "tags":      derive_tags(bullets),
        }
        entry["entry_hash"] = entry_hash(
            entry["work_date"], project["company_name"], headline
        )
        entries.append(entry)

    payload = {
        "schema_version": 1,
        "user_id":        platform["user_id"],
        "display_name":   platform.get("display_name", ""),
        "timezone":       platform.get("timezone", ""),
        "work_shift": {
            "start": ts_module["work_hours"]["start"],
            "end":   ts_module["work_hours"]["end"],
        },
        "last_synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_hash":    source_hash(content),
        "entries":        entries,
    }
    return payload


# --- CLI ------------------------------------------------------------------

def main() -> int:
    parser_cli = argparse.ArgumentParser(
        description="Sync the Timesheet module — read timesheet.md, "
                    "produce schema-conforming JSON, write into sync/."
    )
    parser_cli.add_argument(
        "--dry-run", action="store_true",
        help="Print the JSON to stdout instead of writing to disk."
    )
    args = parser_cli.parse_args()

    if not CONFIG_PATH.exists():
        print(f"ERROR: config.json not found at {CONFIG_PATH}", file=sys.stderr)
        return 2

    config = json.loads(CONFIG_PATH.read_text())
    if not config.get("modules", {}).get("timesheet", {}).get("enabled"):
        print("Timesheet module disabled in config.json — nothing to do.",
              file=sys.stderr)
        return 0

    if not TIMESHEET_PATH.exists():
        print(f"ERROR: timesheet.md not found at {TIMESHEET_PATH}", file=sys.stderr)
        return 2

    content = TIMESHEET_PATH.read_text()
    payload = build_payload(config, content)

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "data.json"

    # No-op detection: if an existing JSON has the same source_hash, the
    # underlying timesheet.md hasn't changed since last sync. Skip the write
    # entirely so dpsync.py's `git status` sees no diff and doesn't make
    # spurious commits. The `last_synced_at` timestamp would otherwise
    # change on every run and cause noise.
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            if existing.get("source_hash") == payload["source_hash"]:
                print(f"  (no-op: source_hash unchanged — skipping write of "
                      f"{out_path.relative_to(DP_DIR)})", file=sys.stderr)
                return 0
        except (json.JSONDecodeError, KeyError):
            # Corrupt or schemaless existing file — fall through and overwrite.
            pass

    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"✓ wrote {out_path.relative_to(DP_DIR)} "
          f"({len(payload['entries'])} entries)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
