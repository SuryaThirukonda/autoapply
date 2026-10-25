"""2026-10-24 02:44, 2026-10-25 08:49 and 13:54: 'model request failed: exception: access violation writing 0x48' (a native
crash in the model client) left the process broken; the next job hung until the agent loop's 20-minute watchdog restarted
goat. The interrupted job goes back to the queue and the process exits at once."""
from goat import db, worker


def test_native_crash_is_recognised():
    assert worker.native_crash("RuntimeError: model request failed: exception: access violation writing 0x0000000000000048")
    assert not worker.native_crash("model request failed: Connection error.")
    assert not worker.native_crash("portal_error: Something went wrong")


def test_the_crashed_job_goes_back_with_its_attempt(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','DTCC','Intern','https://x.example/1','oracle')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority,attempts,hold_reason) VALUES(1,'l1','hold','account',0,1,'error: x')")
    worker.requeue_after_native_crash(1, "access violation writing 0x48")
    row = db.connect().execute("SELECT status, attempts, hold_reason FROM jobs WHERE id=1").fetchone()
    assert (row[0], row[1], row[2]) == ("queued", 0, None)
    assert db.connect().execute("SELECT 1 FROM events WHERE kind='job:requeued' AND detail LIKE '%native_crash%'").fetchone()
