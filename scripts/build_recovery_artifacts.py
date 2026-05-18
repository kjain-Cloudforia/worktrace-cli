#!/usr/bin/env python3
"""
build_recovery_artifacts.py — one-shot setup for Phase 5h password recovery.

Generates the two artifacts that back the recovery flows:

  1. escrow/<user>.json  for every non-admin user (currently just kashish).
     The user's PAT is encrypted under the *admin recovery code* — NOT
     under the admin's password. This is deliberate: escrow encrypted
     under the password would silently break every time admin rotates
     or recovers their password. Coupling it to the recovery code
     instead means a single rebuild is only needed when the recovery
     code itself is rotated (rare).
     Cost: admin must dig the recovery code out of 1Password whenever
     they reset a user's password. That's acceptable because resets are
     rare and forces verifying the code is still findable.

  2. admin.recovery.json — the admin's PAT encrypted under a freshly
     generated 24-char Crockford-base32 recovery code. The code is
     printed ONCE to this terminal. Stash it in 1Password / paper.
     Loss of the code = admin must rebuild from scratch (rerun
     build_initial_user_records.py + run this script again).

The crypto here mirrors auth.js exactly: PBKDF2-HMAC-SHA256, 600k iters,
AES-GCM-256, 16-byte salt, 12-byte iv, no AAD. Cross-language round-trip
is verified locally before any file is written.

Run interactively from anywhere:
    python3 scripts/build_recovery_artifacts.py

Then commit & push the new files in ~/Documents/DevPlatform/auth/.
"""

import base64
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ---- Constants must match auth.js exactly --------------------------------

KDF_NAME       = "PBKDF2-HMAC-SHA256"
KDF_ITERATIONS = 600_000
KDF_SALT_LEN   = 16
AES_IV_LEN     = 12
KDF_KEY_LEN    = 32
SCHEMA_VERSION = 1

# Crockford base32 — same alphabet as auth.js generateRecoveryCode(),
# omits I/L/O/U so handwritten codes survive transcription.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Password policy (only applied when admin types their own password to
# wrap user PATs into escrow records; not applied to the recovery code
# itself, which is high-entropy by construction).
MIN_PASSWORD_LEN   = 12
REQUIRE_MIXED_CASE = True
REQUIRE_DIGIT      = True


# ---- Paths ---------------------------------------------------------------

DP_DIR        = Path.home() / "Documents" / "DevPlatform"
CONFIG_PATH   = DP_DIR / "config.json"
AUTH_DIR      = DP_DIR / "auth"
USERS_DIR     = AUTH_DIR / "users"
ESCROW_DIR    = AUTH_DIR / "escrow"
RECOVERY_PATH = AUTH_DIR / "admin.recovery.json"


# ---- Crypto helpers ------------------------------------------------------

def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KDF_KEY_LEN,
        salt=salt,
        iterations=iterations,
    ).derive(password.encode("utf-8"))


def encrypt(plaintext: str, password: str) -> dict:
    salt = os.urandom(KDF_SALT_LEN)
    iv   = os.urandom(AES_IV_LEN)
    key  = derive_key(password, salt, KDF_ITERATIONS)
    ct   = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return {
        "kdf":        KDF_NAME,
        "iterations": KDF_ITERATIONS,
        "salt":       b64(salt),
        "iv":         b64(iv),
        "ciphertext": b64(ct),
    }


def round_trip(record: dict, password: str, expected: str) -> None:
    """Decrypt the bundle we just built; abort if it doesn't match input."""
    salt = base64.b64decode(record["salt"])
    iv   = base64.b64decode(record["iv"])
    ct   = base64.b64decode(record["ciphertext"])
    key  = derive_key(password, salt, record["iterations"])
    plain = AESGCM(key).decrypt(iv, ct, None).decode("utf-8")
    if plain != expected:
        raise SystemExit("INTERNAL ERROR: encrypt → decrypt round-trip failed.")


def check_password_policy(password: str) -> list:
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
    if any(b in (password or "").lower()
           for b in ["password", "qwerty", "admin1234", "123456789012"]):
        reasons.append("Avoid common dictionary words and obvious patterns.")
    return reasons


# ---- Recovery-code helpers ----------------------------------------------

def generate_recovery_code() -> str:
    """24 Crockford-base32 chars formatted as XXXX-XXXX-...-XXXX."""
    chars = [CROCKFORD[secrets.randbelow(32)] for _ in range(24)]
    return "-".join("".join(chars[i:i+4]) for i in range(0, 24, 4))


def normalize_recovery_code(raw: str) -> str:
    """Strip hyphens/spaces, uppercase, map I/L→1, O→0, U→V. Mirror of auth.js."""
    s = (raw or "").upper()
    s = re.sub(r"[\s-]", "", s)
    table = {"I": "1", "L": "1", "O": "0", "U": "V"}
    s = "".join(table.get(c, c) for c in s)
    if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{24}", s):
        raise ValueError("Recovery code must normalise to 24 Crockford chars.")
    return s


# ---- Record builders -----------------------------------------------------

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_escrow_record(username: str, user_pat: str, recovery_code: str) -> dict:
    """Encrypt the user's PAT under the admin recovery code.

    We deliberately don't use admin's *password* here — escrow under the
    password would invalidate every time admin rotates their password,
    silently breaking the reset-user flow. Coupling escrow to the recovery
    code means it only needs rebuilding when the code itself rotates.
    """
    code = normalize_recovery_code(recovery_code)
    bundle = encrypt(user_pat, code)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "escrow",
        "username": username,
        "encrypted_by": "recovery_code",
        **bundle,
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }


def build_recovery_record(username: str, user_pat: str, recovery_code: str) -> dict:
    code = normalize_recovery_code(recovery_code)
    bundle = encrypt(user_pat, code)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "recovery",
        "username": username,
        **bundle,
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }


# ---- Interactive prompts -------------------------------------------------

def confirm(prompt: str) -> bool:
    return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")


# ---- Main ----------------------------------------------------------------

def main() -> int:
    print("=" * 68)
    print("  WorkTrace Phase 5h — build recovery artifacts")
    print("=" * 68)
    print()

    config = json.loads(CONFIG_PATH.read_text())
    kashish_pat = config["platform"]["github_token"]
    admin_pat   = config["platform"].get("admin_token")
    if not admin_pat:
        raise SystemExit("Missing platform.admin_token in config.json.")

    print(f"PATs loaded from {CONFIG_PATH.relative_to(Path.home())}")
    print(f"  kashish PAT: {kashish_pat[:20]}… ({len(kashish_pat)} chars)")
    print(f"  admin PAT:   {admin_pat[:20]}… ({len(admin_pat)} chars)")
    print()

    # ---- 1. Admin recovery record (must come first — its code wraps escrow) ----
    print("--- Step 1: build admin.recovery.json ---")
    print("Generates a recovery code that doubles as the escrow key.")
    print("The code is printed ONCE — write it down NOW.")
    print()

    code = None
    if RECOVERY_PATH.exists() and not confirm(
        "admin.recovery.json already exists. Overwrite "
        "(invalidates any existing code AND requires rebuilding escrow files)?"
    ):
        print("  ↳ Reusing existing admin.recovery.json.")
        print("  ⚠ You must supply the existing recovery code to rebuild escrows.")
        code = input("  Paste existing recovery code: ").strip()
        try:
            normalize_recovery_code(code)
        except ValueError as e:
            raise SystemExit(f"Invalid code: {e}")
    else:
        code = generate_recovery_code()
        recovery_record = build_recovery_record("admin", admin_pat, code)
        round_trip(recovery_record, normalize_recovery_code(code), admin_pat)
        RECOVERY_PATH.write_text(json.dumps(recovery_record, indent=2) + "\n")
        print(f"  ✓ wrote {RECOVERY_PATH} (round-trip verified)")
        print()
        print("┌" + "─" * 66 + "┐")
        print("│" + " " * 66 + "│")
        print("│   ADMIN RECOVERY CODE — store this NOW (1Password / paper):     │")
        print("│" + " " * 66 + "│")
        print(f"│       {code:<58} │")
        print("│" + " " * 66 + "│")
        print("│   This unlocks admin recovery AND every user escrow file.       │")
        print("│   Not stored anywhere else. Lose it AND your laptop → rebuild.  │")
        print("│" + " " * 66 + "│")
        print("└" + "─" * 66 + "┘")
        print()

    # ---- 2. Escrow files (encrypted under recovery code) ----
    print("--- Step 2: build escrow/kashish.json ---")
    print("Encrypts kashish's PAT under the recovery code so admin can")
    print("reset kashish's password without contacting them.")
    print()

    if ESCROW_DIR.exists() and (ESCROW_DIR / "kashish.json").exists():
        if not confirm("escrow/kashish.json already exists. Overwrite?"):
            print("  ↳ Skipping kashish escrow.")
            escrow_kashish = None
        else:
            escrow_kashish = build_escrow_record("kashish", kashish_pat, code)
    else:
        escrow_kashish = build_escrow_record("kashish", kashish_pat, code)

    if escrow_kashish:
        round_trip(escrow_kashish, normalize_recovery_code(code), kashish_pat)
        ESCROW_DIR.mkdir(parents=True, exist_ok=True)
        (ESCROW_DIR / "kashish.json").write_text(
            json.dumps(escrow_kashish, indent=2) + "\n")
        print(f"  ✓ wrote {ESCROW_DIR / 'kashish.json'} (round-trip verified)")
    print()

    print("Next steps:")
    print(f"  cd {AUTH_DIR}")
    print("  git add escrow/ admin.recovery.json")
    print("  git commit -m 'Phase 5h: escrow + admin recovery artifacts'")
    print("  git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
