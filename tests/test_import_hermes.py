import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from goat import answers, db, ledger, states

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location("import_hermes", ROOT / "scripts" / "import_hermes.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["import_hermes"] = mod
    spec.loader.exec_module(mod)
    return mod


HERMES_SCHEMA = """
CREATE TABLE listings (
  id TEXT PRIMARY KEY, company TEXT, title TEXT, url TEXT, category TEXT,
  locations TEXT, terms TEXT, degrees TEXT, sponsorship TEXT, source TEXT,
  date_posted INTEGER, date_updated INTEGER, first_seen INTEGER, updated_at INTEGER,
  ats TEXT, tier TEXT, variant TEXT, status TEXT, reason TEXT, note TEXT,
  claimed_at INTEGER, finished_at INTEGER, gate_passed_at INTEGER, notified INTEGER DEFAULT 0);
CREATE TABLE answers (n INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT, ts INTEGER,
  question TEXT, answer TEXT, source TEXT);
CREATE TABLE applied (key TEXT PRIMARY KEY, company TEXT, title TEXT, url TEXT, origin TEXT, added_at INTEGER);
CREATE TABLE events (n INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT, ts INTEGER, kind TEXT, detail TEXT);
"""

SUBMITTED_URL = "https://job-boards.greenhouse.io/instead/jobs/7761472003"
QUEUED_URL = "https://job-boards.greenhouse.io/acme/jobs/5551234"
OLD_QUEUED_URL = "https://jobs.ashbyhq.com/oldco/11111111-2222-4333-8444-555555555555/application"
WORKDAY_URL = "https://x.wd1.myworkdayjobs.com/ext/job/Austin-TX/Intern_R1"
OTHER_URL = "https://careers.example.com/jobs/1"


def _listing(id_, company, title, url, status, *, ats, date_posted, reason="", tier="A", variant="swe"):
    return (id_, company, title, url, "Software", '["Austin, TX"]', '["Summer 2027"]', '["Bachelor\'s"]',
            "Other", "Simplify", date_posted, date_posted, 1791665647, 1792000000, ats, tier, variant,
            status, reason, None, None, 1792000100, None, 0)


@pytest.fixture
def hermes_dir(tmp_path):
    d = tmp_path / "hermes"
    (d / "data").mkdir(parents=True)
    con = sqlite3.connect(str(d / "data" / "queue.db"))
    con.executescript(HERMES_SCHEMA)
    con.executemany("INSERT INTO listings VALUES (" + ",".join("?" * 24) + ")", [
        _listing("sub-1", "Instead", "Software Engineer Intern", SUBMITTED_URL, "submitted",
                 ats="greenhouse", date_posted=1780478009),
        _listing("q-new", "Acme", "Software Engineer Intern", QUEUED_URL, "queued",
                 ats="greenhouse", date_posted=1792000000),
        _listing("q-old", "OldCo", "Backend Intern", OLD_QUEUED_URL, "hold",
                 ats="ashby", date_posted=1788000000, reason="login_or_2fa_wall: stale"),
        _listing("q-wd", "Mega", "SWE Intern", WORKDAY_URL, "queued", ats="workday", date_posted=1791000000),
        _listing("q-other", "Tiny", "SWE Intern", OTHER_URL, "queued", ats="other", date_posted=1791500000),
        _listing("base-1", "Base", "Data Intern", "https://boards.greenhouse.io/base/jobs/1", "baseline",
                 ats="greenhouse", date_posted=1790000000, reason="would_be:queued:"),
        _listing("rej-1", "Rej", "Quant Intern", "https://boards.greenhouse.io/rej/jobs/2", "rejected",
                 ats="greenhouse", date_posted=1790000000, reason="title:quant"),
        _listing("closed-1", "Gone", "SWE Intern", "https://boards.greenhouse.io/gone/jobs/3", "closed",
                 ats="greenhouse", date_posted=1790000000),
    ])
    con.executemany("INSERT INTO applied VALUES (?,?,?,?,?,?)", [
        ("u:fellowship.mlh.io", "MLH Fellowship", "Software Engineering Fellow (Fall A 2026)",
         "https://fellowship.mlh.io/", "tracker", 1791667575),
        ("c:mlh fellowship|software engineering fellow fall a 2026", "MLH Fellowship",
         "Software Engineering Fellow (Fall A 2026)", "https://fellowship.mlh.io/", "tracker", 1791667575),
        ("c:pylon|software engineer intern", "Pylon", "Software Engineer, Intern", "", "tracker", 1791667575),
    ])
    con.executemany("INSERT INTO answers(listing_id,ts,question,answer,source) VALUES (?,?,?,?,?)", [
        ("sub-1", 1791900000, "Do you know anyone who currently works for SageSure?", "No", "standing"),
        ("q-new", 1791900001, "Do you know anyone who currently works for SageSure?", "No", "standing"),
        ("sub-1", 1791900002, "Field of study", "Computer Science", "standing"),
        ("sub-1", 1791900003, "Phone", "(555) 010-0100", "standing"),
        ("q-new", 1791900004, "Phone", "5550100100", "standing"),           # inconsistent -> skipped
        ("sub-1", 1791900005, "Portal password", "Hunter2Hunter2Hunter2x9", "standing"),  # secret -> skipped
        ("sub-1", 1791900006, "Degree", "Bachelor's", "simplify"),           # simplify source -> ignored
        ("sub-1", 1791900007, "Why do you want to work here?", "Because of the mission.", "generated"),
        # two distinct questions whose first 60 normalized chars coincide
        ("sub-1", 1791900008, "Work History - Rathinam Group / Texas A&M Department of Mechanical Engineering - Employer Name",
         "Rathinam Group", "standing"),
        ("sub-1", 1791900009, "Work History - Rathinam Group / Texas A&M Department of Mechanical Engineering - Job Title",
         "Research Intern", "standing"),
    ])
    con.executemany("INSERT INTO events(listing_id,ts,kind,detail) VALUES (?,?,?,?)", [
        ("sub-1", 1791900000, "status:queued", "queued by operator"),
        ("sub-1", 1791900100, "status:submitted", "submitted ok"),
        ("q-new", 1791900200, "status:hold", "login_or_2fa_wall"),
    ])
    con.commit()
    con.close()
    return d


@pytest.fixture
def goat_db(tmp_path):
    db.reset_for_tests(tmp_path / "goat.db")
    yield
    db.reset_for_tests(tmp_path / "goat2.db")


def test_import(goat_db, hermes_dir):
    mod = _load_script()
    summary = mod.run_import(hermes_dir)
    con = db.connect()

    # listings: every row landed with the mapped triage status
    status = {r["id"]: r["triage_status"] for r in con.execute("SELECT id, triage_status FROM listings")}
    assert status == {"sub-1": "submitted", "q-new": "queued", "q-old": "queued", "q-wd": "queued",
                      "q-other": "manual", "base-1": "baseline", "rej-1": "rejected", "closed-1": "closed"}
    assert con.execute("SELECT active FROM listings WHERE id='closed-1'").fetchone()[0] == 0
    assert summary["listings"] == 8

    # jobs: queued/hold -> QUEUED with lane, newest posting at priority 0; no lane -> no job
    jobs = {r["listing_id"]: r for r in con.execute("SELECT * FROM jobs")}
    assert set(jobs) == {"q-new", "q-old", "q-wd"}
    assert jobs["q-new"]["status"] == states.QUEUED and jobs["q-new"]["lane"] == "no_account"
    assert jobs["q-new"]["priority"] == 0 and jobs["q-old"]["priority"] < jobs["q-wd"]["priority"] < 0
    assert jobs["q-old"]["hold_reason"] is None and jobs["q-old"]["attempts"] == 0
    assert jobs["q-wd"]["lane"] == "account" and jobs["q-new"]["created_at"] == 1791665647
    assert jobs["q-new"]["apply_url"] == QUEUED_URL and jobs["q-new"]["resume_variant"] == "swe"
    assert summary["jobs_created"] == 3 and summary["jobs_manual_no_lane"] == 1

    # ledger: the submitted listing and every applied key
    assert ledger.already_applied("Instead", "Software Engineer Intern", SUBMITTED_URL)
    assert ledger.already_applied("Pylon", "Software Engineer, Intern", "")
    assert ledger.already_applied("MLH Fellowship", "Software Engineering Fellow (Fall A 2026)", "https://fellowship.mlh.io/")
    assert not ledger.already_applied("Acme", "Software Engineer Intern", QUEUED_URL)
    keys = {r["key"] for r in con.execute("SELECT key FROM applied_keys")}
    assert {"u:fellowship.mlh.io", "c:mlh fellowship|software engineering fellow fall a 2026",
            "c:pylon|software engineer intern", "u:job-boards.greenhouse.io/instead/jobs/7761472003"} <= keys
    app = con.execute("SELECT * FROM applications WHERE listing_id='sub-1'").fetchone()
    assert app["origin"] == "hermes:submitted"
    assert con.execute("SELECT count(*) FROM applications").fetchone()[0] == 3

    # answers: consistent standing rows are live and resolvable by question text
    hit = answers.lookup("Do you know anyone who currently works for SageSure?")
    assert hit is not None and hit.answer == "No" and hit.source == "hermes"
    assert hit.key == "hermes.do_you_know_anyone_who_currently_works_for_sagesure"
    assert answers.lookup("Field of study").answer == "Computer Science"
    assert answers.lookup("Phone") is None  # inconsistent across Hermes rows
    assert not any(k.startswith("hermes.portal_password") for k in answers.all_keys())
    assert not any("degree" in k for k in answers.all_keys() if k.startswith("hermes."))
    emp = answers.lookup("Work History - Rathinam Group / Texas A&M Department of Mechanical Engineering - Employer Name")
    job = answers.lookup("Work History - Rathinam Group / Texas A&M Department of Mechanical Engineering - Job Title")
    assert emp.answer == "Rathinam Group" and job.answer == "Research Intern" and emp.key != job.key
    collision_keys = {emp.key, job.key}
    gen = con.execute("SELECT * FROM answers WHERE key LIKE 'hermes.generated.%'").fetchall()
    assert len(gen) == 1 and gen[0]["approved"] == 0 and answers.get(gen[0]["key"]) is None
    assert summary["answers_skipped_inconsistent"] == 1 and summary["answers_skipped_secret_or_empty"] == 1

    # events: copied with the hermes: prefix, job_id NULL
    ev = con.execute("SELECT * FROM events WHERE kind LIKE 'hermes:%' ORDER BY n").fetchall()
    assert [e["kind"] for e in ev] == ["hermes:status:queued", "hermes:status:submitted", "hermes:status:hold"]
    assert all(e["job_id"] is None for e in ev) and '"listing_id": "sub-1"' in ev[0]["detail"]
    assert summary["events"] == 3
    assert db.meta_get("baseline_done") is not None

    # idempotent: a second run adds nothing
    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("listings", "jobs", "applications", "applied_keys", "question_aliases")}
    live_before = con.execute("SELECT count(*) FROM answers WHERE superseded_by IS NULL").fetchone()[0]
    ev_before = con.execute("SELECT count(*) FROM events WHERE kind LIKE 'hermes:%'").fetchone()[0]
    again = mod.run_import(hermes_dir)
    after = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert after == before
    assert con.execute("SELECT count(*) FROM answers WHERE superseded_by IS NULL").fetchone()[0] == live_before
    assert con.execute("SELECT count(*) FROM events WHERE kind LIKE 'hermes:%'").fetchone()[0] == ev_before
    assert again["jobs_created"] == 0 and again["applications"] == 0 and again["events"] == 0
    assert again["answers"] == 0 and again["aliases"] == 0 and again["answers_orphaned"] == 0
    emp2 = answers.lookup("Work History - Rathinam Group / Texas A&M Department of Mechanical Engineering - Employer Name")
    assert {emp2.key, job.key} == collision_keys  # collision suffix is stable across runs


def test_looks_secret():
    mod = _load_script()
    assert mod.looks_secret("Password", "anything")
    assert mod.looks_secret("Email", "")
    assert mod.looks_secret("Reference code", "ab12CD34ef56GH78ij90")
    assert not mod.looks_secret("Phone", "5550100100")
    assert not mod.looks_secret("Email", "applicant@example.com")
    assert not mod.looks_secret("LinkedIn", "https://linkedin.com/in/surya-thirukonda")
