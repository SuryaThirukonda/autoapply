"""Operational memory in plain Markdown, the way always-on agents keep it:

- data/memory/MEMORY.md         curated standing notes (what works on which portal, decisions, gotchas)
- data/memory/notes/YYYY-MM-DD.md  the day's run notes: one line per job outcome, plus notes the chat
                                 agent or Surya adds ("remember: ...")

Both agents get `snapshot()` at the top of their prompts: MEMORY.md (capped) and the last two days of notes.
Facts about Surya never go here (config/ and the answer store own those); this is about the work."""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

from .config import SETTINGS

DIR = Path(SETTINGS.db_path).parent / "memory"
NOTES = DIR / "notes"
MEMORY_MD = DIR / "MEMORY.md"
HEADER = ("# goat memory\n\nCurated notes about the work (portals, tenants, decisions, gotchas). One line per fact; "
          "edit freely; facts about Surya belong in config/ and the answer store, not here.\n\n## Standing notes\n")


def _ensure() -> None:
    NOTES.mkdir(parents=True, exist_ok=True)
    if not MEMORY_MD.exists():
        MEMORY_MD.write_text(HEADER, encoding="utf-8")


def _notes_path(d: date | None = None) -> Path:
    return NOTES / f"{(d or date.today()).isoformat()}.md"


def note(text: str, *, job_id: int | None = None, by: str = "agent") -> Path:
    """Append one line to today's notes."""
    _ensure()
    p = _notes_path()
    line = f"- {time.strftime('%H:%M')} [{by}]{f' job {job_id}' if job_id else ''}: {' '.join(str(text).split())[:500]}\n"
    if not p.exists():
        p.write_text(f"# {date.today().isoformat()}\n\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write(line)
    return p


def note_outcome(job: dict, status: str, reason: str) -> None:
    """The worker's one line per finished run."""
    try:
        note(f"{job.get('company')} ({job.get('ats')}) -> {status}: {(reason or '')[:160]}", job_id=int(job["id"]), by="worker")
    except Exception:
        pass


def remember(text: str, *, by: str = "chat") -> Path:
    """Append a standing note to MEMORY.md (curated facts about the work)."""
    _ensure()
    with MEMORY_MD.open("a", encoding="utf-8") as f:
        f.write(f"- {date.today().isoformat()} ({by}): {' '.join(str(text).split())[:400]}\n")
    return MEMORY_MD


def snapshot(max_chars: int = 3000) -> str:
    """MEMORY.md (head) plus the tail of today's and yesterday's notes, for a prompt."""
    _ensure()
    parts = []
    mem = MEMORY_MD.read_text(encoding="utf-8").strip()
    if mem and mem != HEADER.strip():
        parts.append(mem[: max_chars // 2])
    for d in (date.today() - timedelta(days=1), date.today()):
        p = _notes_path(d)
        if p.exists():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                lines = txt.splitlines()
                parts.append("\n".join(lines[:1] + lines[-25:]) if len(lines) > 26 else txt)
    out = "\n\n".join(parts).strip()
    if not out:
        return ""
    return "MEMORY (what recent runs taught; notes, not facts about Surya):\n" + out[-max_chars:]
