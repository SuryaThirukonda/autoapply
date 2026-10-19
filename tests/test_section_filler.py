"""The Simplify-style section filler against offline Workday and Greenhouse employment pages: every stored
entry lands once, with dates and the current flag, blanks and duplicates are cleaned up, and a second run
changes nothing."""
from pathlib import Path

import pytest

from goat import db, records
from goat.adapters import greenhouse_sections as gh
from goat.adapters import workday_sections as ws
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

FIX = Path(__file__).parent / "fixtures"

ENTRIES = [
    {"n": 1, "company": "Revio", "title": "Founding Software Engineer (Part-time)", "location": "Remote", "start": "02/01/2026", "end": "", "current": True,
     "description": "• Launched a beta. • Kept documents intact."},
    {"n": 2, "company": "DeKalb Capital Management", "title": "Software Engineering Intern", "location": "Remote", "start": "06/01/2026", "end": "08/20/2026", "current": False,
     "description": "• Built analytics."},
    {"n": 3, "company": "Mai Shan Yun (WaiterDock)", "title": "Contract Software Engineer", "location": "College Station, TX", "start": "02/01/2026", "end": "06/30/2026", "current": False,
     "description": "• Won the challenge."},
    {"n": 4, "company": "Rathinam Group, Texas A&M", "title": "Undergraduate Research Assistant", "location": "College Station, TX", "start": "09/01/2026", "end": "", "current": True,
     "description": "• " + "x" * 300 + ". • " + "y" * 300 + "."},
    {"n": 5, "company": "Southwest Airlines", "title": "Data Analyst", "location": "College Station, TX", "start": "10/01/2025", "end": "12/15/2025", "current": False,
     "description": "• Forecast baggage revenue."},
]
SITES = ["https://github.com/SuryaThirukonda", "https://www.linkedin.com/in/surya-thirukonda"]


@pytest.fixture()
def stored(monkeypatch, tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(ws, "stored_work_entries", lambda company="", portal="workday": [dict(e) for e in ENTRIES])
    monkeypatch.setattr(ws, "stored_websites", lambda company="", portal="workday": list(SITES))
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Acme','Intern','https://x.example/job','workday')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','account',0)")


async def _open(path: Path):
    url = path.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


def _filler(portal, ex):
    job = {"id": 1, "company": "Acme"}
    return (gh.GreenhouseSections if portal == "greenhouse" else ws.WorkdaySections)(ex, job, 1)


def _work(entries):
    return [e for e in entries if e["kind"] in ws.WORK_KINDS or e["kind"] == "employment"]


def _vals(entries):
    out = {}
    for e in _work(entries):
        f = e["fields"]
        comp = (f.get("companyName") or {}).get("value", "")
        out[comp] = {k: v["value"] for k, v in f.items()}
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("portal", ["workday", "greenhouse"])
async def test_run_adds_every_entry_cleans_blanks_and_duplicates_and_is_idempotent(stored, portal):
    ex = await _open(FIX / portal / "sections.html")
    try:
        f = _filler(portal, ex)
        await snapshot.take(ex)
        rep = await f.run()
        assert rep.failed == [], rep.summary()
        added = [a for a in rep.added if not a.startswith("http") and not a.startswith("skill")]
        assert sorted(added) == sorted(["DeKalb Capital Management", "Mai Shan Yun (WaiterDock)", "Rathinam Group, Texas A&M", "Southwest Airlines"])
        assert any("Revio" in u or "duplicate" in u for u in rep.updated) or "Revio" in rep.present
        info = await f.scan()
        vals = _vals(info["entries"])
        assert set(vals) == {e["company"] for e in ENTRIES}, vals.keys()
        assert len(_work(info["entries"])) == 5                         # blank reused, nothing left over, duplicate gone
        rev, dek = vals["Revio"], vals["DeKalb Capital Management"]
        assert rev["currentlyWorkHere"] == "checked" and dek["currentlyWorkHere"] == ""
        if portal == "workday":
            assert (rev["startDate-dateSectionMonth-input"], rev["startDate-dateSectionYear-input"]) == ("02", "2026")
            assert (dek["endDate-dateSectionMonth-input"], dek["endDate-dateSectionYear-input"]) == ("08", "2026")
            assert rev["roleDescription"].startswith("• Launched a beta.\n• Kept")
            assert len(vals["Rathinam Group, Texas A&M"]["roleDescription"]) <= 500 and any("trimmed" in s for s in rep.skipped)
            assert sorted(a for a in rep.added if a.startswith("http")) == sorted(SITES)
        else:
            assert (rev["startMonthCombo"], rev["startYear"]) == ("February", "2026")
            assert (dek["endMonthCombo"], dek["endYear"]) == ("August", "2026")
            assert await ex.page.evaluate("() => document.getElementById('end-date-month-0').disabled") is True
        srcs = {r["source"] for r in records.decisions(1)}
        assert any(s.startswith("standing:experience.work.") for s in srcs)
        # second run: nothing to do
        f2 = _filler(portal, ex)
        rep2 = await f2.run()
        assert rep2.added == [] and [u for u in rep2.updated if "removed" not in u] == [] and rep2.failed == []
        assert sorted(p for p in rep2.present if not p.startswith("skill")) == sorted(e["company"] for e in ENTRIES)
        assert _vals((await f2.scan())["entries"]) == vals
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_greenhouse_prefill_runs_the_filler_and_leaves_the_entry_boxes_to_it(stored, monkeypatch):
    from goat import answers
    from goat.adapters import for_portal
    answers.save_answer("identity.location_city", "Sugar Land, Texas", answer_type="text", source="questionnaire")
    answers.save_answer("identity.first_name", "Surya", answer_type="text", source="questionnaire")
    ex = await _open(FIX / "greenhouse" / "sections.html")
    try:
        snap = await snapshot.take(ex)
        job = {"id": 1, "company": "Acme"}
        res = await for_portal("greenhouse").prefill(ex, snap, job, 1)
        assert any(d.get("label") == "Employment section" and d.get("ok") for d in res.details)
        titles = await ex.page.evaluate("() => Array.from(document.querySelectorAll('input[id^=title-]')).map(e => e.value)")
        assert "Sugar Land, Texas" not in titles and "Surya" not in titles
        assert await ex.page.evaluate("() => document.getElementById('first_name').value") == "Surya"
        ev = db.connect().execute("SELECT detail FROM events WHERE kind='adapter:sections'").fetchone()
        assert ev and "Southwest Airlines" in ev[0]
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_ashby_page_without_an_employment_section_is_a_noop(stored):
    ex = await _open(FIX / "form.html")
    try:
        snap = await snapshot.take(ex)
        assert not gh.is_employment_page(snap)
        rep = await gh.GreenhouseSections(ex, {"id": 1, "company": "Acme"}, 1).run()
        assert rep.added == [] and rep.updated == [] and rep.failed == [] and rep.present == []
        assert rep.skipped in ([], ["no Work Experience section on this page"])
    finally:
        await ex.close(close_browser=True)


def test_greenhouse_gate_blocks_until_every_stored_company_is_in_the_section(stored):
    from goat.browser.snapshot import Snapshot
    from goat.gate import run_gate

    def snap(companies):
        items = [{"ref": f"e{i}", "tag": "input", "type": "text", "label": "Company name", "value": c} for i, c in enumerate(companies, 1)]
        s = Snapshot(url="https://job-boards.greenhouse.io/embed/job_app", title="Apply", headings=[], items=items, groups=[], errors={}, challenge={})
        for i in items:
            s.refs[i["ref"]] = i
        return s
    job = {"id": 1, "company": "Acme", "title": "Intern", "url": "https://x.example/job", "listing_id": "l1"}
    g = run_gate(job=job, snapshot=snap(["Revio", "DeKalb Capital Management"]), page_text="Apply", expected_resume=None, free_text_answers=[], portal="greenhouse")
    assert g.result == "blocked" and any("Southwest Airlines" in r and "fill_sections" in r for r in g.reasons)
    g = run_gate(job=job, snapshot=snap([e["company"] for e in ENTRIES]), page_text="Apply", expected_resume=None, free_text_answers=[], portal="greenhouse")
    assert not any("Employment section" in r for r in g.reasons)


@pytest.mark.asyncio
async def test_greenhouse_education_only_form_gets_no_blank_blocks(stored):
    """Tenstorrent (2026-10-18): the Education section's 'Add another' is not the employment section's."""
    ex = await _open(FIX / "greenhouse" / "education_only.html")
    try:
        snap = await snapshot.take(ex)
        assert not gh.is_employment_page(snap)
        f = gh.GreenhouseSections(ex, {"id": 1, "company": "Acme"}, 1)
        assert (await f.scan())["sections"] == []
        rep = await f.run()
        assert rep.added == [] and all("Add button not found" in x for x in rep.failed)
        assert await ex.page.evaluate("() => document.querySelectorAll('#education > div').length") == 1
    finally:
        await ex.close(close_browser=True)
