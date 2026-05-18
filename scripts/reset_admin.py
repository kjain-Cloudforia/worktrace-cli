#!/usr/bin/env python3
"""
reset_admin.py — emergency fallback for admin password loss.

When to run this:
  • You lost your admin password.
  • You also lost the recovery code (so the dashboard's "Forgot password"
    flow can't help).
  • You DO still have a laptop with ~/Documents/DevPlatform/config.json
    containing platform.admin_token (the admin PAT plaintext).

What it does (no surprises — same dance you'd do by hand):
  1. Reads the admin PAT from config.json.
  2. Prompts you for a new password (twice, policy-enforced).
  3. Re-encrypts the admin PAT under the new password using the same
     crypto auth.js uses (PBKDF2 600k iters + AES-GCM-256).
  4. Rewrites ~/Documents/DevPlatform/auth/users/admin.json.
  5. Round-trips locally to verify before printing the commit command.

What it does NOT do automatically:
  • git add / commit / push — you do that yourself so this script never
    has to deal with auth or merge conflicts. The last line of output
    is the exact one-liner you need.

If you've also lost the laptop, you have no fallback short of:
  • generating a brand-new admin PAT on GitHub,
  • running build_initial_user_records.py to rebuild admin.json,
  • running build_recovery_artifacts.py to mint a fresh recovery code.
That's the irreducible cost of choosing a backend-free architecture.

Usage:
    python3 scripts/reset_admin.py
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


# Constants must match auth.js exactly (otherwise round-trip fails and
# we refuse to write the file).
KDF_NAME       = "PBKDF2-HMAC-SHA256"
KDF_ITERATIONS = 600_000
KDF_SALT_LEN   = 16
AES_IV_LEN     = 12
KDF_KEY_LEN    = 32

MIN_PASSWORD_LEN   = 12
REQUIRE_MIXED_CASE = True
REQUIRE_DIGIT      = True

DP_DIR      = Path.home() / "Documents" / "DevPlatform"
CONFIG_PATH = DP_DIR / "config.json"
ADMIN_PATH  = DP_DIR / "auth" / "users" / "admin.json"


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
        "salt":       base64.b64encode(salt).decode("ascii"),
        "iv":         base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ct).decode("ascii"),
    }


def round_trip(bundle: dict, password: str, expected: str) -> None:
    salt = base64.b64decode(bundle["salt"])
    iv   = base64.b64decode(bundle["iv"])
    ct   = base64.b64decode(bundle["ciphertext"])
    key  = derive_key(password, salt, bundle["iterations"])
    plain = AESGCM(key).decrypt(iv, ct, None).decode("utf-8")
    if plain != expected:
        raise SystemExit("INTERNAL ERROR: round-trip decrypt did not match input PAT.")


def check_policy(password: str) -> list:
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


def prompt_new_password() -> str:
    while True:
        p1 = getpass.getpass("New admin password: ")
        p2 = getpass.getpass("Confirm new admin password: ")
        if p1 != p2:
            print("  ✖ Passwords don't match. Try again.\n", file=sys.stderr)
            continue
        reasons = check_policy(p1)
        if reasons:
            print("  ✖ Password does not meet policy:", file=sys.stderr)
            for r in reasons:
                print(f"     - {r}", file=sys.stderr)
            print("  Try again.\n", file=sys.stderr)
            continue
        return p1


def main() -> int:
    print("=" * 60)
    print("  WorkTrace — reset admin password (local fallback)")
    print("=" * 60)
    print()

    if not CONFIG_PATH.exists():
        raise SystemExit(f"No config.json at {CONFIG_PATH}. Are you on the right laptop?")
    if not ADMIN_PATH.exists():
        raise SystemExit(
            f"No admin.json at {ADMIN_PATH}.\n"
            "Make sure ~/Documents/DevPlatform/auth/ is a clone of worktrace-auth.")

    config = json.loads(CONFIG_PATH.read_text())
    admin_pat = config.get("platform", {}).get("admin_token")
    if not admin_pat:
        raise SystemExit("Missing platform.admin_token in config.json — nothing to encrypt.")

    current = json.loads(ADMIN_PATH.read_text())
    print(f"Found admin.json — currently encrypted under salt prefix "
          f"{current.get('salt', '')[:8]}…")
    print(f"Admin PAT prefix from config.json: {admin_pat[:20]}…")
    print()

    new_pw = prompt_new_password()
    print()
    print("Re-encrypting (≈2s PBKDF2)…")

    bundle = encrypt(admin_pat, new_pw)
    round_trip(bundle, new_pw, admin_pat)

    new_record = {
        **current,
        **bundle,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    ADMIN_PATH.write_text(json.dumps(new_record, indent=2) + "\n")

    print()
    print(f"✓ Wrote {ADMIN_PATH} (round-trip verified)")
    print()
    print("Now commit + push to make the new password live:")
    print()
    print(f"  cd {ADMIN_PATH.parent.parent}")
    print("  git add users/admin.json")
    print("  git commit -m 'Local admin password reset (recovery script)'")
    print("  git push")
    print()
    print("After push: hard-refresh the dashboard and sign in with the new password.")
    print("If the recovery code was lost, run scripts/build_recovery_artifacts.py")
    print("to mint a fresh one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
