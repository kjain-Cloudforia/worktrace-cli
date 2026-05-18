"""
modules/timesheet/parser.py — Parse timesheet.md into structured entries.

Input: the contents of ~/Documents/DevPlatform/modules/timesheet/timesheet.md
       (or any markdown with the same structure).

Output: a list of entry dicts, one per (work_date, project) pair:
    {
        "work_date": "2026-05-11",                       # IST work-day date
        "project": {
            "company_name": "GenServe, LLC",             # always present
            "friendly_name": "Genserve",                 # None if no parens in header
        },
        "bullets": [
            "**Onboarded 11 users to preprod.** For every user: ...",
            "**Added a 'bundle rollup' feature ...**",
            ...
        ],
    }

Pure function, stdlib only (no external deps). All I/O happens at the call
site (`sync.py`). Easy to unit-test by feeding markdown strings directly.

Expected timesheet.md format:
    # <Title>
    > <Optional blockquote intro>
    ---
    ## Week of YYYY-MM-DD
    ### Day N — Mon YYYY-MM-DD (14:00 IST) → Tue YYYY-MM-DD (05:00 IST)
    **Project: Company Name (FriendlyName)**
    - **Bullet title.** Bullet body...
      - Optional sub-bullet
      - Optional sub-bullet
    - **Next bullet...**
    **Project: Another Company**
    - **Bullets for this project on the same day...**
    ### Day N+1 — ...

The parser ignores top-level title (#), blockquotes (>), week headers (## Week),
horizontal rules (---), and blank lines. Sub-bullets and indented continuation
text are folded into the parent bullet's text with literal newlines preserved.
"""

import re
from typing import Dict, List, Optional


# --- Compiled regex patterns ----------------------------------------------

# Day header: "### Day N — Mon YYYY-MM-DD (14:00 IST) → ..."
# Accepts em-dash (—), en-dash (–), or hyphen (-) between "Day N" and weekday.
RE_DAY = re.compile(
    r"^###\s+Day\s+\d+\s+[—–-]\s+\w+\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

# Project header: "**Project: Company Name (FriendlyName)**" with optional trailing notes.
# Non-greedy company-name capture, optional `(FriendlyName)` group.
RE_PROJECT = re.compile(
    r"^\*\*Project:\s+(.+?)(?:\s+\(([^)]+?)\))?\*\*(?:\s*[—–-].*)?$"
)

# Top-level bullet: "- ..." at column 0
RE_TOP_BULLET = re.compile(r"^-\s+(.*)")

# Week header (informational only — we use day header for the actual date)
RE_WEEK = re.compile(r"^##\s+Week\s+of\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def parse_timesheet(content: str) -> List[Dict]:
    """
    Parse timesheet markdown content into a list of structured entries.

    Returns a list of dicts, one per (work_date, project) pair. Entries
    with no bullets are dropped — empty project sub-sections don't appear
    in the output.

    Order in the returned list matches the file order (most recent week
    last in the file → last in the list). Callers that want a specific
    sort should sort the result.
    """
    entries: List[Dict] = []

    current_day: Optional[str] = None
    current_project: Optional[Dict[str, Optional[str]]] = None
    current_bullets: List[str] = []
    current_bullet_lines: List[str] = []  # buffer for the bullet being built

    def flush_bullet():
        """Commit the in-progress bullet (if any) to current_bullets."""
        nonlocal current_bullet_lines
        if current_bullet_lines:
            text = "\n".join(current_bullet_lines).rstrip()
            if text:
                current_bullets.append(text)
            current_bullet_lines = []

    def flush_entry():
        """Commit the in-progress (day, project) entry if it has bullets."""
        nonlocal current_bullets
        flush_bullet()
        if current_day and current_project and current_bullets:
            entries.append({
                "work_date": current_day,
                "project": dict(current_project),
                "bullets": list(current_bullets),
            })
        current_bullets = []

    for raw_line in content.splitlines():
        # Strip only trailing whitespace; preserve leading indentation for
        # sub-bullet detection.
        line = raw_line.rstrip()

        # ## Week of YYYY-MM-DD — informational, doesn't bind state. Skip.
        if RE_WEEK.match(line):
            continue

        # ### Day N — Mon YYYY-MM-DD
        m = RE_DAY.match(line)
        if m:
            flush_entry()
            current_day = m.group(1)
            current_project = None
            continue

        # **Project: Company (Friendly)** — flush prior entry, start new
        m = RE_PROJECT.match(line.strip())
        if m:
            flush_entry()
            current_project = {
                "company_name": m.group(1).strip(),
                "friendly_name": m.group(2).strip() if m.group(2) else None,
            }
            continue

        # Horizontal rule — flush current bullet (but don't close entry yet,
        # since the rule might be just a visual separator within the file)
        if line.strip() == "---":
            flush_bullet()
            continue

        # Top-level bullet: "- ..." at column 0
        m = RE_TOP_BULLET.match(line)
        if m:
            flush_bullet()
            current_bullet_lines = [m.group(1)]
            continue

        # Indented line (sub-bullet or continuation) → append to current
        # bullet IF we're inside one. Two-or-more leading spaces is the
        # markdown convention for continuation/sub-items.
        if (line.startswith("  ") or line.startswith("\t")) and current_bullet_lines:
            current_bullet_lines.append(line)
            continue

        # Anything else (title, blockquote, blank line, unrelated text) — ignore.

    # End of file — flush any pending bullet/entry
    flush_entry()
    return entries


# --- Lightweight self-test (run module directly to sanity-check) ----------

def _self_test():
    """Run as: python3 parser.py — feeds a tiny sample and prints the parse."""
    sample = """\
# Sample Timesheet

> Work shift info here.

---

## Week of 2026-05-11

### Day 1 — Mon 2026-05-11 (14:00 IST) → Tue 2026-05-12 (05:00 IST)

**Project: Acme, LLC (Acme)**

- **First bullet.** Body of first bullet.
- **Second bullet** with sub-bullets:
  - First sub-bullet
  - Second sub-bullet

**Project: Beta Corp**

- **Sole bullet on Beta.**

### Day 2 — Tue 2026-05-12 (14:00 IST) → Wed 2026-05-13 (05:00 IST)

**Project: Acme, LLC (Acme)** — busy day

- **Day 2 bullet.**
"""
    import json
    result = parse_timesheet(sample)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    assert len(result) == 3, f"expected 3 entries, got {len(result)}"
    assert result[0]["work_date"] == "2026-05-11"
    assert result[0]["project"]["company_name"] == "Acme, LLC"
    assert result[0]["project"]["friendly_name"] == "Acme"
    assert len(result[0]["bullets"]) == 2
    assert "First sub-bullet" in result[0]["bullets"][1]
    assert result[1]["project"]["friendly_name"] is None  # no parens
    assert result[2]["work_date"] == "2026-05-12"
    print("\n✓ self-test passed")


if __name__ == "__main__":
    _self_test()
