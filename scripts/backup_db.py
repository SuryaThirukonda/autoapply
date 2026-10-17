"""Nightly backup of goat.db: a consistent online copy (SQLite's backup API, what the sqlite3 shell's
`.backup` uses, safe while goat is writing), checked with PRAGMA quick_check, gzip-compressed into
data/backups/, oldest copies pruned. With --upload-cmd (or BACKUP_UPLOAD_CMD) the finished file is
handed to an uploader for off-box storage, e.g. `rclone copy {file} remote:goat-backups`; {file} is
replaced by the path, or the path is appended. The command runs without a shell.

  python scripts/backup_db.py [--db data/goat.db] [--dest data/backups] [--keep 14] [--upload-cmd "..."]

On the PC, a daily Task Scheduler task can run it:
  python scripts/backup_db.py >> data/backups/backup.log 2>&1

Exit codes: 0 ok, 1 backup failed (nothing new kept), 2 backup kept but the upload failed.
"""
from __future__ import annotations

import argparse
import gzip
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "goat-"
SUFFIX = ".db.gz"


def backup(db_path: Path, dest_dir: Path, keep: int = 14, now: float | None = None) -> Path:
    """Write <dest>/goat-YYYYmmdd-HHMMSS.db.gz (UTC) and prune to the newest `keep`. Returns the path."""
    db_path, dest_dir = Path(db_path), Path(dest_dir)
    if not db_path.is_file():
        raise FileNotFoundError(f"no database at {db_path}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    final = dest_dir / f"{PREFIX}{stamp}{SUFFIX}"
    tmp_db = dest_dir / f".{PREFIX}{stamp}.db.tmp"
    try:
        # A plain connection: a read-only one cannot open a WAL database whose -shm file is missing.
        src = sqlite3.connect(db_path, timeout=30)
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
            src.close()
        if ok != "ok":
            raise RuntimeError(f"quick_check on the copy failed: {ok}")
        part = final.with_name(final.name + ".part")
        with open(tmp_db, "rb") as fin, gzip.open(part, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
        os.replace(part, final)
    finally:
        tmp_db.unlink(missing_ok=True)
        final.with_name(final.name + ".part").unlink(missing_ok=True)
    prune(dest_dir, keep)
    return final


def prune(dest_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` backups (by name, which sorts by time). Returns what was removed."""
    files = sorted(p for p in Path(dest_dir).glob(f"{PREFIX}*{SUFFIX}") if p.is_file())
    old = files[:-keep] if keep > 0 else []
    for p in old:
        p.unlink()
    return old


def upload_argv(cmd: str, file: Path) -> list[str]:
    argv = shlex.split(cmd)
    if any("{file}" in a for a in argv):
        return [a.replace("{file}", str(file)) for a in argv]
    return [*argv, str(file)]


def main(argv: list[str] | None = None) -> int:
    try:   # GOAT_DB and BACKUP_* may live in .env; variables already set (the container's) win
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default=os.environ.get("GOAT_DB") or str(ROOT / "data" / "goat.db"))
    p.add_argument("--dest", default=os.environ.get("BACKUP_DIR") or str(ROOT / "data" / "backups"))
    p.add_argument("--keep", type=int, default=int(os.environ.get("BACKUP_KEEP") or 14))
    p.add_argument("--upload-cmd", default=os.environ.get("BACKUP_UPLOAD_CMD", ""))
    a = p.parse_args(argv)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        out = backup(Path(a.db), Path(a.dest), a.keep)
    except Exception as e:
        print(f"{stamp} backup FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"{stamp} backup ok: {out} ({out.stat().st_size} bytes)")
    if a.upload_cmd:
        try:
            r = subprocess.run(upload_argv(a.upload_cmd, out), capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"{stamp} upload FAILED: {type(e).__name__}", file=sys.stderr)
            return 2
        if r.returncode != 0:
            print(f"{stamp} upload FAILED (exit {r.returncode})", file=sys.stderr)
            return 2
        print(f"{stamp} upload ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
