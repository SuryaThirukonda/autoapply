"""The two compilations goat reads besides Simplify (zshah101's jobs.json, adminazhar's lists): what is taken from them,
and that a posting two lists carry becomes one job."""
import json
import time
from datetime import datetime, timezone

import pytest

from goat import db, ledger, scraper, states, triage

GH_ACME = "https://boards.greenhouse.io/acme/jobs/7001234"
GLOBEX = "https://job-boards.greenhouse.io/globex/jobs/7002001"
FEEDS = (("zshah101", "https://z.example/data/jobs.json"), ("adminazhar", "https://a.example/README.md"))


@pytest.fixture
def fresh_db(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    yield
    db.reset_for_tests(tmp_path / "t2.db")


def _zshah(key, company, title, url, *, location="Austin, TX", is_open=True, season="Summer 2027"):
    return {"id": key, "company": company, "title": title, "url": url, "location": location, "is_open": is_open,
            "season": season, "posted_at": "2026-10-20T12:00:00Z", "last_seen_at": "2026-10-22T08:00:00Z"}


def _md(*rows):
    head = "| Company | Role | Location | Pay | Age | Apply |\n|---|---|---|---|---|---|\n"
    return head + "\n".join(f'| **{c}** | {t} | {loc} | $40/hr | 2d | <a href="{u}"><img src="assets/apply.svg"></a> '
                            f'<a href="https://www.talentdpro.com/us/jobs/x"><img src="assets/details.svg"></a> |'
                            for c, t, loc, u in rows)


def _simplify(id_, company, title, url):
    return {"id": id_, "company_name": company, "title": title, "url": url, "category": "Software", "active": True,
            "is_visible": True, "locations": ["Austin, TX"], "terms": ["Summer 2027"], "degrees": ["Bachelor's"],
            "sponsorship": "Other", "source": "Simplify", "date_posted": int(time.time()) - 3600,
            "date_updated": int(time.time())}


def _fetcher(texts):
    def fetch(label, url):
        t = texts[label]
        if isinstance(t, Exception):
            raise t
        return t
    return fetch


def _job(listing_id):
    return db.connect().execute("SELECT * FROM jobs WHERE listing_id=?", (listing_id,)).fetchone()


def _listing(listing_id):
    return db.connect().execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()


def test_zshah_rows_take_simplifys_shape():
    payload = {"gh:acme:1": _zshah("gh:acme:1", "Acme", "Data Science Intern", GH_ACME, is_open=False,
                                   location="London, United Kingdom; New York, NY, United States")}
    (r,) = scraper.zshah_rows(payload)
    assert r["id"] == "zshah-gh:acme:1" and r["company_name"] == "Acme" and r["category"] == "AI/ML/Data"
    assert r["locations"] == ["London, United Kingdom", "New York, NY, United States"]
    assert r["terms"] == ["Summer 2027"] and r["active"] is False and r["source"] == "zshah101"
    assert r["date_posted"] == int(datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp())


def test_adminazhar_rows_take_the_employers_link_never_the_mirror():
    md = _md(("Acme", "Software Engineer Intern", "Austin, TX", GH_ACME))
    (r,) = scraper.adminazhar_rows(md, now=1_000_000)
    assert r["url"] == GH_ACME and r["company_name"] == "Acme" and r["locations"] == ["Austin, TX"]
    assert r["id"].startswith("adminazhar-") and r["id"] == scraper.adminazhar_rows(md, now=5)[0]["id"]
    assert r["date_posted"] == 1_000_000 - 2 * 86400 and r["category"] == "Software"


@pytest.mark.parametrize("place", ["Austin, TX", "Atlanta GA", "WI Madison", "US-IA-CEDAR RAPIDS-131", "Boston, Massachusetts",
                                   "Warsaw, IN", "Dublin, CA", "Vancouver, WA", "Remote", "New York City (Hybrid)",
                                   "Albuquerque, New Mexico, United States", "Multiple Locations", "Aguadilla, PR"])
def test_us_places(place):
    assert scraper.us_place(place)


@pytest.mark.parametrize("place", ["Toronto, ON", "Bengaluru, KA", "Hyderabad, IN", "Berlin, DE", "Tel Aviv, IL",
                                   "Vancouver, BC", "Vancouver, CA", "Beijing, CHN", "London, UK", "Remote - Canada",
                                   "Mexico City, Mexico", "Waterloo, ON, CA", "Hybrid"])
def test_places_abroad_or_unknown(place):
    assert not scraper.us_place(place)


@pytest.mark.parametrize("title, company, kept", [
    ("Software Engineer Intern", "Acme", True),
    ("Cybersecurity Undergraduate Intern", "LLNL", True),
    ("Software Engineer", "Acme", False),                                       # not an internship
    ("Mechanical Engineering Intern", "Acme", False),                           # not one of his roles
    ("Cybersecurity Graduate Intern", "LLNL", False),                           # graduate students only
    ("Field Applications Engineer Intern", "NXP", False),                       # customer-facing chip roles
    ("Applications Engineering Intern", "KLA", False),
    ("Epidemiology and RWE Capabilities & Analytics Intern", "Merck", False),
    ("Software Engineer Intern", "Optiver", False),                             # quant and trading firms
    ("Software Engineer Intern", "Jump Trading", False),
    ("Technology Summer Analyst", "Goldman Sachs", True),
    ("Asset Management Data Scientist - Summer Associate", "JP Morgan Chase", False),   # MBA and graduate program
])
def test_keep_extra_takes_internships_in_his_roles(title, company, kept):
    assert scraper.keep_extra({"title": title, "company_name": company, "locations": ["Austin, TX"], "active": True}) is kept


def test_keep_extra_leaves_closed_and_foreign_rows():
    row = {"title": "Software Engineer Intern", "company_name": "Acme", "locations": ["Toronto, ON"], "active": True}
    assert not scraper.keep_extra(row)
    assert scraper.keep_extra({**row, "locations": ["Toronto, ON", "Seattle, WA"]})
    assert not scraper.keep_extra({**row, "locations": ["Seattle, WA"], "active": False})


def test_same_posting_by_id_link_and_title():
    assert ledger.same_posting("Software Engineer Intern", GH_ACME, "SWE Intern",
                               "https://job-boards.greenhouse.io/acme/jobs/7001234?gh_src=x")           # one posting id
    wd = "https://nxp.wd3.myworkdayjobs.com/careers/job/Austin/Software-Intern_R12345"
    assert ledger.same_posting("Software Intern", wd, "Software Intern", wd.replace("/careers/", "/en-US/careers/"))
    assert not ledger.same_posting("Software Engineer Intern", GH_ACME, "Software Engineer Intern",
                                   "https://boards.greenhouse.io/acme/jobs/7009999")                    # the ids differ
    assert ledger.same_posting("Software Engineering Intern - Summer 2027", "https://acme.example/careers",
                               "Software Engineer Intern", "https://other.example/jobs")
    assert not ledger.same_posting("Software Engineer Intern", "https://acme.example/jobs/a",
                                   "Software Engineer Intern", "https://acme.example/jobs/b")           # one portal, two links


def test_a_poll_queues_new_postings_once_and_leaves_what_goat_holds(fresh_db):
    scraper.ingest([_simplify("s-1", "Acme", "Software Engineer Intern", GH_ACME)], first_run=False)
    z = {k: _zshah(k, *v[:3], **v[3]) for k, v in {
        "gh:acme:1": ("Acme", "Software Engineer Intern", "https://job-boards.greenhouse.io/acme/jobs/7001234", {}),
        "gh:globex:1": ("Globex", "Software Developer Intern", GLOBEX, {}),
        "gh:globex:2": ("Globex", "Software Developer Intern", "https://job-boards.greenhouse.io/globex/jobs/7002002",
                        {"location": "Denver, CO"}),
        "gh:ibm:1": ("IBM", "Software Engineering Intern", "https://ibm.example/jobs/1", {}),
        "gh:acme:2": ("Acme", "Mechanical Engineering Intern", "https://job-boards.greenhouse.io/acme/jobs/7001999", {}),
    }.items()}
    md = _md(("Globex", "Software Developer Intern", "Austin, TX", GLOBEX),
             ("Initech", "Data Analyst Intern", "Toronto, ON", "https://initech.example/jobs/1"),
             ("Hooli", "Data Science Intern", "Mountain View, CA", "https://jobs.lever.co/hooli/abc"))
    out = scraper.poll_extra_once(FEEDS, fetch=_fetcher({"zshah101:jobs.json": json.dumps(z), "adminazhar:README.md": md}))

    assert out["zshah101:jobs.json"]["queued"] == 1 and out["zshah101:jobs.json"]["skipped"] == 1
    assert _job("zshah-gh:globex:1")["status"] == states.QUEUED
    assert _listing("zshah-gh:ibm:1")["triage_reason"] == "do_not_reapply"            # "Don't add IBM roles"
    # Simplify's copy of Acme, Globex's other site and a role outside his fields are left out
    assert all(_listing(f"zshah-gh:{k}") is None for k in ("acme:1", "globex:2", "acme:2"))
    # adminazhar: Globex came from zshah101 this same poll and Initech is in Canada, so only Hooli is new
    assert out["adminazhar:README.md"]["new"] == 1
    hooli = db.connect().execute("SELECT * FROM listings WHERE company='Hooli'").fetchone()
    assert hooli["triage_status"] == "queued" and scraper.feed_of(hooli["id"]) == "adminazhar"


def test_a_row_the_list_marks_closed_closes_its_job(fresh_db):
    row = _zshah("gh:globex:1", "Globex", "Software Developer Intern", GLOBEX)
    feeds = FEEDS[:1]
    scraper.poll_extra_once(feeds, fetch=_fetcher({"zshah101:jobs.json": json.dumps({"gh:globex:1": row})}))
    assert _job("zshah-gh:globex:1")["status"] == states.QUEUED
    closed = {"gh:globex:1": {**row, "is_open": False}}
    out = scraper.poll_extra_once(feeds, fetch=_fetcher({"zshah101:jobs.json": json.dumps(closed)}))
    assert out["zshah101:jobs.json"]["closed"] == 1 and _listing("zshah-gh:globex:1")["triage_status"] == "closed"
    job = _job("zshah-gh:globex:1")
    assert job["status"] == states.DEAD and job["hold_reason"] == "posting_closed"


def test_a_list_that_cannot_be_read_does_not_stop_the_other(fresh_db):
    md = _md(("Hooli", "Data Science Intern", "Mountain View, CA", "https://jobs.lever.co/hooli/abc"))
    out = scraper.poll_extra_once(FEEDS, fetch=_fetcher({"zshah101:jobs.json": RuntimeError("down"),
                                                         "adminazhar:README.md": md}))
    assert out["zshah101:jobs.json"] == {"error": "RuntimeError: down"} and out["adminazhar:README.md"]["queued"] == 1
    assert db.meta_get("extra_feed:zshah101:jobs.json:last_ok") is None
    out = scraper.poll_extra_once(FEEDS, fetch=_fetcher({"zshah101:jobs.json": None, "adminazhar:README.md": None}))
    assert out == {"zshah101:jobs.json": {"status": 304}, "adminazhar:README.md": {"status": 304}}
    assert db.meta_get("extra_feed:zshah101:jobs.json:last_ok") is not None      # an unchanged list was still read


def test_a_poll_never_breaks_the_workers_writes(tmp_path, monkeypatch):
    """The poll runs on the scraper's thread over goat's one connection. While it built its index from an open cursor,
    a write from another process made every write of the worker fail with 'database is locked' (2026-10-22 18:26)."""
    import sqlite3
    import threading
    db.reset_for_tests(tmp_path / "race.db")
    with db.tx() as c:
        for i in range(1500):
            c.execute("INSERT INTO listings(id, company, title, url, triage_status, active) VALUES(?,?,?,?,'queued',1)",
                      (f"l{i}", f"Co {i}", "Software Engineer Intern", f"https://x.example/jobs/{i}"))
    add = scraper.PostingIndex.add
    monkeypatch.setattr(scraper.PostingIndex, "add", lambda self, *a: (time.sleep(0.0005), add(self, *a)))
    errors = []

    def worker():
        time.sleep(0.1)
        other = sqlite3.connect(str(tmp_path / "race.db"), timeout=30)       # an operator script writes meanwhile
        other.execute("INSERT INTO meta(key, value) VALUES('x', '1') ON CONFLICT(key) DO UPDATE SET value='2'")
        other.commit()
        for k in range(10):
            try:
                db.event(None, "test:write", {"k": k})
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
            time.sleep(0.02)

    t = threading.Thread(target=worker)
    t.start()
    scraper.poll_extra_once(FEEDS[:1], fetch=_fetcher({"zshah101:jobs.json": json.dumps({})}))
    t.join()
    db.reset_for_tests(tmp_path / "after.db")
    assert errors == []


def test_simplify_adds_no_second_job_for_a_posting_another_list_gave(fresh_db):
    scraper.ingest(scraper.zshah_rows({"gh:globex:1": _zshah("gh:globex:1", "Globex", "Software Developer Intern", GLOBEX)}),
                   first_run=False)
    counts = scraper.ingest([_simplify("s-9", "Globex", "Software Developer Intern - Summer 2027",
                                       "https://boards.greenhouse.io/globex/jobs/7002001?gh_src=Simplify")], first_run=False)
    assert counts["duplicate"] == 1 and _job("s-9") is None
    dup = _listing("s-9")
    assert dup["triage_status"] == "skipped" and dup["triage_reason"] == "duplicate:zshah-gh:globex:1"
    # the same role's posting for another site (its own id) is its own job, as Simplify's rows always were
    scraper.ingest([_simplify("s-10", "Globex", "Software Developer Intern",
                              "https://boards.greenhouse.io/globex/jobs/7002999")], first_run=False)
    assert _job("s-10")["status"] == states.QUEUED


def test_feed_of_names_the_list_a_listing_came_from():
    assert scraper.feed_of("zshah-gh:acme:1") == "zshah101" and scraper.feed_of("adminazhar-0123abcd") == "adminazhar"
    assert scraper.feed_of("manual:05b192837c41") == "manual"
    assert scraper.feed_of("3f2b1c9e-8a7d-4e6f-9b0a-1c2d3e4f5a6b") == "simplify"


@pytest.mark.parametrize("company, blocked", [("IBM", True), ("IBM Corporation", True), ("Kibmo Labs", False),
                                              ("Netic", True), ("Magnetic Inc", False), ("Cybernetic Labs", True)])
def test_blocked_companies_match_whole_words(company, blocked):
    _, reason, _, _ = triage.classify({"title": "Software Engineer Intern", "company_name": company, "category": "Software",
                                       "url": GH_ACME, "locations": ["Austin, TX"]})
    assert (reason == "do_not_reapply") is blocked


@pytest.mark.parametrize("place, abroad", [("Indianapolis, IN", False), ("Fort Wayne, Indiana", False),
                                           ("Santa Fe, New Mexico", False), ("Pune, India", True),
                                           ("Mexico City, Mexico", True)])
def test_us_places_named_like_a_country_are_not_abroad(place, abroad):
    _, reason, _, _ = triage.classify({"title": "Software Engineer Intern", "company_name": "Acme", "category": "Software",
                                       "url": GH_ACME, "locations": [place]})
    assert (reason == "location_outside_us") is abroad
