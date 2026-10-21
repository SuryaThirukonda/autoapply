"""Queue order (Surya, 2026-10-21): newest posting first, explicit choices (release, pin, drag order, answered question)
before that."""
import time

import pytest

from goat import cloud, db, states, worker
from goat.config import SETTINGS
from tests.test_answer_rules import store  # noqa: F401 - fixture

DAY = 86400


def _job(con, jid, posted, priority=0, first_seen=None, cleared=0, status=None):
    con.execute("INSERT INTO listings(id,company,title,url,ats,date_posted,first_seen) VALUES(?,?,?,?,?,?,?)",
                (f"L{jid}", f"Co{jid}", "Intern", f"https://boards.greenhouse.io/c{jid}/jobs/{jid}", "greenhouse", posted, first_seen))
    con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority,cleared_live,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (jid, f"L{jid}", status or states.QUEUED, "no_account", priority, cleared, 1, 1))


def _claims(n):
    out = []
    for _ in range(n):
        j = worker.claim_next()
        if not j:
            break
        out.append(int(j["id"]))
    return out


def test_the_newest_posting_is_claimed_first_whatever_its_old_priority(store, monkeypatch):
    monkeypatch.setattr(SETTINGS, "lanes_enabled", ["no_account"])
    now = int(time.time())
    with db.tx() as con:
        _job(con, 1, now - 30 * DAY, priority=900)        # old posting with a big leftover bump
        _job(con, 2, now - 1 * DAY)                       # posted yesterday
        _job(con, 3, now - 5 * DAY, priority=55)
        _job(con, 4, None, first_seen=now - 3 * DAY)      # no posting date: the day goat first saw it
        _job(con, 5, now - 2 * 3600)                      # posted two hours ago
    assert _claims(5) == [5, 2, 4, 3, 1]


def test_explicit_choices_stay_ahead_of_the_posting_order(store, monkeypatch):
    monkeypatch.setattr(SETTINGS, "lanes_enabled", ["no_account"])
    now = int(time.time())
    with db.tx() as con:
        _job(con, 1, now - 3600)                                         # fresh posting
        _job(con, 2, now - 40 * DAY, priority=cloud.PIN_PRIORITY)        # pinned
        _job(con, 3, now - 20 * DAY, priority=5005, cleared=1)           # released by Surya
        _job(con, 4, now - 10 * DAY, priority=cloud.ORDER_BASE + 1)      # his drag order on the website
    assert _claims(4) == [2, 3, 4, 1]


def test_ranks_are_written_so_every_view_shows_the_claim_order(store):
    now = int(time.time())
    with db.tx() as con:
        _job(con, 1, now - 9 * DAY)
        _job(con, 2, now - 1 * DAY)
        _job(con, 3, now - 4 * DAY, status=states.HOLD, priority=7)      # not queued: left alone
        assert worker.rank_queue(con, now) == 2
    con = db.connect()
    pr = {r["id"]: r["priority"] for r in con.execute("SELECT id, priority FROM jobs")}
    assert pr[2] == worker.AUTO_TOP and pr[1] == worker.AUTO_TOP - 1 and pr[3] == 7
    assert worker.AUTO_TOP + 10 < worker.EXPLICIT_PRIORITY      # a requeue's +5 / an answered +10 stays in the auto band
    assert worker.EXPLICIT_PRIORITY == cloud.ORDER_BASE


def test_an_answered_question_resumes_before_the_posting_order(store, monkeypatch):
    from goat import answers
    monkeypatch.setattr(SETTINGS, "lanes_enabled", ["no_account"])
    now = int(time.time())
    with db.tx() as con:
        _job(con, 1, now - 3600)
        _job(con, 2, now - 30 * DAY, status=states.HOLD)
        con.execute("UPDATE jobs SET blocked_on_question_key='q.team' WHERE id=2")
    answers.save_answer("q.team", "Backend", answer_type="text", source="telegram")
    assert _claims(2) == [2, 1]
