"""Read-only: list Bitwarden vault items matching a search term (id prefix, name, URIs, username match,
revision date). Never prints a password. Run from Surya's terminal (where bw is unlocked):

    .venv\\Scripts\\python.exe scripts\\list_vault_items.py gevernova out.txt
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goat.creds import bitwarden  # noqa: E402

term = sys.argv[1]
out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
bitwarden.ensure_unlocked()
items = json.loads(bitwarden._bw("list", "items", "--search", term, what="list") or "[]")
lines = []
for it in items:
    login = it.get("login") or {}
    uris = [u.get("uri", "") for u in (login.get("uris") or [])]
    lines.append(f"{it.get('id', '')[:8]} | {it.get('name', '')} | uris={uris} | user={'thirukondas' in (login.get('username') or '')} | rev={it.get('revisionDate', '')}")
text = "\n".join(lines) or "(no items)"
print(text)
if out:
    out.write_text(text, encoding="utf-8")
