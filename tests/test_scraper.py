import time

import pytest

from goat import db, ledger, scraper, states


def _row(id_, company, title, url, *, category="Software", active=True, locations=("Austin, TX",),
         terms=("Summer 2027",), degrees=("Bachelor's",), date_posted=None):
    return {
        "id": id_, "company_name": company, "title": title, "url": url, "category": category,
        "active": active, "is_visible": True, "locations": list(locations), "terms": list(terms),
        "degrees": list(degrees), "sponsorship": "Other", "source": "Simplify",
        "date_posted": date_posted or int(time.time()) - 3600, "date_updated": int(time.time()),
    }


GH = _row("gh-1", "Acme", "Software Engineer Intern", "https://job-boards.greenhouse.io/acme/jobs/4001")
LEVER = _row("lv-1", "Globex", "Software Developer Intern", "https://jobs.lever.co/globex/9b2d5a1e-1234-4c1a-9f0e-abcdefabcdef")
QUANT = _row("q-1", "Hedgie", "Quant Trading Intern", "https://job-boards.greenhouse.io/hedgie/jobs/4002", category="Quant")


@pytest.fixture
def fresh_db(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    yield
    db.reset_for_tests(tmp_path / "t2.db")


def _job(listing_id):
    return db.connect().execute("SELECT * FROM jobs WHERE listing_id=?", (listing_id,)).fetchone()


def _listing(listing_id):
    return db.connect().execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()


def test_ingest_creates_jobs_by_lane(fresh_db):
    counts = scraper.ingest([GH, LEVER, QUANT], first_run=False)
    assert counts["new"] == 3 and counts["queued"] == 2 and counts["rejected"] == 1

    gh = _job("gh-1")
    assert gh["status"] == states.QUEUED and gh["lane"] == "no_account"
    assert gh["apply_url"] == GH["url"] and gh["resume_variant"] == "swe" and gh["priority"] == 0

    lv = _job("lv-1")
    assert lv["status"] == states.QUEUED and lv["lane"] == "ready_for_you"

    assert _job("q-1") is None
    q = _listing("q-1")
    assert q["triage_status"] == "rejected" and q["triage_reason"] == "category:Quant"

    l = _listing("gh-1")
    assert l["company"] == "Acme" and l["locations"] == '["Austin, TX"]' and l["terms"] == '["Summer 2027"]'
    kinds = [r["kind"] for r in db.connect().execute("SELECT kind FROM events")]
    assert kinds.count("listing:new") == 3


def test_a_skipped_portal_gets_a_closed_job_not_a_queued_one(fresh_db, monkeypatch):
    from goat.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "skip_ats", ["icims"])
    ic = _row("ic-1", "Peraton", "Software Engineer Intern", "https://careers-peraton.icims.com/jobs/1234/software-engineer-intern/job")
    scraper.ingest([GH, ic], first_run=False)
    job = _job("ic-1")
    assert job is not None and job["status"] == states.DEAD and job["hold_reason"].startswith("skipped: icims is not run")
    assert job["finished_at"] and _job("gh-1")["status"] == states.QUEUED


def test_reingest_inactive_closes_job(fresh_db):
    scraper.ingest([GH], first_run=False)
    closed = dict(GH, active=False)
    counts = scraper.ingest([closed], first_run=False)
    assert counts["closed"] == 1 and counts["jobs_closed"] == 1 and counts["new"] == 0
    job = _job("gh-1")
    assert job["status"] == states.DEAD and job["hold_reason"] == "posting_closed"
    l = _listing("gh-1")
    assert l["active"] == 0 and l["triage_status"] == "closed"
    # a second identical poll is a no-op
    counts = scraper.ingest([closed], first_run=False)
    assert counts["closed"] == 0 and counts["jobs_closed"] == 0


def test_first_run_is_baseline_without_jobs(fresh_db):
    counts = scraper.ingest([GH, QUANT], first_run=True)
    assert counts["baseline"] == 2
    assert _job("gh-1") is None
    assert _listing("gh-1")["triage_status"] == "baseline"
    assert _listing("gh-1")["triage_reason"] == "would_be:queued:"
    assert _listing("q-1")["triage_reason"] == "would_be:rejected:category:Quant"


def test_already_applied_is_skipped(fresh_db):
    ledger.record_application(None, "Acme", "Software Engineer Intern", GH["url"], origin="manual")
    counts = scraper.ingest([GH], first_run=False)
    assert counts["already_applied"] == 1 and _job("gh-1") is None
    assert _listing("gh-1")["triage_status"] == "skipped"
    assert _listing("gh-1")["triage_reason"] == "already_applied"


def test_backfill_promotes_recent_baseline(fresh_db):
    old = _row("gh-old", "Old Co", "Software Engineer Intern", "https://boards.greenhouse.io/oldco/jobs/1",
               date_posted=int(time.time()) - 40 * 86400)
    scraper.ingest([GH, old, QUANT], first_run=True)
    plan = scraper.backfill(days=14, dry_run=True)
    assert [p["listing_id"] for p in plan] == ["gh-1"]
    assert _job("gh-1") is None
    done = scraper.backfill(days=14, dry_run=False)
    assert done[0]["listing_id"] == "gh-1" and _job("gh-1")["status"] == states.QUEUED
    assert _listing("gh-1")["triage_status"] == "queued"
    assert scraper.backfill(days=14, dry_run=False) == []


def test_parse_greenhouse():
    assert scraper.parse_greenhouse("https://job-boards.greenhouse.io/instead/jobs/7761472003") == ("instead", "7761472003")
    assert scraper.parse_greenhouse("https://boards.greenhouse.io/acme/jobs/123?utm=x") == ("acme", "123")
    assert scraper.parse_greenhouse("https://www.sagesure.com/careers/current-job-openings/?gh_jid=4719536006") \
        == ("sagesure", "4719536006")
    assert scraper.parse_greenhouse("https://careers.example.co.uk/jobs?gh_jid=77&for=examp") == ("examp", "77")
    assert scraper.parse_greenhouse("https://boards.greenhouse.io/embed/job_app?for=sagesure&token=4719536006") \
        == ("sagesure", "4719536006")
    assert scraper.parse_greenhouse("https://jobs.lever.co/x/y") is None
    assert scraper.parse_greenhouse("") is None


def test_parse_lever():
    u = "https://jobs.lever.co/hermeus/5b08e2df-c9db-4831-aece-67d89e744796/apply"
    assert scraper.parse_lever(u) == ("hermeus", "5b08e2df-c9db-4831-aece-67d89e744796")
    assert scraper.parse_lever("https://jobs.lever.co/hermeus") is None
    assert scraper.parse_lever("https://job-boards.greenhouse.io/a/jobs/1") is None


def test_parse_ashby():
    u = "https://jobs.ashbyhq.com/telus-digital/32ee6ede-d3cf-4827-b7af-f8d64f2bca11/application?embed=true"
    assert scraper.parse_ashby(u) == ("telus-digital", "32ee6ede-d3cf-4827-b7af-f8d64f2bca11")
    assert scraper.parse_ashby("https://jobs.ashbyhq.com/telus-digital") is None
    assert scraper.parse_ashby("https://example.com/x") is None


def test_check_posting_live_unknown_ats_and_unparseable():
    assert scraper.check_posting_live("workday", "https://x.myworkdayjobs.com/a/job/b") is None
    assert scraper.check_posting_live("greenhouse", "https://example.com/nothing") is None


def test_ingest_marks_his_roles_and_holds_other_fields(fresh_db):
    """Surya (2026-10-21): the feed's new postings in his roles are counted 'aligned' (rank_queue puts them first);
    a posting in another field waits as 'unsure'; a SuccessFactors posting is queued for the agent."""
    import json
    mech = _row("me-1", "Zebra", "Mechanical Engineer Intern", "https://job-boards.greenhouse.io/zebra/jobs/4003")
    sf = _row("sf-1", "NetApp", "Data Engineer Intern", "https://career4.successfactors.com/career?career_job_req_id=12345")
    counts = scraper.ingest([GH, mech, sf], first_run=False)
    assert counts["aligned"] == 2 and counts["queued"] == 2 and counts["unsure"] == 1
    assert _job("me-1") is None and _listing("me-1")["triage_reason"] == "title:off_role"
    assert _job("sf-1")["status"] == states.QUEUED and _job("sf-1")["lane"] == "account"
    fits = {json.loads(r["detail"])["listing_id"]: json.loads(r["detail"])["role_fit"]
            for r in db.connect().execute("SELECT detail FROM events WHERE kind='listing:new'")}
    assert fits == {"gh-1": "aligned", "me-1": "off_role", "sf-1": "aligned"}
