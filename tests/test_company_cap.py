"""Surya (2026-10-22): roles less aligned with his preferences stop at 6 active, non-rejected positions per company;
aligned roles and his own Need to Apply rows are never held back by it."""
import time

import pytest

from goat import caps, db, scraper


@pytest.fixture
def fresh_db(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    yield
    db.reset_for_tests(tmp_path / "t2.db")


def _job(con, lid, company, title, status):
    con.execute("INSERT INTO listings(id, company, title, url, ats, triage_status, active) VALUES(?,?,?,?,?,'queued',1)",
                (lid, company, title, f"https://x.example/{lid}", "greenhouse"))
    con.execute("INSERT INTO jobs(listing_id, status, lane, created_at, updated_at) VALUES(?,?,?,1,1)", (lid, status, "no_account"))


def _row(id_, company, title, url):
    return {"id": id_, "company_name": company, "title": title, "url": url, "category": "Software", "active": True,
            "is_visible": True, "locations": ["Austin, TX"], "terms": ["Summer 2027"], "degrees": ["Bachelor's"],
            "sponsorship": "Other", "source": "Simplify", "date_posted": int(time.time()) - 3600, "date_updated": int(time.time())}


def test_company_names_fold_to_one_employer():
    assert caps.company_key("Palantir Technologies") == caps.company_key("Palantir")
    assert caps.company_key("Qorvo, Inc.") == caps.company_key("Qorvo")
    assert caps.company_key("JPMorgan Chase") == caps.company_key("JP Morgan Chase")
    assert caps.company_key("Lawrence Livermore National Laboratory (LLNL)") == caps.company_key("Lawrence Livermore National Laboratory")
    assert caps.company_key("Apple") != caps.company_key("Applied Materials")


def test_positions_are_sent_applications_jobs_under_way_and_the_queue_when_asked(fresh_db):
    with db.tx() as con:
        for i, st in enumerate(["submitted", "filling", "ready_for_you", "queued", "dead", "hold"]):
            _job(con, f"L{i}", "Acme Inc.", f"Role {i}", st)
        con.execute("INSERT INTO applications(listing_id, canonical_key, company, title, url, origin, submitted_at) "
                    "VALUES('L0', 'u:x.example/l0', 'Acme', 'Role 0', 'https://x.example/L0', 'goat', 100)")
    assert caps.company_positions("Acme", with_queued=False) == 3          # sent, filling, waiting on him
    assert caps.company_positions("ACME, Inc", with_queued=True) == 4      # and the queued one; dead and held never


def test_a_less_aligned_role_stops_at_six_per_company(fresh_db):
    with db.tx() as con:
        for i in range(6):
            _job(con, f"L{i}", "Acme", f"Software Engineer Intern {i}", "queued")
    assert "company_cap" in caps.less_aligned_capped("Acme", "Engineering Intern", with_queued=True)
    assert caps.less_aligned_capped("Acme", "Software Engineer Intern", with_queued=True) is None      # aligned
    assert caps.less_aligned_capped("Acme", "Engineering Intern", with_queued=False) is None           # the queue behind it
    assert caps.less_aligned_capped("Acme", "Engineering Intern", with_queued=True, listing_id="manual:ab12") is None


def test_ingest_queues_aligned_roles_and_skips_the_seventh_less_aligned_one(fresh_db):
    rows = [_row(f"s-{i}", "Acme", f"Software Engineer Intern - Team {i}", f"https://boards.greenhouse.io/acme/jobs/700{i}001")
            for i in range(6)]
    assert scraper.ingest(rows, first_run=False)["queued"] == 6
    out = scraper.ingest([_row("s-7", "Acme Inc.", "Engineering Intern", "https://boards.greenhouse.io/acme/jobs/7007001"),
                          _row("s-8", "Acme", "Backend Software Engineer Intern", "https://boards.greenhouse.io/acme/jobs/7008001")],
                         first_run=False)
    assert out["company_cap"] == 1 and out["queued"] == 1
    con = db.connect()
    capped = con.execute("SELECT triage_status, triage_reason FROM listings WHERE id='s-7'").fetchone()
    assert capped["triage_status"] == "skipped" and capped["triage_reason"].startswith("company_cap")
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE listing_id='s-7'").fetchone()[0] == 0
