"""Exit 1 when the agent is hung: a job is mid-run (filling / gated / submitting) and no event was written for HANG_S.

ensure_agent_loop.ps1 runs this every 5 minutes and kills `goat run` on exit 1 (the loop starts it again). On 2026-10-24 a
model request died with 'access violation writing 0x...48' and the process sat idle for 28 minutes with job 557 'filling',
until the half-hourly check-in found it. A job waiting on Surya's answer or an emailed code writes no events and is not a
hang.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

HANG_S = 20 * 60
MID_RUN = ("filling", "gated", "submitting")


def hung(con: sqlite3.Connection, now: int | None = None, hang_s: int = HANG_S) -> int | None:
    """The id of the job the agent is stuck on, else None."""
    now = now or int(time.time())
    marks = ",".join("?" for _ in MID_RUN)
    row = con.execute(f"SELECT id, updated_at FROM jobs WHERE status IN ({marks}) ORDER BY updated_at DESC LIMIT 1", MID_RUN).fetchone()
    if row is None or (row[1] or 0) > now - hang_s:
        return None
    last = con.execute("SELECT MAX(ts) FROM events").fetchone()[0] or 0
    return int(row[0]) if last < now - hang_s else None


def main() -> int:
    db = Path(__file__).resolve().parent.parent / "data" / "goat.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    job = hung(con)
    if job is not None:
        print(f"hung on job {job}: no event for {HANG_S // 60} minutes")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
