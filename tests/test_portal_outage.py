"""Workday outage (2026-10-24, 01:50-02:20): 'Workday is currently unavailable. We are experiencing a service interruption.'
held eighteen Workday jobs in half an hour, each claim spending one of its three attempts. While two or more outage holds of
one portal fall within 45 minutes, its jobs are not claimed; an outage hold gives its attempt back when it is requeued."""
import json

from goat import db, states
from goat.worker import OUTAGE_PAUSE_S, PORTAL_RETRY_AFTER_S, claim_next, outage_portals, requeue_portal_errors

OUTAGE = "portal_error: English Workday is currently unavailable. We are experiencing a service interruption. Your service"


def _job(con, n, ats, status=states.QUEUED, posted=1):
    con.execute("INSERT INTO listings(id,company,title,url,ats,date_posted) VALUES(?,?,?,?,?,?)",
                (f"L{n}", f"Co{n}", "Software Intern", f"https://x{n}.example/jobs/1", ats, posted))
    return con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES(?,?,'no_account',1,1)",
                       (f"L{n}", status)).lastrowid


def _hold(con, job_id, ts, reason=OUTAGE):
    con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=?, attempts=1 WHERE id=?", (states.HOLD, reason, ts, job_id))
    con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(?,?,?,?)", (job_id, ts, "job:hold", json.dumps({"reason": reason})))


def test_an_outage_pauses_that_portal_and_only_that_portal(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    now = db.now()
    with db.tx() as con:
        a, b = _job(con, 1, "workday"), _job(con, 2, "workday")
        _hold(con, a, now - 600)
        _hold(con, b, now - 300)
        wd = _job(con, 3, "workday", posted=9)          # the newest posting: claimed first were Workday not paused
        gh = _job(con, 4, "greenhouse", posted=1)
    assert outage_portals(now=now) == {"workday"}
    got = claim_next()
    assert got and got["id"] == gh, got
    assert outage_portals(now=now + OUTAGE_PAUSE_S + 1) == set()


def test_one_outage_hold_is_no_pause(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    now = db.now()
    with db.tx() as con:
        _hold(con, _job(con, 1, "workday"), now - 60)
    assert outage_portals(now=now) == set()


def test_an_outage_hold_gives_its_attempt_back_when_requeued(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        a = _job(con, 1, "workday")
        _hold(con, a, 1000)
        con.execute("UPDATE jobs SET attempts=3 WHERE id=?", (a,))          # spent by the outage
        b = _job(con, 2, "workday")
        _hold(con, b, 1000, reason="portal_error: Something went wrong. Error Code: VPS|abc")
    assert requeue_portal_errors(now=1000 + PORTAL_RETRY_AFTER_S + 1) == 2
    rows = dict(db.connect().execute("SELECT id, attempts FROM jobs").fetchall())
    assert rows[a] == 2 and rows[b] == 1
