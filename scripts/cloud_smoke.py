"""Cloud-link smoke run: the real cloud link (goat/cloud.py) and question broker against a COPY of goat.db,
walking one fake job ("Simulator Co", never a real posting, never Chrome) through every progress mark.
It asks one question and waits for the answer from the website or Telegram, then parks as a shadow draft.
Stop / Skip / Pause from the website act on it like on a real job.

    python scripts/cloud_smoke.py --env-file <file with GOAT_DB_URL> --workdir <scratch dir> [--idle 120]

The copy of goat.db and records/ live in --workdir; the real ones are only read.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()


def _copy_state(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    src = ROOT / "data" / "goat.db"
    dst = work / "goat.db"
    if src.exists() and not dst.exists():
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
            s.backup(d)
    rec = work / "records"
    if (ROOT / "records").is_dir() and not rec.exists():
        shutil.copytree(ROOT / "records", rec)
    os.environ["GOAT_DB"] = str(dst)
    os.environ["RECORDS_DIR"] = str(rec)


def _png(text: str) -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1440, 900), (246, 247, 249))
    d = ImageDraw.Draw(im)
    d.rectangle((80, 80, 1360, 820), outline=(49, 87, 213), width=4)
    d.text((120, 120), text, fill=(21, 24, 30))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


async def main(idle: float) -> None:
    from goat import db, records, states, worker
    from goat.cloud import CloudLink
    from goat.human.questions import BROKER

    db.connect()
    link = CloudLink()
    link.start(asyncio.get_running_loop())
    db.event(None, "supervisor:start", {"smoke": True})

    n = int(time.time())
    lid = f"smoke:{n}"
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats,source,first_seen,updated_at,active,triage_status,locations)"
                    " VALUES(?,?,?,?,?,?,?,?,1,'queued',?)",
                    (lid, "Simulator Co", "Software Engineering Intern (test run, not a real application)",
                     f"https://example.com/simulator/{n}", "simulator", "manual", n, n, '["Austin, TX"]'))
        cur = con.execute("INSERT INTO jobs(listing_id,status,lane,priority,attempts,created_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                          (lid, states.QUEUED, "no_account", 0, n, n))
        jid = int(cur.lastrowid)
    db.event(jid, "listing:added", {"company": "Simulator Co", "by": "smoke"})
    print(f"smoke: job {jid} queued", flush=True)
    await asyncio.sleep(3)

    async def fake_run(job: dict):
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, attempts=attempts+1, claimed_at=?, updated_at=? WHERE id=?",
                        (states.FILLING, db.now(), db.now(), jid))
        db.event(jid, "job:claimed", {"company": "Simulator Co", "ats": "simulator", "attempt": 1})
        await asyncio.sleep(3)
        records.save_screenshot(jid, _png("Simulator Co: application page 1"), "opened")
        db.event(jid, "adapter:prefill", {"set": 12, "failed": 0})
        await asyncio.sleep(3)
        db.event(jid, "page:done", {"page": 1, "next": 2})
        await asyncio.sleep(2)
        keys = await BROKER.ask(jid, [{"key": f"custom.smoke_hear_about_{n}", "question": f"How did you hear about Simulator Co? (test {n})",
                                       "options": ["LinkedIn", "Company website", "Other"], "field_type": "select"}])
        print("smoke: question asked; answer it on the website or in Telegram", flush=True)
        got = await BROKER.await_answers(jid, keys)
        print(f"smoke: got {got}", flush=True)
        await asyncio.sleep(2)
        db.event(jid, "page:done", {"page": 2, "next": 3})
        await asyncio.sleep(2)
        db.event(jid, "gate:passed", {"result": "passed", "checks": 9})
        records.save_screenshot(jid, _png("Simulator Co: review page, gate passed"), "hold")
        worker.set_status(jid, states.HOLD, reason="shadow_mode: gate passed; stopping before the final click")
        return worker.RunOutcome(states.HOLD, "shadow_mode")

    worker.CONTROL.bind()
    from unittest import mock
    with mock.patch.object(worker, "run_job", fake_run):
        out = await worker._run_controlled({"id": jid})
    print(f"smoke: job {jid} -> {out.status} ({out.reason})", flush=True)
    end = time.monotonic() + idle
    while time.monotonic() < end:
        await asyncio.sleep(1)
    db.event(None, "supervisor:stop", {"smoke": True})
    await asyncio.to_thread(link.stop, 10.0)
    print("smoke: done", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", required=True, type=Path)
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--idle", type=float, default=60.0, help="seconds to keep the link up after the job (for website commands)")
    a = ap.parse_args()
    _load_env(a.env_file)
    _copy_state(a.workdir)
    sys.path.insert(0, str(ROOT))
    asyncio.run(main(a.idle))
