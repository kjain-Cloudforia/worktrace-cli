#!/usr/bin/env python3
"""
build_initial_user_records.py — one-shot setup script for Phase 5c.

Generates the two initial encrypted user records (kashish.json + admin.json)
that bootstrap the WorkTrace username/password auth flow. Writes them into
~/Documents/DevPlatform/auth/users/ for review + commit.

Why a Python script instead of the browser admin-setup.html UX:
- Passwords stay in this terminal via getpass — never typed elsewhere
- PATs come from ~/Documents/DevPlatform/config.json — already on disk; no
  re-pasting into a browser form
- Cross-language crypto compatibility: the AES-GCM output here decrypts
  cleanly via the JS auth.js module (same primitives, same parameters)

Run interactively:
    python3 scripts/build_initial_user_records.py

This is a one-off — you run it once for the initial kashish + admin records.
After Phase 5e, admin user-management is in the dashboard UI; this script is
no longer needed and can stay around as a backup recovery tool.
"""

import base64
import getpass
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ---- Constants must match worktrace-app/docs/auth/auth.js exactly --------

KDF_NAME       = "PBKDF2-HMAC-SHA256"
KDF_ITERATIONS = 600_000
KDF_SALT_LEN   = 16   # bytes
AES_IV_LEN     = 12   # bytes — AES-GCM standard
KDF_KEY_LEN    = 32   # bytes = 256 bits
SCHEMA_VERSION = 1

# Password policy — matches auth.js MIN_PASSWORD_LEN etc.
MIN_PASSWORD_LEN   = 12
REQUIRE_MIXED_CASE = True
REQUIRE_DIGIT      = True


# ---- Paths ---------------------------------------------------------------

DP_DIR = Path.home() / "Documents" / "DevPlatform"
CONFIG_PATH = DP_DIR / "config.json"
AUTH_USERS_DIR = DP_DIR / "auth" / "users"


# ---- Crypto helpers ------------------------------------------------------

def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KDF_KEY_LEN,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt_pat(plaintext_pat: str, password: str) -> dict:
    """Mirror of auth.js encryptSecret. Returns the crypto-bundle subset of a user record."""
    salt = os.urandom(KDF_SALT_LEN)
    iv   = os.urandom(AES_IV_LEN)
    key  = derive_key(password, salt, KDF_ITERATIONS)
    aes  = AESGCM(key)
    ct   = aes.encrypt(iv, plaintext_pat.encode("utf-8"), None)
    return {
        "kdf":        KDF_NAME,
        "iterations": KDF_ITERATIONS,
        "salt":       b64(salt),
        "iv":         b64(iv),
        "ciphertext": b64(ct),
    }


def check_password_strength(password: str) -> list:
    """Mirror of auth.js checkPasswordStrength. Returns [] if ok, else list of reasons."""
    reasons = []
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        reasons.append(f"Must be at least {MIN_PASSWORD_LEN} characters.")
    if REQUIRE_MIXED_CASE:
        if not re.search(r"[a-z]", password):
            reasons.append("Must include a lowercase letter.")
        if not re.search(r"[A-Z]", password):
            reasons.append("Must include an uppercase letter.")
    if REQUIRE_DIGIT and not re.search(r"[0-9]", password):
        reasons.append("Must include a digit.")
    lower = (password or "").lower()
    if any(b in lower for b in ["password", "qwerty", "admin1234", "123456789012"]):
        reasons.append("Avoid common dictionary words and obvious patterns.")
    return reasons


# ---- Record builders -----------------------------------------------------

def build_record(
    username: str,
    display_name: str,
    data_repo,
    pat: str,
    password: str,
    is_admin: bool = False,
    managed_repos: list = None,
) -> dict:
    if not re.match(r"^[a-z][a-z0-9-]*$", username or ""):
        raise ValueError("username must be a lowercase slug (starts with a letter; "
                         "letters, digits, hyphens only)")
    bundle = encrypt_pat(pat, password)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "schema_version": SCHEMA_VERSION,
        "username": username,
        "display_name": display_name,
        "data_repo": data_repo,
        "is_admin": bool(is_admin),
        **bundle,
        "created_at": now,
        "updated_at": now,
    }
    if is_admin and managed_repos:
        record["managed_repos"] = list(managed_repos)
    return record


# ---- Round-trip self-test -----------------------------------------------

def round_trip_verify(record: dict, password: str, expected_pat: str) -> None:
    """Decrypt the record we just built; confirm it matches the input PAT.

    This is paranoia: if any constant drifts (iterations, lengths, KDF name)
    between Python and the JS auth.js, this will catch it locally before we
    commit and lock ourselves out.
    """
    salt = base64.b64decode(record["salt"])
    iv   = base64.b64decode(record["iv"])
    ct   = base64.b64decode(record["ciphertext"])
    key  = derive_key(password, salt, record["iterations"])
    plain = AESGCM(key).decrypt(iv, ct, None).decode("utf-8")
    if plain != expected_pat:
        raise SystemExit("INTERNAL ERROR: encrypt-then-decrypt did not round-trip.")


# ---- Prompts -------------------------------------------------------------

def prompt_password(label: str) -> str:
    """Prompt for a password twice; enforce policy; abort on mismatch."""
    while True:
        p1 = getpass.getpass(f"Password for `{label}`: ")
        p2 = getpass.getpass(f"Confirm password for `{label}`: ")
        if p1 != p2:
            print("  ✖ Passwords don't match. Try again.\n", file=sys.stderr)
            continue
        reasons = check_password_strength(p1)
        if reasons:
            print("  ✖ Password does not meet policy:", file=sys.stderr)
            for r in reasons:
                print(f"     - {r}", file=sys.stderr)
            print("  Try again.\n", file=sys.stderr)
            continue
        return p1


# ---- Main ---------------------------------------------------------------

def main() -> int:
    print("=" * 64)
    print("  WorkTrace Phase 5c — generate initial user records")
    print("=" * 64)
    print()

    config = json.loads(CONFIG_PATH.read_text())
    kashish_pat = config["platform"]["github_token"]
    admin_pat   = config["platform"].get("admin_token")
    if not admin_pat:
        raise SystemExit("Missing platform.admin_token in config.json.")
    data_repo   = config["platform"]["github_login"] + "/worktrace-data-kashish"
    display     = config["platform"].get("display_name", "Kashish Jain")
    print(f"PATs loaded from {CONFIG_PATH.relative_to(Path.home())}")
    print(f"  kashish PAT: {kashish_pat[:20]}… ({len(kashish_pat)} chars)")
    print(f"  admin PAT:   {admin_pat[:20]}… ({len(admin_pat)} chars)")
    print()

    print("Policy reminder: passwords must be 12+ chars, mixed case, at least one digit.")
    print("(Don't type them anywhere except this prompt. Nothing is logged or shown back.)")
    print()

    pw_kashish = prompt_password("kashish")
    print()
    pw_admin   = prompt_password("admin")
    print()

    print("Generating records (PBKDF2 600k iters × 2 — takes ~3-4 seconds)…")
    kashish_record = build_record(
        username="kashish",
        display_name=display,
        data_repo=data_repo,
        pat=kashish_pat,
        password=pw_kashish,
        is_admin=False,
    )
    round_trip_verify(kashish_record, pw_kashish, kashish_pat)

    admin_record = build_record(
        username="admin",
        display_name="Admin",
        data_repo=None,
        pat=admin_pat,
        password=pw_admin,
        is_admin=True,
        managed_repos=[data_repo],
    )
    round_trip_verify(admin_record, pw_admin, admin_pat)

    # Write to auth/users/
    AUTH_USERS_DIR.mkdir(parents=True, exist_ok=True)
    (AUTH_USERS_DIR / "kashish.json").write_text(
        json.dumps(kashish_record, indent=2) + "\n"
    )
    (AUTH_USERS_DIR / "admin.json").write_text(
        json.dumps(admin_record, indent=2) + "\n"
    )

    print()
    print("✓ Records generated and round-trip verified.")
    print(f"  {AUTH_USERS_DIR / 'kashish.json'}")
    print(f"  {AUTH_USERS_DIR / 'admin.json'}")
    print()
    print("Next: review the files, then `cd auth && git add users/ && git commit && git push`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
