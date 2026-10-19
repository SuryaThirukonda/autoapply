"""Backlog sweep tooling: the batch requeue by hold reason, the watchdog drain of old browser-failure holds
(idle-only, once per job, attempt not counted) and the holds report."""
import json

import pytest

from goat import cli, db, states, worker

CDP = "error: TimeoutError: BrowserType.connect_over_cdp: Timeout 15000ms exceeded."


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(worker.CONTROL, "browser_ok", True)
    rows = [(1, "workday", "hold", CDP, 1, 1000), (2, "workday", "hold", CDP, 1, 1000), (3, "greenhouse", "hold", CDP, 1, 1000),
            (4, "workday", "hold", "portal_error: Something went wrong", 1, 1000), (5, "workday", "submitted", CDP, 1, 1000),
            (6, "ashby", "confirmed", CDP, 1, 1000), (7, "workday", "hold", "error: RuntimeError: model request failed: Error code: 400", 1, 1000)]
    with db.tx() as con:
        for jid, ats, status, reason, attempts, upd in rows:
            con.execute("INSERT INTO listings(id,company,title,url,ats,date_posted) VALUES(?,?,?,?,?,?)",
                        (f"l{jid}", f"Co{jid}", f"Intern {jid}", f"https://x.example/{jid}", ats, 100 - jid))
            con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority,attempts,hold_reason,updated_at) VALUES(?,?,?,?,0,?,?,?)",
                        (jid, f"l{jid}", status, "account", attempts, reason, upd))
    return tmp_path


def _job(jid):
    return dict(db.connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def test_browser_failure_text_marks():
    assert worker.browser_failure_text(CDP)
    assert worker.browser_failure_text("error: Error: Browser has been closed")
    assert not worker.browser_failure_text("error: RuntimeError: model request failed: Error code: 400")
    assert worker.is_browser_failure(TimeoutError("BrowserType.connect_over_cdp: Timeout 15000ms exceeded."))


def test_requeue_where_dry_run_changes_nothing(seeded):
    out = cli.requeue_where("error: TimeoutError: BrowserType.connect_over_cdp", dry_run=True)
    assert sorted(c["id"] for c in out["candidates"]) == [1, 2, 3] and out["changed"] == []
    assert all(_job(j)["status"] == "hold" for j in (1, 2, 3))
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='job:requeued'").fetchone()[0] == 0


def test_requeue_where_filters_by_ats_and_limit(seeded):
    out = cli.requeue_where("error: TimeoutError: BrowserType.connect_over_cdp", ats="workday", limit=1)
    assert len(out["changed"]) == 1
    j = _job(out["changed"][0])
    assert j["status"] == states.QUEUED and j["attempts"] == 0 and j["priority"] == 5 and j["hold_reason"] is None
    ev = db.connect().execute("SELECT detail FROM events WHERE kind='job:requeued' AND job_id=?", (j["id"],)).fetchone()[0]
    assert json.loads(ev)["by"] == "operator:requeue-where"
    assert sum(1 for jid in (1, 2, 3) if _job(jid)["status"] == "hold") == 2


def test_requeue_where_never_touches_submitted_or_confirmed(seeded):
    before = (_job(5), _job(6))
    out = cli.requeue_where("error: TimeoutError", limit=50)
    assert 5 not in out["changed"] and 6 not in out["changed"]
    assert (_job(5), _job(6)) == before


def test_watchdog_drains_browser_error_holds_once_when_idle(seeded):
    assert worker.requeue_browser_error_holds(now=1000 + 3599) == 0            # not old enough
    assert worker.requeue_browser_error_holds(now=1000 + 3601) == 1
    first = [jid for jid in (1, 2, 3) if _job(jid)["status"] == states.QUEUED]
    assert len(first) == 1
    j = _job(first[0])
    assert j["attempts"] == 0 and j["tab_target_id"] is None
    ev = db.connect().execute("SELECT detail FROM events WHERE kind='job:requeued' AND job_id=?", (j["id"],)).fetchone()[0]
    assert "browser_error_hold" in ev
    assert worker.requeue_browser_error_holds(now=1000 + 3601) == 0            # a job is queued: idle-only
    with db.tx() as con:
        con.execute("UPDATE jobs SET status='hold', hold_reason=?, updated_at=1000 WHERE id=?", (CDP, j["id"]))
    assert worker.requeue_browser_error_holds(now=1000 + 3601) == 1            # another one, never the same job twice
    assert _job(j["id"])["status"] == "hold"
    with db.tx() as con:
        con.execute("UPDATE jobs SET status='hold', hold_reason=?, updated_at=1000 WHERE status='queued'", (CDP,))
    assert worker.requeue_browser_error_holds(now=1000 + 3601, limit=0) == 0
    worker.CONTROL.browser_ok = False
    assert worker.requeue_browser_error_holds(now=1000 + 3601) == 0
    worker.CONTROL.browser_ok = True
    db.meta_set("worker_paused", True)
    assert worker.requeue_browser_error_holds(now=1000 + 3601) == 0


def test_watchdog_rule_ignores_non_browser_errors_and_submitted(seeded):
    for _ in range(5):
        worker.requeue_browser_error_holds(now=1000 + 3601)
        with db.tx() as con:
            con.execute("UPDATE jobs SET status='dead' WHERE status='queued'")
    assert _job(4)["status"] == "hold" and _job(7)["status"] == "hold"
    assert _job(5)["status"] == "submitted" and _job(6)["status"] == "confirmed"


def test_holds_report_groups_reasons_and_recent_outcomes(seeded, capsys):
    with db.tx() as con:
        con.execute("UPDATE jobs SET updated_at=? WHERE id=1", (db.now() - 60,))
    rep = cli.holds_report(since_s=7200)
    by = {r["reason"]: r["n"] for r in rep["holds_by_reason"]}
    assert by["error"] == 4 and by["portal_error"] == 1
    assert rep["top_reason"] == "error" and {r["ats"]: r["n"] for r in rep["top_reason_by_ats"]} == {"workday": 3, "greenhouse": 1}
    assert rep["error_holds"][0]["n"] == 3 and rep["error_holds"][0]["wording"].startswith("error: TimeoutError")
    assert [r["id"] for r in rep["recent"]] == [1]
    cli.main(["holds-report", "--since-hours", "2"])
    out = capsys.readouterr().out
    assert "error: TimeoutError" in out and "portal_error" in out and "#1 " in out and "applications in the ledger: 0" in out
    cli.main(["requeue-where", "--reason-prefix", "error: TimeoutError", "--ats", "greenhouse", "--dry-run"])
    assert "would requeue" in capsys.readouterr().out
