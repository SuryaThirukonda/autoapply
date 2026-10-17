"""List Bitwarden login items that match a search term: name, username and URIs only (never passwords).
Usage: python scripts/bw_find_workday.py [search-term]"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from goat.config import SETTINGS  # noqa: E402

term = sys.argv[1] if len(sys.argv) > 1 else "myworkdayjobs"
env = {**os.environ, "BW_SESSION": SETTINGS.creds.bw_session}
proc = subprocess.run([SETTINGS.creds.bw_exe, "list", "items", "--search", term, "--nointeraction"],
                      capture_output=True, text=True, timeout=60, env=env)
if proc.returncode != 0:
    print("bw failed:", proc.returncode)
    sys.exit(1)
items = json.loads(proc.stdout or "[]")
print(f"{len(items)} item(s) matching {term!r}")
for it in items:
    login = it.get("login") or {}
    uris = [u.get("uri") for u in (login.get("uris") or [])]
    print(f"- id={it.get('id')} name={it.get('name')!r} username={login.get('username')!r} uris={uris}")
