"""The nightly proposals pass: alias promotion, holds summary, the report, approval, and its schedule."""
import json
import time

import pytest

from goat import answers, db, nightly
from goat.config import SETTINGS


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(SETTINGS, "proposals_dir", tmp_path / "proposals")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Acme','Intern','https://x.example/1','greenhouse')")
        for j in (1, 2, 3):
            con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(?,?, 'hold','no_account',0)", (j, f"l{j}" if j == 1 else None))
    return tmp_path


def _lookup_event(job_id, question, ts=None):
    with db.tx() as con:
        con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(?,?,?,?)",
                    (job_id, int(ts or time.time()), "tool:lookup_answer", json.dumps({"args": {"question": question}, "out": {}})))


def _hold_event(job_id, reason, ts):
    with db.tx() as con:
        con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(?,?,?,?)", (job_id, int(ts), "job:hold", json.dumps({"reason": reason})))


def test_alias_promotion_needs_two_jobs_and_is_idempotent(fresh):
    answers.save_answer("defaults.us_citizen", "Yes", answer_type="choice", source="questionnaire")
    answers.record_classifier_alias("Are you a US person?", "defaults.us_citizen", 0.9)
    _lookup_event(1, "Are you a US person?")
    assert nightly.promote_aliases(min_seen=2) == 0
    _lookup_event(2, "Are you a US person?")
    _lookup_event(2, "Are you a US person?")                      # the same job twice still counts once
    assert nightly.promote_aliases(min_seen=2) == 1
    assert nightly.promote_aliases(min_seen=2) == 0
    rows = nightly.list_proposals()
    assert len(rows) == 1 and rows[0]["kind"] == "alias" and rows[0]["title"] == "are you a us person -> defaults.us_citizen"
    ev = json.loads(db.connect().execute("SELECT evidence FROM proposals").fetchone()[0])
    assert ev == {"jobs": [1, 2], "seen": 2}


def test_hold_summary_groups_by_prefix_inside_the_window(fresh):
    now = time.time()
    _hold_event(1, "credentials_unknown: the vault is locked", now - 3600)
    _hold_event(2, "credentials_unknown", now - 1800)
    _hold_event(3, "portal_error: Something went wrong", now - 600)
    _hold_event(3, "eligibility: masters only", now - 3 * 86400)      # outside the window
    s = nightly.summarize_holds(now - 86400, now)
    assert s == {"credentials_unknown": {"count": 2, "jobs": [1, 2]}, "portal_error": {"count": 1, "jobs": [3]}}
    day = nightly._day(now)
    assert nightly.record_holds_proposal(day, s, now=now) is True
    assert nightly.record_holds_proposal(day, s, now=now) is False
    row = db.connect().execute("SELECT kind, title, body FROM proposals").fetchone()
    assert row[0] == "code" and row[1] == f"holds to look at {day}" and "| credentials_unknown | 2 |" in row[2]


def test_run_nightly_writes_the_report_and_logs_an_event(fresh):
    now = time.time()
    _hold_event(1, "needs_you: SSN", now - 100)
    counts = nightly.run_nightly(now=now)
    p = SETTINGS.proposals_dir / f"{nightly._day(now)}.md"
    assert p.exists() and counts["report"] == str(p)
    text = p.read_text(encoding="utf-8")
    assert "## Alias proposals" in text and "## Holds to look at" in text and "needs_you" in text
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='nightly:done'").fetchone()[0] == 1


def test_approving_an_alias_proposal_makes_the_lookup_deterministic(fresh):
    answers.save_answer("defaults.us_citizen", "Yes", answer_type="choice", source="questionnaire")
    answers.record_classifier_alias("Are you a US person?", "defaults.us_citizen", 0.9)
    assert answers.lookup("Are you a US person?") is None            # unapproved aliases do not resolve
    _lookup_event(1, "Are you a US person?"); _lookup_event(2, "Are you a US person?")
    nightly.promote_aliases()
    pid = nightly.list_proposals()[0]["id"]
    out = nightly.apply_proposal(pid)
    assert out == {"ok": True, "status": "applied"}
    a = answers.lookup("Are you a US person?")
    assert a is not None and a.key == "defaults.us_citizen" and a.answer == "Yes"
    row = db.connect().execute("SELECT status, decided_at FROM proposals WHERE id=?", (pid,)).fetchone()
    assert row[0] == "applied" and row[1]
    assert nightly.apply_proposal(pid) == {"ok": False, "why": "already applied"}
    assert nightly.reject_proposal(pid)["ok"] is False


def test_rejecting_and_code_proposals(fresh):
    now = time.time()
    _hold_event(1, "needs_you: SSN", now - 100)
    nightly.record_holds_proposal(nightly._day(now), nightly.summarize_holds(now - 86400, now), now=now)
    pid = nightly.list_proposals()[0]["id"]
    assert nightly.apply_proposal(pid) == {"ok": True, "status": "approved"}
    assert nightly.reject_proposal(999) == {"ok": False, "why": "no such proposal"}
    _hold_event(2, "eligibility: x", now - 50)
    assert nightly.record_holds_proposal("2026-01-01", {}, now=now) is True
    pid2 = [r for r in nightly.list_proposals() if r["title"].endswith("2026-01-01")][0]["id"]
    assert nightly.reject_proposal(pid2) == {"ok": True, "status": "rejected"}


def test_due_daily_fires_once_per_day_in_the_hour():
    from goat.supervisor import due_daily

    def lt(day, hour, minute=0):
        return time.struct_time((2026, 1, 1, hour, minute, 0, 0, day, 0))

    assert due_daily(2, None, lt(10, 2)) is True
    assert due_daily(2, 10, lt(10, 2, 30)) is False
    assert due_daily(2, 10, lt(10, 1, 59)) is False
    assert due_daily(2, 10, lt(11, 2, 30)) is True
    assert due_daily(18, None, lt(10, 17)) is False


def test_cli_proposals_lists_and_approves(fresh, capsys):
    from goat import cli
    answers.save_answer("defaults.us_citizen", "Yes", answer_type="choice", source="questionnaire")
    answers.record_classifier_alias("Are you a US person?", "defaults.us_citizen", 0.9)
    _lookup_event(1, "Are you a US person?"); _lookup_event(2, "Are you a US person?")
    cli.main(["proposals", "--run-nightly"])
    out = capsys.readouterr().out
    assert "alias  proposed  are you a us person -> defaults.us_citizen" in out
    pid = nightly.list_proposals(status="proposed")[0]["id"]
    cli.main(["proposals", "--approve", str(pid)])
    out = capsys.readouterr().out
    assert "applied" in out
    assert answers.lookup("Are you a US person?").key == "defaults.us_citizen"
