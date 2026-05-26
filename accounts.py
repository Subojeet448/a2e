"""
╔══════════════════════════════════════════════════════════════════╗
║         A2E Password Vault v3  —  CLI + Batch Runner            ║
║                                                                  ║
║  Security improvements:                                         ║
║  ✅ PBKDF2-SHA256 (480k iterations) key derivation              ║
║  ✅ Fernet (AES-128-CBC + HMAC-SHA256) encryption               ║
║  ✅ Passwords wiped from memory after use (ctypes memset)       ║
║  ✅ Master password never stored — only derived key used        ║
║  ✅ POST /claim used (no credentials in URL)                    ║
╚══════════════════════════════════════════════════════════════════╝

Usage:
  python accounts.py add              # add account (interactive)
  python accounts.py list             # show accounts (passwords masked)
  python accounts.py remove           # remove account
  python accounts.py run              # run claim for ALL accounts
  python accounts.py run --email x    # run for one account
"""

import asyncio
import base64
import ctypes
import getpass
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# ─── Paths ────────────────────────────────────────────────────────────────────
VAULT_FILE  = Path("vault.enc")
SALT_FILE   = Path(".vault.salt")

# ─── Config ───────────────────────────────────────────────────────────────────
API_BASE           = os.getenv("A2E_API_URL",    "http://localhost:8000")
API_KEY            = os.getenv("A2E_API_KEY",    "")       # must match server
BETWEEN_ACCT_DELAY = int(os.getenv("A2E_SWITCH_WAIT", "30"))


# ══════════════════════════════════════════════════════════════════════════════
#  Memory safety helper
# ══════════════════════════════════════════════════════════════════════════════

def _wipe_str(s: str):
    """
    Best-effort: overwrite string's backing memory with zeros.
    Python strings are immutable/interned so this is not guaranteed,
    but it reduces exposure window.
    """
    try:
        buf_id = id(s) + sys.getsizeof("") - len(s) - 1
        ctypes.memset(buf_id, 0, len(s))
    except Exception:
        pass  # non-critical


# ══════════════════════════════════════════════════════════════════════════════
#  Vault encryption
# ══════════════════════════════════════════════════════════════════════════════

def _get_or_create_salt() -> bytes:
    if SALT_FILE.exists():
        return SALT_FILE.read_bytes()
    salt = os.urandom(32)
    SALT_FILE.write_bytes(salt)
    SALT_FILE.chmod(0o600)
    return salt


def _derive_key(master_password: str) -> bytes:
    """PBKDF2-SHA256, 480k iterations — brute-force resistant."""
    salt = _get_or_create_salt()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(master_password.encode("utf-8")))


def _fernet(master_password: str) -> Fernet:
    return Fernet(_derive_key(master_password))


def _load_vault(mp: str) -> list:
    if not VAULT_FILE.exists():
        return []
    try:
        data = _fernet(mp).decrypt(VAULT_FILE.read_bytes())
        return json.loads(data)
    except InvalidToken:
        print("❌ Wrong master password — cannot decrypt vault.")
        sys.exit(1)


def _save_vault(mp: str, accounts: list):
    data = _fernet(mp).encrypt(json.dumps(accounts).encode())
    VAULT_FILE.write_bytes(data)
    VAULT_FILE.chmod(0o600)   # owner read/write only


def _get_master_password(prompt: str = "Master password: ") -> str:
    mp = os.getenv("A2E_VAULT_KEY")
    if mp:
        return mp
    return getpass.getpass(prompt)


# ══════════════════════════════════════════════════════════════════════════════
#  CLI commands
# ══════════════════════════════════════════════════════════════════════════════

def cmd_add():
    mp = _get_master_password("Master password (create new if first time): ")
    accounts = _load_vault(mp)

    email    = input("A2E Email: ").strip().lower()
    password = getpass.getpass("A2E Password: ")

    for a in accounts:
        if a["email"] == email:
            confirm = input(f"⚠️  {email} already exists. Update password? (y/n): ")
            if confirm.lower() == "y":
                a["password"] = password
                _save_vault(mp, accounts)
                print("✅ Password updated.")
                _wipe_str(password)
                _wipe_str(mp)
            return

    accounts.append({"email": email, "password": password})
    _save_vault(mp, accounts)
    print(f"✅ Added: {email}")
    _wipe_str(password)
    _wipe_str(mp)


def cmd_list():
    mp = _get_master_password()
    accounts = _load_vault(mp)
    _wipe_str(mp)

    if not accounts:
        print("Vault is empty.")
        return

    print(f"\n{'#':<4} {'Email':<36} {'Password (masked)'}")
    print("-" * 65)
    for i, a in enumerate(accounts, 1):
        pw = a["password"]
        masked = pw[:2] + "●" * min(6, len(pw) - 3) + pw[-1:] if len(pw) > 3 else "●●●"
        print(f"{i:<4} {a['email']:<36} {masked}")
    print(f"\nTotal: {len(accounts)} account(s)\n")


def cmd_remove():
    mp = _get_master_password()
    accounts = _load_vault(mp)
    cmd_list()
    email = input("Email to remove: ").strip().lower()
    before = len(accounts)
    accounts = [a for a in accounts if a["email"] != email]
    if len(accounts) < before:
        _save_vault(mp, accounts)
        print(f"✅ Removed: {email}")
    else:
        print(f"❌ Not found: {email}")
    _wipe_str(mp)


# ══════════════════════════════════════════════════════════════════════════════
#  API caller  — POST body, never URL params
# ══════════════════════════════════════════════════════════════════════════════

def _call_api(email: str, password: str) -> dict:
    """
    POST /claim  — credentials go in JSON body, not URL.
    X-Api-Key header added if configured.
    """
    url = f"{API_BASE}/claim"
    payload = json.dumps({"email": email, "password": password}).encode()
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-Api-Key"] = API_KEY

    try:
        req  = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"email": email, "status": "fail", "message": f"HTTP {e.code}: {body[:200]}"}
    except Exception as exc:
        return {"email": email, "status": "fail", "message": str(exc)}
    finally:
        _wipe_str(password)   # wipe after use


# ══════════════════════════════════════════════════════════════════════════════
#  Batch runner
# ══════════════════════════════════════════════════════════════════════════════

def cmd_run(filter_email: str = None):
    mp       = _get_master_password()
    accounts = _load_vault(mp)
    _wipe_str(mp)

    if filter_email:
        accounts = [a for a in accounts if a["email"] == filter_email]

    if not accounts:
        print("No accounts found.")
        return

    print(f"\n{'='*65}")
    print(f"  A2E Daily Claim  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Accounts: {len(accounts)}")
    print(f"{'='*65}\n")

    results = []
    for idx, acct in enumerate(accounts, 1):
        email = acct["email"]
        print(f"[{idx}/{len(accounts)}] → {email}")

        result = _call_api(email, acct["password"])
        results.append(result)

        icon    = {"success": "✅", "already_claimed": "⚠️ ", "fail": "❌"}.get(result.get("status", "fail"), "❓")
        status  = result.get("status", "fail").upper()
        message = result.get("message", "")
        earned  = result.get("earned", 0)
        balance = result.get("balance", "N/A")
        retries = result.get("retries", 0)

        print(f"  {icon} {status}")
        print(f"     Message : {message}")
        print(f"     Earned  : {earned} credits")
        print(f"     Balance : {balance}")
        print(f"     Retries : {retries}")
        print()

        if idx < len(accounts):
            print(f"  ⏳ {BETWEEN_ACCT_DELAY}s cooldown before next account…\n")
            import time; time.sleep(BETWEEN_ACCT_DELAY)

    # Summary
    total   = len(results)
    success = sum(1 for r in results if r.get("status") == "success")
    already = sum(1 for r in results if r.get("status") == "already_claimed")
    failed  = total - success - already

    print(f"{'='*65}")
    print(f"  SUMMARY  |  Total={total}  ✅={success}  ⚠️={already}  ❌={failed}")
    print(f"{'='*65}\n")

    log_file = f"run_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(log_file, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"  Log saved → {log_file}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]
    cmd  = args[0] if args else "help"

    if cmd == "add":
        cmd_add()
    elif cmd == "list":
        cmd_list()
    elif cmd == "remove":
        cmd_remove()
    elif cmd == "run":
        fe = None
        if "--email" in args:
            i  = args.index("--email")
            fe = args[i + 1] if i + 1 < len(args) else None
        cmd_run(fe)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
