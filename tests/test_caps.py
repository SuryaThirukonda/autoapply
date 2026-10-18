"""Board application caps from config/triage_rules.json: Waymo 5 per 60 days, Harvey 4 per 30 days, Scale AI
90 days before reapplying to the same role. A capped job is held until the window opens, then requeued."""
from __future__ import annotations

from goat import caps, db, ledger, states

DAY = 86400


def _apply(company: str, title: str, days_ago: int, n: int = 1, now: int = 2_000_000_000):
    for i in range(n):
        ledger.record_application(None, company, f"{title} {i}" if n > 1 else title,
                                  f"https://jobs.example.com/{company}/{title}/{days_ago}/{i}", "goat",
                                  submitted_at=now - days_ago * DAY)


def test_count_caps(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    now = 2_000_000_000
    _apply("Waymo", "Intern", 10, n=4, now=now)
    assert caps.check("Waymo", "Software Engineering Intern", now=now) is None          # 4 of 5
    _apply("Waymo", "Intern", 70, n=3, now=now)                                          # outside the 60-day window
    assert caps.check("Waymo", "Software Engineering Intern", now=now) is None
    _apply("Waymo LLC", "Other Intern", 5, now=now)
    hit = caps.check("Waymo", "Software Engineering Intern", now=now)
    assert hit and hit[0].startswith("application_cap: Waymo accepts 5") and hit[1] == now - 10 * DAY + 60 * DAY


def test_same_role_wait(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    now = 2_000_000_000
    _apply("Scale AI", "Software Engineer Intern (Summer 2027)", 30, now=now)
    hit = caps.check("Scale AI", "Software Engineer Intern", now=now)
    assert hit and "90 days" in hit[0] and hit[1] == now - 30 * DAY + 90 * DAY
    assert caps.check("Scale AI", "Machine Learning Research Intern", now=now) is None
    assert caps.check("Scale Systems", "Software Engineer Intern", now=now) is None     # a different company


def test_a_capped_job_is_held_then_requeued_when_the_window_opens(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    now = db.now()
    _apply("Harvey", "Intern", 3, n=4, now=now)
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('H1','Harvey','Software Engineer Intern','https://jobs.ashbyhq.com/harvey/1','ashby')")
        jid = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('H1','filling','no_account',1,1)").lastrowid
    reason = caps.hold_if_capped({"id": jid, "company": "Harvey", "title": "Software Engineer Intern", "listing_id": "H1"})
    assert reason and reason.startswith("application_cap")
    with db.tx() as con:
        con.execute("UPDATE jobs SET status=?, hold_reason=? WHERE id=?", (states.HOLD, reason, jid))
    assert caps.requeue_reopened(now=now) == 0
    assert caps.requeue_reopened(now=now + 31 * DAY) == 1
    assert db.connect().execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] == states.QUEUED
