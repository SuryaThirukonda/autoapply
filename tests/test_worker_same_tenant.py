"""Q2 (2026-10-19): a second job on a portal account whose sign-in just failed is held without another try."""
from goat import db, states
from goat.worker import same_tenant_credentials_hold


def _job(con, jid, url, status="queued", reason=None, updated=1000):
    con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES(?,?,?,?, 'workday')", (f"L{jid}", "Q2", "Intern", url))
    con.execute("INSERT INTO jobs(id,listing_id,status,lane,hold_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (jid, f"L{jid}", status, "account", reason, 1, updated))


def test_same_tenant_credentials_hold(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        _job(con, 82, "https://q2ebanking.wd5.myworkdayjobs.com/Q2/job/A", states.HOLD, "credentials_unknown", 5000)
        _job(con, 83, "https://q2ebanking.wd5.myworkdayjobs.com/Q2/job/B")
        _job(con, 84, "https://other.wd5.myworkdayjobs.com/x/job/C")
        _job(con, 85, "https://boards.greenhouse.io/q2/jobs/1")
    con = db.connect()
    row = lambda i: dict(con.execute("SELECT j.*, l.url FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (i,)).fetchone())
    assert same_tenant_credentials_hold(row(83), now=6000) == 82
    assert same_tenant_credentials_hold(row(84), now=6000) is None
    assert same_tenant_credentials_hold(row(85), now=6000) is None
    assert same_tenant_credentials_hold(row(83), now=5000 + 13 * 3600) is None      # older than 12 hours



def test_a_posting_already_in_the_ledger_is_held_before_any_run(tmp_path):
    """Ryan (2026-10-22): requisition R0020808 on two career sites; the second job opened Workday to read 'You applied'."""
    from goat import db, ledger
    from goat.worker import ledger_hold
    db.reset_for_tests(tmp_path / "t.db")
    ledger.record_application(None, "Ryan", "Summer 2027 Intern, Application Engineering",
                              "https://ryan.wd1.myworkdayjobs.com/RyanCareers/job/Plano/Summer-2027-Intern--Application-Engineering_R0020808-2", "agent")
    twin = {"company": "Ryan", "title": "Application Engineering Intern",
            "url": "https://ryan.wd1.myworkdayjobs.com/Students-Graduates/job/Plano/Summer-2027-Intern--Application-Engineering_R0020808-1"}
    assert ledger_hold(twin).startswith("already_applied")
    assert ledger_hold({**twin, "url": twin["url"].replace("R0020808", "R0099999"), "title": "Data Intern"}) is None



def test_a_portal_error_retry_never_queues_a_job_past_its_attempts(tmp_path):
    """GM Financial (2026-10-22): requeued at 3 attempts (retry limit 4, claim limit 3) and never claimed."""
    from goat import db, states, worker
    from goat.config import SETTINGS
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','GM','Intern','https://x.oraclecloud.com/a','oracle')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,attempts,hold_reason,updated_at) VALUES(1,'L1',?,?,?,?,?)",
                    (states.HOLD, "account", SETTINGS.budgets.max_attempts, "portal_error: tiles", 1))
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L2','DP','Intern','https://y.oraclecloud.com/a','oracle')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,attempts,hold_reason,updated_at) VALUES(2,'L2',?,?,?,?,?)",
                    (states.HOLD, "account", 1, "portal_error: tiles", 1))
    assert worker.requeue_portal_errors(now=10 ** 9) == 1
    st = {r["id"]: r["status"] for r in db.connect().execute("SELECT id, status FROM jobs")}
    assert st == {1: states.HOLD, 2: states.QUEUED}
