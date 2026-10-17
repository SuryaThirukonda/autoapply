"""Show which vault items goat can see for a search term, and which one it would use as the universal
login. Prints ids, names, usernames and revision dates only, never a password.
Usage: python scripts/bw_probe_item.py [search-term] [host]"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goat.config import SETTINGS  # noqa: E402
from goat.creds import bitwarden  # noqa: E402

term = sys.argv[1] if len(sys.argv) > 1 else "myworkdayjobs"
host = sys.argv[2] if len(sys.argv) > 2 else "assurant.wd1.myworkdayjobs.com"

print("BW_UNIVERSAL_ITEM setting:", repr(SETTINGS.creds.universal_item))
env = {**os.environ, "BW_SESSION": SETTINGS.creds.bw_session}
proc = subprocess.run([SETTINGS.creds.bw_exe, "list", "items", "--search", term, "--nointeraction"],
                      capture_output=True, text=True, timeout=60, env=env)
if proc.returncode != 0:
    print("bw list failed:", proc.returncode)
    sys.exit(1)
items = json.loads(proc.stdout or "[]")
print(f"{len(items)} item(s) matching {term!r}, newest revision first:")
for it in sorted(items, key=lambda i: i.get("revisionDate") or "", reverse=True):
    login = it.get("login") or {}
    uris = [u.get("uri", "")[:60] for u in (login.get("uris") or [])]
    print(f"  id={it.get('id')}")
    print(f"     name={it.get('name')!r} user={login.get('username')!r} revised={it.get('revisionDate')}")
    print(f"     uris={uris}")
print()
print("goat would use for", host, "->", bitwarden.universal_item(host, SETTINGS.creds.application_email))
print("credentials_index row for that host:", bitwarden.lookup(host))
