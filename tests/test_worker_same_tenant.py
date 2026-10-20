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
