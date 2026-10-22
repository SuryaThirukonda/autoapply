from __future__ import annotations

import pytest

from goat import answers, db, ledger, states, triage
from goat.config import SETTINGS
from goat.gate import guardrail_check, scan_page_prohibitions
from goat.worker import reconcile_after_restart


@pytest.fixture()
def fresh(tmp_path):
    return db.reset_for_tests(tmp_path / "t.db")


def test_states_transitions():
    states.check(states.QUEUED, states.FILLING)
    states.check(states.HOLD, states.QUEUED)
    with pytest.raises(states.IllegalTransition):
        states.check(states.SUBMITTED, states.FILLING)


def test_save_answer_releases_holds(fresh):
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url) VALUES('L1','A','T','https://x.com/a')")
        con.execute("INSERT INTO listings(id,company,title,url) VALUES('L2','B','T','https://x.com/b')")
        con.execute("INSERT INTO jobs(listing_id,status,blocked_on_question_key,created_at,updated_at) VALUES('L1','hold','q.relocate_to_mars',1,1)")
        con.execute("INSERT INTO jobs(listing_id,status,blocked_on_question_key,created_at,updated_at) VALUES('L2','hold','q.other',1,1)")
    released = answers.save_answer("q.relocate_to_mars", "No", source="telegram", aliases=["Would you relocate to Mars?"])
    assert released == 1
    rows = {r["listing_id"]: r["status"] for r in db.connect().execute("SELECT listing_id,status FROM jobs")}
    assert rows == {"L1": "queued", "L2": "hold"}
    a = answers.lookup("would you relocate to mars")
    assert a and a.answer == "No" and a.via == "alias_exact"
    # supersede
    answers.save_answer("q.relocate_to_mars", "Yes", source="telegram")
    assert answers.get("q.relocate_to_mars").answer == "Yes"
    assert db.connect().execute("SELECT COUNT(*) FROM answers WHERE key='q.relocate_to_mars' AND superseded_by IS NOT NULL").fetchone()[0] == 1


def test_seed_idempotent(fresh):
    c1 = answers.seed_from_config(SETTINGS.config_dir)
    n1 = db.connect().execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    c2 = answers.seed_from_config(SETTINGS.config_dir)
    n2 = db.connect().execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    assert c1["answers"] > 100 and c2["answers"] == 0 and n1 == n2
    assert answers.lookup("Are you a U.S. citizen?").answer == "Yes"
    assert answers.lookup("Do you now or will you in the future require sponsorship?").answer == "No"
    race = answers.get("self_id.race_ethnicity").answer
    assert race.startswith("decline;") and "Not Specified" in race      # every decline wording, never a category
    assert answers.lookup("Have you previously completed an internship?").answer == "Yes"


def test_ledger_keys_and_fuzzy(fresh):
    ledger.record_application("L1", "Acme Corp", "Software Engineering Internship - Summer 2027 (Remote)", "https://boards.greenhouse.io/acme/jobs/1234567", "test")
    assert ledger.already_applied("Acme Corp", "Software Engineer Intern", "https://other.example.com/x")
    assert ledger.already_applied("Acme Corp", "Anything", "https://boards.greenhouse.io/acme/jobs/1234567/apply")
    assert not ledger.already_applied("Other Co", "Software Engineer Intern", "https://x.com")


def test_triage():
    row = {"id": "1", "company_name": "Acme", "title": "Software Engineer Intern", "url": "https://boards.greenhouse.io/acme/jobs/1",
           "category": "Software Engineering", "terms": ["Summer 2027"], "degrees": ["Bachelor's"], "locations": ["Austin, TX"], "active": True, "is_visible": True}
    assert triage.classify(row)[0] == triage.QUEUED
    assert triage.classify({**row, "title": "Quantitative Trading Intern"})[0] == triage.REJECTED
    assert triage.classify({**row, "url": "https://jobs.lever.co/acme/abc"})[2] == "lever"
    assert triage.lane_for("lever") == triage.LANE_READY_FOR_YOU
    assert triage.classify({**row, "url": "https://careers.smartrecruiters.com/acme/1"})[0] == triage.QUEUED
    assert triage.classify({**row, "url": "https://acme.eightfold.ai/careers/job/1"})[0] == triage.MANUAL


def test_gate_patterns():
    assert scan_page_prohibitions("Use of AI tools to complete this application will result in disqualification.")
    assert not scan_page_prohibitions("We use AI in our products.")
    blocks, warns = guardrail_check("I co-authored a published paper and I know TypeScript.")
    assert "published_paper" in blocks and "typescript_claim" in blocks
    assert guardrail_check("I built the backend for Revio; it has processed 200+ conversions.")[0] == []


def test_reconcile_after_restart(fresh):
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url) VALUES('L1','A','T','https://x.com/a')")
        con.execute("INSERT INTO listings(id,company,title,url) VALUES('L2','B','T','https://x.com/b')")
        con.execute("INSERT INTO jobs(listing_id,status,created_at,updated_at) VALUES('L1','filling',1,1)")
        con.execute("INSERT INTO jobs(listing_id,status,created_at,updated_at) VALUES('L2','submitting',1,1)")
    out = reconcile_after_restart()
    assert out == {"requeued": 1, "submitting_held": 1}
    rows = {r["listing_id"]: (r["status"], r["hold_reason"]) for r in db.connect().execute("SELECT listing_id,status,hold_reason FROM jobs")}
    assert rows["L1"][0] == "queued" and rows["L2"][0] == "hold" and "unreconciled" in rows["L2"][1]



def test_role_fit_names_his_roles_and_holds_other_fields():
    """Surya (2026-10-21): SWE, SDE, application development, programming, technology, data science / engineering /
    analytics first; a title in another field only waits for his call."""
    from goat import triage
    for t in ("Software Engineer Intern", "Software Development Engineer Intern", "SDE Intern", "Application Engineering Intern",
              "Application Analyst Intern", "Information Technology Development and Infrastructure Intern", "Data Science Intern",
              "Data Engineer Intern", "Data Analyst Undergraduate Intern", "Marketing Analytics Intern", "AI Engineer Intern",
              "Cloud Software Developer Intern", "Technology Intern", "Machine Learning Research Intern", "Programmer Intern"):
        assert triage.role_fit(t) == triage.ALIGNED, t
    for t in ("Mechanical Engineer Intern", "Business Operations Engineer Intern", "Operations Analyst Intern",
              "Systems/Process Engineering Intern - Tapeout Operations", "AI Consulting Associate Program Product Owner Intern",
              "Reimbursement Analyst Intern"):
        assert triage.role_fit(t) == triage.OFF_ROLE, t
    for t in ("Business Analyst Intern", "Engineering Intern", "AI and Estimation Intern"):
        assert triage.role_fit(t) == triage.NEUTRAL, t
    row = {"id": "x", "title": "Mechanical Engineer Intern", "company_name": "Zebra", "category": "Software",
           "terms": ["Summer 2027"], "url": "https://zebra.wd5.myworkdayjobs.com/x/job/y/z", "active": True, "is_visible": True}
    assert triage.classify(row)[:2] == (triage.UNSURE, "title:off_role")
    assert triage.classify({**row, "title": "Wireless Hardware Engineer Intern"})[:2] == (triage.UNSURE, "title:hardware_role")
    assert triage.classify({**row, "title": "Software Engineer Intern"})[0] == triage.QUEUED
