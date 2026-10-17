"""The cloud link (goat/cloud.py) against a throwaway Postgres built from goat-dashboard's migrations:
the mirror, the two roles' limits, every command, and the worker's stop / skip / browser back-off.

Needs TEST_PG_ADMIN_URL (a superuser URL to a scratch server; a fresh database is made per module) and
the goat-dashboard checkout next to this repo (or GOAT_DASHBOARD_DIR). Skipped otherwise. Never point it
at RDS.

    TEST_PG_ADMIN_URL=postgresql://postgres@127.0.0.1:55432/postgres pytest tests/test_cloud.py -q
"""
from __future__ import annotations

import asyncio
import os
import secrets
import struct
import zlib
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest

from goat import answers, db, states, worker
from goat.config import SETTINGS
from goat.human import questions

psycopg = pytest.importorskip("psycopg")

ADMIN = os.environ.get("TEST_PG_ADMIN_URL")
DASH = Path(os.environ.get("GOAT_DASHBOARD_DIR", Path(__file__).resolve().parents[2] / "goat-dashboard"))
pytestmark = pytest.mark.skipif(not ADMIN or not (DASH / "db" / "migrations").is_dir(),
                                reason="TEST_PG_ADMIN_URL or the goat-dashboard migrations missing")


def _url(base: str, dbname: str, user: str | None = None) -> str:
    u = urlparse(base)
    host = u.netloc.split("@")[-1]
    netloc = f"{user}@{host}" if user else u.netloc
    return urlunparse(u._replace(netloc=netloc, path="/" + dbname))


@pytest.fixture(scope="module")
def pg():
    name = "goat_cloud_" + secrets.token_hex(4)
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"create database {name}")
    owner = _url(ADMIN, name)
    with psycopg.connect(owner) as c:
        for f in sorted((DASH / "db" / "migrations").glob("*.sql")):
            c.execute(f.read_text(encoding="utf-8"))
        c.execute("alter role goat_agent login")
        c.execute("alter role goat_web login")
        c.commit()
    yield {"owner": owner, "agent": _url(ADMIN, name, "goat_agent"), "web": _url(ADMIN, name, "goat_web")}
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"drop database {name} with (force)")


@pytest.fixture
def link(pg, tmp_path, monkeypatch):
    """A fresh local goat.db, an empty mirror, and a CloudLink that is not running its threads."""
    from goat import cloud
    db.reset_for_tests(tmp_path / "goat.db")
    monkeypatch.setattr(SETTINGS, "records_dir", tmp_path / "records")
    monkeypatch.setattr(SETTINGS.budgets, "answer_wait_s", 60)
    with psycopg.connect(pg["owner"]) as c:
        c.execute("alter table events disable trigger events_no_update")
        c.execute("truncate listings, jobs, events, questions, answers, answer_aliases, field_fills, applications, "
                  "screenshots, agent_status, commands, telegram_messages cascade")
        c.execute("alter table events enable trigger events_no_update")
        c.commit()
    monkeypatch.setattr(questions, "BROKER", questions.QuestionBroker())
    lk = cloud.CloudLink(url=pg["agent"], agent="test-pc")
    yield lk
    lk._close_push()


def owner_q(pg, sql, *args):
    with psycopg.connect(pg["owner"]) as c:
        return c.execute(sql, args).fetchall()


def make_job(lid: str, status: str = states.QUEUED, company: str = "Acme", blocked_on: str | None = None,
             url: str | None = None) -> int:
    with db.tx() as con:
        con.execute("INSERT INTO listings(id, company, title, url, ats, first_seen, updated_at, active, locations) "
                    "VALUES(?,?,?,?,?,?,?,1,?)",
                    (lid, company, "SWE Intern", url or f"https://boards.greenhouse.io/acme/jobs/{lid}", "greenhouse",
                     db.now(), db.now(), '["Austin, TX", "Remote"]'))
        cur = con.execute("INSERT INTO jobs(listing_id, status, lane, blocked_on_question_key, hold_reason, created_at, updated_at)"
                          " VALUES(?,?,?,?,?,?,?)",
                          (lid, status, "no_account", blocked_on, f"answer_timeout: {blocked_on}" if blocked_on else None,
                           db.now(), db.now()))
    jid = int(cur.lastrowid)
    db.event(jid, "listing:new", {"listing_id": lid})
    return jid


def command(pg, kind: str, payload: dict, source: str = "web") -> int:
    """Insert a command the way the website does: as goat_web."""
    from psycopg.types.json import Jsonb
    with psycopg.connect(pg["web"]) as c:
        cid = c.execute("insert into commands (source, kind, payload) values (%s, %s, %s) returning id",
                        (source, kind, Jsonb(payload))).fetchone()[0]
        c.commit()
    return cid


def settle(link, pg) -> None:
    with psycopg.connect(pg["agent"], autocommit=True) as c:
        link.apply_pending(c)


def command_row(pg, cid: int):
    return owner_q(pg, "select status, result, error from commands where id = %s", cid)[0]


def png_bytes(w: int = 4, h: int = 3) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------------------------------- mirror
def test_push_mirrors_the_agent_state_and_resumes_from_rds(link, pg):
    jid = make_job("L1")
    answers.save_answer("identity.email", "x@example.com", source="seed")
    answers.save_answer("identity.email", "y@example.com", source="seed")
    with db.tx() as con:
        con.execute("INSERT INTO decisions(job_id,page,field_label,value,source,ref,ts) VALUES(?,?,?,?,?,?,?)",
                    (jid, 1, "Email", "y@example.com", "standing:identity.email", "", db.now()))
        con.execute("INSERT INTO applications(listing_id, canonical_key, company, title, url, origin, submitted_at) "
                    "VALUES('L0','u:x','Old Co','Intern','https://x','hermes',?)", (db.now(),))
    db.event(jid, "adapter:prefill", {"set": 7, "failed": 0})
    sent = link.push_now()
    assert sent["events"] >= 2 and sent["jobs"] == 1
    assert owner_q(pg, "select state, priority from jobs where id = %s", jid) == [("queued", 0)]
    assert owner_q(pg, "select company, locations from listings where id = 'L1'") == [("Acme", "Austin, TX; Remote")]
    assert ("prefilled 7 known field(s)",) in owner_q(pg, "select message from events where kind = 'adapter:prefill'")
    cur = owner_q(pg, "select answer from answers where key = 'identity.email' and superseded_by is null")
    assert cur == [("y@example.com",)]
    assert owner_q(pg, "select count(*) from answers where superseded_by is not null") == [(1,)]
    assert owner_q(pg, "select label, source from field_fills") == [("Email", "standing:identity.email")]
    assert owner_q(pg, "select company from applications") == [("Old Co",)]
    # nothing new: nothing sent; a restarted link resumes from what RDS holds
    assert link.push_now()["events"] == 0
    from goat import cloud
    again = cloud.CloudLink(url=pg["agent"], agent="test-pc")
    db.event(jid, "job:hold", {"reason": "x"})
    assert again.push_now()["events"] == 1
    again._close_push()
    assert owner_q(pg, "select count(*) from events where agent_n is not null")[0][0] == \
        db.connect().execute("SELECT COUNT(*) FROM events").fetchone()[0]


def test_heartbeat_and_screenshots(link, pg):
    jid = make_job("L2")
    d = SETTINGS.records_dir / str(jid)
    d.mkdir(parents=True)
    (d / "1792049211663_opened.png").write_bytes(png_bytes(2000, 10))
    db.event(jid, "job:claimed", {})
    link.push_now()
    rows = owner_q(pg, "select job_id, tag, mime, width, length(bytes) > 0 from screenshots")
    assert rows == [(jid, "opened", "image/jpeg", 1280, True)]
    hb = owner_q(pg, "select agent, mode, worker_paused from agent_status")
    assert hb == [("test-pc", SETTINGS.mode, False)]


# ------------------------------------------------------------------------------------------- role limits
@pytest.mark.parametrize("sql", [
    "update jobs set state = 'queued'",
    "insert into answers (id, key) values (999, 'x')",
    "update questions set status = 'answered'",
    "delete from commands",
    "update commands set kind = 'x'",
    "insert into agent_status (agent) values ('x')",
])
def test_web_role_cannot_touch_agent_rows(pg, sql):
    with psycopg.connect(pg["web"]) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(sql)


@pytest.mark.parametrize("sql", [
    "insert into commands (source, kind) values ('web', 'resync')",
    "delete from jobs",
    "update app_settings set telegram_progress = 'off'",
    "insert into telegram_state (key, value) values ('x', '1')",
    "select bytes from screenshots",
])
def test_agent_role_limits(pg, sql):
    with psycopg.connect(pg["agent"]) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(sql)


def test_events_stay_append_only(pg):
    with psycopg.connect(pg["owner"]) as c, pytest.raises(psycopg.errors.RaiseException):
        c.execute("insert into events (kind) values ('t')")
        c.execute("delete from events")


# ------------------------------------------------------------------------------------------- commands
async def test_answer_from_the_website_releases_holds_and_wakes_the_waiting_job(link, pg):
    from goat.human import questions as q
    waiting = make_job("L3", status=states.FILLING)
    held = make_job("L4", status=states.HOLD, blocked_on="custom.hear_about")
    await q.BROKER.ask(waiting, [{"key": "custom.hear_about", "question": "How did you hear about us?",
                                  "options": ["LinkedIn", "Other"]}])
    link.push_now()
    qid, status = owner_q(pg, "select id, status from questions")[0]
    assert status == "open"
    waiter = asyncio.create_task(q.BROKER.await_answers(waiting, ["custom.hear_about"]))
    await asyncio.sleep(0.05)
    cid = command(pg, "answer_question", {"question_id": qid, "answer": "1"})
    await asyncio.to_thread(settle, link, pg)
    got = await asyncio.wait_for(waiter, 5)
    assert got == {"custom.hear_about": "LinkedIn"}
    st, result, err = command_row(pg, cid)
    assert st == "done" and result["released"] == 1 and err is None
    assert owner_q(pg, "select status, answer from questions where id = %s", qid) == [("answered", "LinkedIn")]
    assert owner_q(pg, "select state from jobs where id = %s", held) == [("queued",)]
    assert owner_q(pg, "select source from answers where key = 'custom.hear_about'") == [("web",)]
    kinds = [r[0] for r in owner_q(pg, "select kind from events order by id")]
    assert "question:answered" in kinds and kinds[-1] == "command:done"
    # the second channel loses: one answer wins
    late = command(pg, "answer_question", {"question_id": qid, "answer": "Other"}, source="telegram")
    await asyncio.to_thread(settle, link, pg)
    assert command_row(pg, late)[0] == "rejected"


async def test_decline_and_company_scope(link, pg):
    from goat.human import questions as q
    a = make_job("L5", status=states.FILLING, company="Zeta Corp")
    await q.BROKER.ask(a, [{"key": "custom.ref", "question": "Referred by anyone?"},
                           {"key": "custom.why", "question": "Why Zeta?"}])
    link.push_now()
    ids = {k: i for i, k in owner_q(pg, "select id, key from questions")}
    c1 = command(pg, "answer_question", {"question_id": ids["custom.ref"], "answer": "No", "scope": "company"})
    c2 = command(pg, "decline_question", {"question_id": ids["custom.why"]}, source="telegram")
    await asyncio.to_thread(settle, link, pg)
    assert command_row(pg, c1)[0] == "done" and command_row(pg, c2)[0] == "done"
    assert answers.get("custom.ref", company="Zeta Corp").answer == "No"
    assert answers.get("custom.ref") is None
    assert owner_q(pg, "select status from questions where id = %s", ids["custom.why"]) == [("declined",)]


def test_add_listing_and_duplicates(link, pg):
    url = "https://jobs.ashbyhq.com/acme/1234-5678"
    c1 = command(pg, "add_listing", {"url": url, "company": "Acme", "title": "Backend Intern"})
    settle(link, pg)
    st, result, _ = command_row(pg, c1)
    assert st == "done" and result["ats"] == "ashby"
    assert owner_q(pg, "select l.company, l.title, j.state, j.priority from jobs j join listings l on l.id = j.listing_id "
                       "where j.id = %s", result["job_id"]) == [("Acme", "Backend Intern", "queued", 50)]
    c2 = command(pg, "add_listing", {"url": url + "/application"})
    c3 = command(pg, "add_listing", {"url": "not a url"})
    settle(link, pg)
    assert command_row(pg, c2)[0] == "rejected" and "Already in the queue" in command_row(pg, c2)[2]
    assert command_row(pg, c3)[0] == "rejected"


def test_job_actions(link, pg):
    held = make_job("L6", status=states.HOLD)
    queued = make_job("L7")
    running = make_job("L8", status=states.FILLING)
    cid = command(pg, "job_action", {"job_ids": [held], "action": "retry"})
    settle(link, pg)
    assert command_row(pg, cid)[0] == "done"
    assert owner_q(pg, "select state from jobs where id = %s", held) == [("queued",)]
    cid = command(pg, "job_action", {"job_ids": [queued], "action": "pin"})
    settle(link, pg)
    assert owner_q(pg, "select priority from jobs where id = %s", queued)[0][0] >= 10_000
    cid = command(pg, "job_action", {"job_ids": [running], "action": "skip"})
    settle(link, pg)
    assert command_row(pg, cid)[0] == "rejected"
    cid = command(pg, "reorder", {"job_ids": [held, queued]})
    settle(link, pg)
    assert command_row(pg, cid)[0] == "done"


def test_worker_pause_resume_reaches_the_heartbeat(link, pg):
    command(pg, "worker", {"action": "pause"})
    settle(link, pg)
    assert db.meta_get("worker_paused") is True
    link._last_heartbeat = 0
    link.push_now()
    assert owner_q(pg, "select worker_paused from agent_status") == [(True,)]
    command(pg, "worker", {"action": "resume"})
    settle(link, pg)
    assert db.meta_get("worker_paused") is False
    assert "worker:resumed" in [r[0] for r in owner_q(pg, "select kind from events")]


# ------------------------------------------------------------------------------------------- worker control
@pytest.fixture
def plain_db(tmp_path):
    db.reset_for_tests(tmp_path / "w.db")


async def test_stop_now_holds_the_running_job(plain_db, monkeypatch):
    jid = make_job("W1", status=states.FILLING)
    started = asyncio.Event()

    async def slow_run(job, **kw):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(worker, "run_job", slow_run)
    task = asyncio.create_task(worker._run_controlled({"id": jid}))
    await asyncio.wait_for(started.wait(), 2)
    assert worker.CONTROL.request_stop("stop", "web") == jid
    out = await asyncio.wait_for(task, 5)
    assert out.status == states.HOLD
    row = db.connect().execute("SELECT status, hold_reason FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == states.HOLD and "stopped from the website" in row["hold_reason"]
    assert db.meta_get("worker_paused") is True


async def test_skip_current_marks_it_dead(plain_db, monkeypatch):
    jid = make_job("W2", status=states.FILLING)
    started = asyncio.Event()

    async def slow_run(job, **kw):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(worker, "run_job", slow_run)
    task = asyncio.create_task(worker._run_controlled({"id": jid}))
    await asyncio.wait_for(started.wait(), 2)
    worker.CONTROL.request_stop("skip", "telegram")
    out = await asyncio.wait_for(task, 5)
    assert out.status == states.DEAD
    assert db.connect().execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] == states.DEAD


def test_browser_failure_requeues_without_spending_the_attempt(plain_db):
    jid = make_job("W3", status=states.FILLING)
    with db.tx() as con:
        con.execute("UPDATE jobs SET attempts=1 WHERE id=?", (jid,))
    e = TimeoutError("BrowserType.connect_over_cdp: Timeout 15000ms exceeded.")
    assert worker.is_browser_failure(e) and not worker.is_browser_failure(ValueError("bad selector"))
    out = worker._browser_down(jid, str(e))
    assert out.status == worker.BROWSER_DOWN
    row = db.connect().execute("SELECT status, attempts, hold_reason FROM jobs WHERE id=?", (jid,)).fetchone()
    assert (row["status"], row["attempts"], row["hold_reason"]) == (states.QUEUED, 0, None)


async def test_worker_loop_backs_off_and_reports_the_outage_once(plain_db, monkeypatch):
    for i in range(3):
        make_job(f"W4{i}")
    calls = []

    async def fake_run(job):
        calls.append(job["id"])
        return worker._browser_down(int(job["id"]), "connect_over_cdp timeout")

    monkeypatch.setattr(worker, "_run_controlled", fake_run)
    monkeypatch.setattr(worker, "BROWSER_BACKOFF_S", (0.2, 0.2, 0.2, 0.2, 0.2))
    worker.CONTROL.browser_ok = None
    worker.CONTROL.browser_failures = 0
    stop = asyncio.Event()
    t = asyncio.create_task(worker.worker_loop(stop, idle_s=0.05))
    await asyncio.sleep(0.9)
    stop.set()
    worker.CONTROL.wake.set()
    await asyncio.wait_for(t, 5)
    downs = db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='browser:down'").fetchone()[0]
    holds = db.connect().execute("SELECT COUNT(*) FROM jobs WHERE status='hold'").fetchone()[0]
    assert downs == 1 and holds == 0
    assert 2 <= len(calls) <= 6          # retried after each back-off, not in a tight loop
