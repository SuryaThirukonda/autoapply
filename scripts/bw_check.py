"""Print the Bitwarden vault state as goat sees it (BW_EXE + BW_SESSION from .env). Never prints secrets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from goat.config import SETTINGS  # noqa: E402
from goat.creds import bitwarden  # noqa: E402

print("bw exe:", SETTINGS.creds.bw_exe)
print("session key set:", bool(SETTINGS.creds.bw_session))
try:
    print("vault status:", bitwarden.status())
except Exception as e:
    print("error:", type(e).__name__, str(e)[:200])
