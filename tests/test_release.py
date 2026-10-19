"""Per-listing release: the one way a draft leaves shadow mode is `goat release N` (or the chat / dashboard
equivalents), refused for listings already in the ledger."""
import json

import pytest

from goat import cli, db, ledger, states
from goat.cloud import job_action
from goat.config import SETTINGS


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(SETTINGS, "mode", "shadow")
    monkeypatch.setattr(SETTINGS, "live_portals", [])
    with db.tx() as con:
        for jid, status in ((1, "hold"), (2, "submitted"), (3, "filling"), (4, "hold")):
            con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES(?,?,?,?,?)", (f"l{jid}", f"Co{jid}", "Intern", f"https://x.example/{jid}", "workday"))
            con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority,attempts,hold_reason) VALUES(?,?,?,?,0,1,?)",
                        (jid, f"l{jid}", status, "account", "shadow_mode: gate passed" if status == "hold" else None))
    return tmp_path


def _job(jid):
    return dict(db.connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def test_release_sets_the_flag_requeues_first_in_line_and_logs(seeded):
    out = job_action([1], "release", "operator")
    assert out["changed"] == [1] and out["refused"] == []
    j = _job(1)
    assert j["cleared_live"] == 1 and j["status"] == states.QUEUED and j["priority"] >= 5000 and j["hold_reason"] is None and j["attempts"] == 0
    ev = json.loads(db.connect().execute("SELECT detail FROM events WHERE kind='job:released' AND job_id=1").fetchone()[0])
    assert ev["by"] == "operator"


def test_release_refuses_a_listing_already_in_the_ledger(seeded):
    ledger.record_application("l4", "Co4", "Intern", "https://x.example/4", "surya:manual")
    out = job_action([4], "release", "operator")
    assert out["changed"] == [] and "ledger" in out["refused"][0]["why"]
    assert _job(4)["cleared_live"] == 0 and _job(4)["status"] == "hold"


def test_release_refuses_running_and_submitted_jobs(seeded):
    out = job_action([2, 3], "release", "operator")
    assert out["changed"] == [] and {r["job_id"]: r["why"] for r in out["refused"]} == {2: "already submitted", 3: "running now: use Stop or Skip on the Now page"}


def test_unrelease_clears_the_flag_without_touching_the_status(seeded):
    job_action([1], "release", "operator")
    out = job_action([1], "unrelease", "operator")
    assert out["changed"] == [1]
    j = _job(1)
    assert j["cleared_live"] == 0 and j["status"] == states.QUEUED
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='job:unreleased'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_gate_passes_live_for_a_cleared_job_in_shadow_mode(seeded):
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    from goat.gate import run_gate
    page = seeded / "review.html"
    page.write_text("<html><body><h1>Review</h1><p>Everything looks right.</p><button>Submit</button></body></html>", encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        text = await ex.page_text(2000)
        base = {"id": 1, "company": "Co1", "title": "Intern", "url": "https://x.example/1", "listing_id": "l1"}
        assert run_gate(job={**base, "cleared_live": 0}, snapshot=snap, page_text=text, expected_resume=None, free_text_answers=[], portal="greenhouse").result == "pass_shadow"
        assert run_gate(job={**base, "cleared_live": 1}, snapshot=snap, page_text=text, expected_resume=None, free_text_answers=[], portal="greenhouse").result == "pass_live"
    finally:
        await ex.close(close_browser=True)


def test_cli_release_and_undo(seeded, capsys):
    cli.main(["release", "1"])
    assert _job(1)["cleared_live"] == 1 and '"changed": [\n    1' in capsys.readouterr().out.replace("\r", "")
    cli.main(["release", "1", "--undo"])
    assert _job(1)["cleared_live"] == 0
    cli.main(["list", "--status", "queued"])
    assert "LIVE" not in capsys.readouterr().out
    cli.main(["release", "1"]); capsys.readouterr()
    cli.main(["list", "--status", "queued"])
    assert "LIVE" in capsys.readouterr().out


def test_a_migrated_database_gets_the_column(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    con = sqlite3.connect(str(p))
    con.executescript(db.SCHEMA)                        # today's tables, none of the migrated columns
    assert "cleared_live" not in {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    con.execute("INSERT INTO jobs(listing_id,status) VALUES('l1','hold')")
    con.commit(); con.close()
    db.reset_for_tests(p)
    cols = {r[1] for r in db.connect().execute("PRAGMA table_info(jobs)")}
    assert "cleared_live" in cols
    assert db.connect().execute("SELECT cleared_live FROM jobs").fetchone()[0] == 0


def test_dashboard_action_release_goes_through_the_same_guard(seeded):
    from goat.dashboard import action
    assert action(1, "release", {})["ok"] is True and _job(1)["cleared_live"] == 1
    assert action(2, "release", {})["ok"] is False


def _queued_with_decisions(cleared: int, tab: str | None):
    from goat import records
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('k1','Sierra','Intern','https://jobs.ashbyhq.com/Sierra/1','ashby')")
        cur = con.execute("INSERT INTO jobs(listing_id,status,lane,priority,cleared_live,tab_target_id,created_at,updated_at) "
                          "VALUES('k1',?,'no_account',5000,?,?,1,1)", (states.QUEUED, cleared, tab))
        jid = cur.lastrowid
    records.record_decision(jid, 1, "Which Sierra office would you prefer?", "New York", "standing:availability.location_ranking", "e30")
    return jid


def test_a_released_draft_with_its_tab_keeps_the_decisions_he_approved(seeded, monkeypatch, tmp_path):
    from goat import records, worker
    monkeypatch.setattr(SETTINGS, "records_dir", tmp_path / "records")
    jid = _queued_with_decisions(1, "TAB123")
    job = worker.claim_next()
    assert job["id"] == jid and worker.keeps_decisions(job)
    assert [d["value"] for d in records.decisions(jid)] == ["New York"]
    assert not list(records.job_dir(jid).glob("decisions_attempt*.md"))


def test_a_released_draft_without_a_tab_and_a_plain_requeue_archive_as_before(seeded, monkeypatch, tmp_path):
    from goat import records, worker
    monkeypatch.setattr(SETTINGS, "records_dir", tmp_path / "records")
    jid = _queued_with_decisions(1, None)
    job = worker.claim_next()
    assert job["id"] == jid and not worker.keeps_decisions(job)
    assert records.decisions(jid) == [] and list(records.job_dir(jid).glob("decisions_attempt*.md"))
    assert not worker.keeps_decisions({"cleared_live": 0, "tab_target_id": "T"})


def test_archive_decisions_moves_the_rows_into_the_attempt_file(seeded, monkeypatch, tmp_path):
    from goat import records, worker
    monkeypatch.setattr(SETTINGS, "records_dir", tmp_path / "records")
    jid = _queued_with_decisions(0, None)
    with db.tx() as con:
        assert worker.archive_decisions(con, jid) == 1
        assert worker.archive_decisions(con, jid) == 0
    text = (records.job_dir(jid) / "decisions_attempt1.md").read_text(encoding="utf-8")
    assert "Which Sierra office would you prefer?" in text and "New York" in text and records.decisions(jid) == []
