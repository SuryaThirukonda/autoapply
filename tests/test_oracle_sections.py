"""Oracle Recruiting Cloud's Experience / Education entries (GM Financial, Uber, BNY 2026-10-22): the harness deletes the
résumé import's entries that are none of his, sets his own entries' employer, title, dates and description from the
store, adds the missing ones, fixes his degree entry, works on the tile and the timeline layouts, and a second pass
changes nothing."""
from pathlib import Path

import pytest

from goat import db, gate, records
from goat.adapters import oracle_sections as osec
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

FIX = Path(__file__).parent / "fixtures" / "oracle" / "profile_items.html"

ENTRIES = [
    {"n": 1, "company": "Revio", "title": "Founding Software Engineer (Part-time)", "location": "Remote", "start": "02/01/2026", "end": "",
     "current": True, "description": "• Launched a beta. • Kept documents intact."},
    {"n": 2, "company": "DeKalb Capital Management", "title": "Software Engineering Intern, Quantitative Systems", "location": "Remote",
     "start": "06/01/2026", "end": "08/20/2026", "current": False, "description": "• Built analytics. • Synced positions."},
    {"n": 3, "company": "Mai Shan Yun (WaiterDock)", "title": "Contract Software Engineer", "location": "College Station, TX",
     "start": "02/01/2026", "end": "06/30/2026", "current": False, "description": "• Won the challenge."},
    {"n": 4, "company": "Rathinam Group, Texas A&M Department of Mechanical Engineering", "title": "Undergraduate Research Assistant",
     "location": "College Station, TX", "start": "09/01/2026", "end": "", "current": True, "description": "• Building an orbit viewer."},
    {"n": 5, "company": "Southwest Airlines", "title": "Data Analyst, Baggage Revenue Challenge (Aggie Data Science Club)",
     "location": "College Station, TX", "start": "10/01/2025", "end": "12/15/2025", "current": False, "description": "• Forecast baggage revenue."},
]
EDU = {"education.school": "Texas A&M University", "education.school_variants": "Texas A & M University; Texas A&M University - College Station",
       "education.high_school": "Clements High School", "education.major": "Computer Science", "education.degree": "Bachelor of Science",
       "education.start": "August 2025", "education.graduation": "May 2028"}
SKILLS = "Quantitative Systems, FastAPI, QuestDB, Docker"

# GM Financial after the résumé import (2026-10-22): skill lists as employers, an untitled Southwest entry, a degree
# entry with no school or degree marked Graduated, and an 'Engineering Honors' entry
GM = {"layout": "tiles", "currentFlag": False, "degreeField": True, "work": [
    {"title": "Founding Software Engineer (Part-time)", "employer": "Revio", "sm": "February", "sy": "2026"},
    {"title": "Undergraduate Research Assistant", "employer": "FastAPI, React, Supabase, Docker", "sm": "September", "sy": "2026"},
    {"title": "Software Engineering Intern", "employer": SKILLS, "sm": "June", "sy": "2026", "em": "August", "ey": "2026"},
    {"title": "Contract Software Engineer", "employer": SKILLS, "sm": "February", "sy": "2026", "em": "June", "ey": "2026"},
    {"title": "", "employer": "Southwest Airlines Baggage Revenue Challenge, Python, SARIMAX, Streamlit", "sm": "October", "sy": "2025",
     "em": "December", "ey": "2025"}],
    "education": [{"major": "Computer Science", "school": "", "degree": "", "em": "May", "ey": "2028", "graduated": True},
                  {"major": "Engineering Honors", "school": "Texas A&M University", "degree": ""}]}


@pytest.fixture()
def stored(monkeypatch, tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(osec, "stored_work_entries", lambda company="", portal="": [dict(e) for e in ENTRIES])
    monkeypatch.setattr(osec.OracleSections, "_answer", lambda self, key: EDU.get(key, ""))
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Acme','Intern','https://acme.fa.oraclecloud.com/job/1','oracle')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','account',0)")


async def _open(state: dict) -> Executor:
    url = FIX.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    await ex.page.goto(url)
    await ex.page.evaluate("(s) => window.setup(s)", state)
    await ex.settle(quiet_ms=100)
    return ex


def _filler(ex) -> osec.OracleSections:
    return osec.OracleSections(ex, {"id": 1, "company": "Acme"}, 3)


async def _state(ex) -> dict:
    return await ex.page.evaluate("() => window.state()")


def _work_by_employer(st: dict) -> dict:
    return {w["employer"]: w for w in st["work"]}


def _assert_his_work(st: dict, current_flag: bool) -> None:
    by = _work_by_employer(st)
    assert sorted(by) == sorted(e["company"] for e in ENTRIES), sorted(by)
    dk = by["DeKalb Capital Management"]
    assert (dk["title"], dk["sm"], dk["sy"], dk["em"], dk["ey"]) == ("Software Engineering Intern, Quantitative Systems", "June", "2026", "August", "2026")
    assert dk["desc"] == "• Built analytics.\n• Synced positions." and not dk.get("city")          # Remote: no city
    sw = by["Southwest Airlines"]
    assert (sw["title"], sw["sm"], sw["sy"], sw["em"], sw["ey"], sw["city"]) == (
        "Data Analyst, Baggage Revenue Challenge (Aggie Data Science Club)", "October", "2025", "December", "2025", "College Station")
    ra = by["Rathinam Group, Texas A&M Department of Mechanical Engineering"]
    assert (ra["sm"], ra["sy"], ra["em"], ra["ey"]) == ("September", "2026", "", "")
    if current_flag:
        assert ra["current"] is True
    assert by["Mai Shan Yun (WaiterDock)"]["em"] == "June"


@pytest.mark.asyncio
async def test_gm_financial_import_is_repaired_and_a_second_pass_changes_nothing(stored):
    ex = await _open(GM)
    try:
        f = _filler(ex)
        assert await f.applies()
        rep = await f.run()
        assert rep.failed == [], rep.failed
        assert "experience: Revio" in rep.present                       # right as shown: never opened
        assert len([u for u in rep.updated if u.startswith("experience: ")]) == 4
        assert [u for u in rep.updated if "Engineering Honors" in u] and "education: Texas A&M University" in rep.updated
        assert rep.added == []
        st = await _state(ex)
        assert not st["open"]
        _assert_his_work(st, current_flag=False)
        assert [(d["major"], d["school"], d["degree"], d["sm"], d["sy"], d["em"], d["ey"], d["graduated"]) for d in st["education"]] == [
            ("Computer Science", "Texas A&M University", "Bachelors", "August", "2025", "May", "2028", False)]
        srcs = {r["source"] for r in records.decisions(1)}
        assert {"standing:experience.work.2.company", "standing:experience.work.2.title", "standing:experience.work.2.start",
                "standing:experience.work.2.end", "standing:experience.work.5.title", "standing:experience.work.4.description",
                "standing:education.school", "standing:education.degree", "standing:education.graduation"} <= srcs
        # the gate's own tile check finds nothing left to fix
        snap = await snapshot.take(ex)
        assert gate.oracle_tile_problems(snap.tiles, snap.items, "Acme", "oracle", entries=ENTRIES, school=EDU["education.school"],
                                         high_school=EDU["education.high_school"], required_blocks=snap.required_blocks) == []
        again = await _filler(ex).run()
        assert again.added == [] and again.updated == [] and again.failed == [], (again.added, again.updated, again.failed)
        assert len(again.present) == 6
        assert await _state(ex) == st
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_an_empty_required_experience_block_gets_every_entry_with_current_job_ticked(stored):
    """Uber (2026-10-22): no Experience entries at all, a Current Job box, no Degree field, its School box named
    educationalEstablishmentId and his degree entry already right."""
    ex = await _open({"layout": "tiles", "currentFlag": True, "degreeField": False, "schoolName": "educationalEstablishmentId", "work": [],
                      "education": [{"major": "Computer Science", "school": "Texas A&M University", "sm": "August", "sy": "2025",
                                     "em": "May", "ey": "2028"}]})
    try:
        rep = await _filler(ex).run()
        assert rep.failed == [], rep.failed
        assert len([a for a in rep.added if a.startswith("experience: ")]) == 5
        assert rep.present == ["education: Texas A&M University"]
        st = await _state(ex)
        _assert_his_work(st, current_flag=True)
        assert _work_by_employer(st)["Revio"]["current"] is True and _work_by_employer(st)["Revio"]["em"] == ""
        assert _work_by_employer(st)["Southwest Airlines"].get("current") is False
        assert len(st["education"]) == 1
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_the_timeline_layout_is_repaired_through_hover_actions_and_not_reopened(stored):
    """BNY (2026-10-22): one 'Work and Education History' timeline; most entries show no employer, so each is opened
    once, and a parsed project entry and 'Engineering Honors' are removed through the hover Remove."""
    state = {"layout": "timeline", "currentFlag": True, "degreeField": True, "degreePills": True, "schoolName": "educator", "noGraduated": True, "work": [
        {"title": "Undergraduate Research Assistant", "employer": "FastAPI, React, Supabase, Docker", "sm": "September", "sy": "2026", "current": True},
        {"title": "Software Engineering Intern", "employer": SKILLS, "sm": "June", "sy": "2026", "em": "August", "ey": "2026"},
        {"title": "Contract Software Engineer", "employer": SKILLS, "sm": "February", "sy": "2026", "em": "June", "ey": "2026"},
        {"title": "Founding Software Engineer Part-time", "employer": "FastAPI, React, Supabase, Docker", "sm": "February", "sy": "2026",
         "current": True, "showSub": True},
        {"title": "", "employer": "Southwest Airlines", "sm": "October", "sy": "2025", "em": "December", "ey": "2025"},
        {"title": "Tempo | Node.js, WebSockets", "employer": "", "sm": "March", "sy": "2024"}],
        "education": [{"major": "Engineering Honors", "school": "", "degree": ""},
                      {"major": "Computer Science", "school": "", "degree": "", "em": "May", "ey": "2028"}]}
    ex = await _open(state)
    try:
        rep = await _filler(ex).run()
        assert rep.failed == [], rep.failed
        assert [u for u in rep.updated if "Tempo" in u] and [u for u in rep.updated if "Engineering Honors" in u]
        st = await _state(ex)
        _assert_his_work(st, current_flag=True)
        assert _work_by_employer(st)["Revio"]["title"] == "Founding Software Engineer (Part-time)"
        # BNY's degree pills and its 'Educator' box (2026-10-22: Save was refused, 'The Degree field is required')
        assert [(d["major"], d["school"], d["degree"], d["sm"], d["em"]) for d in st["education"]] == [
            ("Computer Science", "Texas A&M University", "Bachelor's Degree", "August", "May")]
        again = await _filler(ex).run()
        assert again.added == [] and again.updated == [] and again.failed == [], (again.added, again.updated, again.failed)
        assert await _state(ex) == st
        # a new run opens the entries that show no employer once more, finds every value right and cancels: nothing saved
        ex.__dict__.pop("_oracle_verified", None)
        third = await _filler(ex).run()
        assert third.updated == [] and third.failed == [] and len(third.present) == 6, (third.updated, third.failed, third.present)
        assert await _state(ex) == st
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_a_refused_save_is_reported_and_the_form_is_closed(stored):
    """A tenant field the store cannot fill ('Type of Business', required) keeps Save from closing the form: the entry is
    reported failed, the form cancelled, and the other sections still run."""
    ex = await _open(dict(GM, extraRequired=True))
    try:
        rep = await _filler(ex).run()
        assert rep.failed and all("Save was refused" in x for x in rep.failed if x.startswith("experience: ")), rep.failed
        assert "Type of Business" in " ".join(rep.failed)
        st = await _state(ex)
        assert not st["open"]
        assert st["education"][0]["school"] == "Texas A&M University"
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_a_form_left_open_is_cancelled_first_and_the_snapshot_pass_waits_for_it(stored):
    ex = await _open(GM)
    try:
        await ex.page.locator(".apply-flow-profile-item-tile__new-tile").first.click()
        await ex.page.locator("input[id^=employerName-]").fill("Half typed")
        f = _filler(ex)
        assert not await f.applies()          # the model is working in a form: the snapshot pass leaves it alone
        rep = await f.run()                   # fill_sections closes it and writes the store
        assert rep.failed == [], rep.failed
        st = await _state(ex)
        assert "Half typed" not in _work_by_employer(st) and len(st["work"]) == 5
    finally:
        await ex.close()


def test_work_tiles_match_by_employer_then_title_then_dates():
    tiles = [{"idx": i, "kind": "work", "title": w["title"], "sub": s, "dates": "", "invalid": False} for i, (w, s) in enumerate([
        ({"title": "Founding Software Engineer (Part-time)"}, "Revio 02/2026"),
        ({"title": "Software Engineering Intern"}, SKILLS + " 06/2026 - 08/2026"),
        ({"title": "Unnamed Job Title"}, "Southwest Airlines Baggage Revenue Challenge, Python 10/2025 - 12/2025"),
        ({"title": "Unnamed Job Title"}, "Python, SARIMAX 10/2025 - 12/2025"),
        ({"title": "Founding Software Engineer (Part-time)"}, "Revio 02/2026"),
        ({"title": "Tempo"}, "Node.js 03/2024")])]
    kept, doomed, missing = osec.match_work(tiles, ENTRIES)
    got = {t["idx"]: e["n"] for t, e in kept}
    assert got == {0: 1, 1: 2, 2: 5}
    assert [(t["idx"], why) for t, why in doomed] == [(3, "none of his stored roles"), (4, "a second entry for Revio"), (5, "none of his stored roles")]
    assert [e["n"] for e in missing] == [3, 4]
    # dates alone find an untitled entry when no employer or title does
    kept, _, _ = osec.match_work([{"idx": 0, "kind": "work", "title": "Unnamed Job Title", "sub": "", "dates": "From October 2025 to December 2025"}], ENTRIES)
    assert [e["n"] for _, e in kept] == [5]


def test_his_degree_entry_is_told_apart_from_imported_ones():
    names = ["Texas A&M University", "Texas A & M University"]
    tiles = [{"idx": 0, "title": "Engineering Honors", "sub": "Texas A&M University"}, {"idx": 1, "title": "Computer Science", "sub": "05/2028"},
             {"idx": 2, "title": "High School Diploma", "sub": "Clements High School"}, {"idx": 3, "title": "Computer Science", "sub": "Texas A&M University"}]
    mine, doomed = osec.match_education(tiles, names, "Computer Science", "Clements High School")
    assert mine["idx"] == 1
    assert [(t["idx"], why) for t, why in doomed] == [(0, "not his education entry"), (3, "a second entry for his degree")]
    assert osec.edu_tile_right({"title": "Computer Science", "sub": "Texas A&M University 08/2025 - 05/2028"}, names, "Computer Science", (8, 2025), (5, 2028))
    assert not osec.edu_tile_right({"title": "Computer Science", "sub": " 05/2028", "invalid": True}, names, "Computer Science", (8, 2025), (5, 2028))


def test_degree_lists_and_small_helpers():
    assert osec.degree_option(["Associates", "Bachelors", "Masters"], "Bachelor of Science") == "Bachelors"
    assert osec.degree_option(["Bachelor of Arts", "Bachelor of Science (BS)", "Bachelors"], "Bachelor of Science") == "Bachelor of Science (BS)"
    assert osec.degree_option(["Bachelor's Degree", "Master's Degree"], "Bachelor of Science") == "Bachelor's Degree"
    assert osec.degree_option(["Associates", "Masters"], "Bachelor of Science") is None
    assert osec.title_matches("Software Engineering Intern", "Software Engineering Intern, Quantitative Systems")
    assert osec.title_matches("Founding Software Engineer Part-time", "Founding Software Engineer (Part-time)")
    assert not osec.title_matches("Software Engineer", "Software Engineering Intern, Quantitative Systems")
    assert not osec.title_matches("Unnamed Job Title", "Unnamed Job Title")
    assert osec.city_of("College Station, TX") == "College Station" and osec.city_of("Remote") == ""
    assert osec.tile_employer({"sub": "Revio 02/2026"}) == "Revio"
    assert osec.tile_employer({"sub": SKILLS + " 06/2026 - 08/2026"}) == SKILLS


@pytest.mark.asyncio
async def test_a_snapshot_of_an_oracle_step_runs_the_harness_and_says_so(stored):
    from goat.agent.tools import RunContext, ToolDispatcher
    from goat.budget import Budget
    ex = await _open(GM)
    try:
        job = {"id": 1, "listing_id": "l1", "company": "Acme", "title": "Intern", "ats": "oracle", "url": FIX.resolve().as_uri(), "resume_file": None}
        ctx = RunContext(job=job, ex=ex, budget=Budget(1, "oracle"), classifier=None)
        out, _ = await ToolDispatcher(ctx).t_snapshot()
        assert out["outline"].startswith("HARNESS: the Experience and Education entries (tiles) hold his stored entries (added 0, replaced 6)"), out["outline"][:300]
        assert "Leave those entries alone." in out["outline"]
        kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=1")]
        assert kinds.count("adapter:sections") == 1
        _assert_his_work(await _state(ex), current_flag=False)
        # the next snapshot reads the entries and changes nothing
        out, _ = await ToolDispatcher(ctx).t_snapshot()
        assert out["outline"].startswith("HARNESS: the Experience and Education entries (tiles) hold his stored entries. Leave")
        assert [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=1")].count("adapter:sections") == 1
    finally:
        await ex.close()


def test_the_gate_sends_the_run_to_the_step_that_holds_entries_it_cannot_see(stored, monkeypatch):
    """GM Financial's shadow retry (2026-10-22): on step 1 the gate said 'call fill_sections first' for entries on step 3;
    fill_sections found no block there and the run circled. Entries on another step send the run to that step."""
    from goat import gate as g
    from goat.browser.snapshot import Snapshot
    snap = Snapshot(url="u", title="t", headings=[], items=[{"ref": "e1", "tag": "button", "label": "NEXT"}], groups=[], errors={}, challenge={})
    job = {"id": 1, "company": "Acme", "title": "Intern", "url": "u"}
    hidden = ("the Work History tile 'Software Engineering Intern' gives its employer as 'Quantitative Systems', but it is his DeKalb role: "
              "it is on another step of the form: go back to that step (Back, or its section link), take a snapshot, then open it with its Edit")
    monkeypatch.setattr(g, "oracle_tile_problems", lambda *a, **k: [hidden])
    r = g.run_gate(job=job, snapshot=snap, page_text="NEXT", expected_resume=None, free_text_answers=[], portal="oracle", page_no=1)
    assert r.result == "blocked" and r.reasons[0].startswith("Oracle's Experience / Education entries on another step"), r.reasons
    monkeypatch.setattr(g, "oracle_tile_problems", lambda *a, **k: ["the Education tile 'Engineering Honors' / '' is not his degree entry: remove it"])
    r = g.run_gate(job=job, snapshot=snap, page_text="NEXT", expected_resume=None, free_text_answers=[], portal="oracle", page_no=1)
    assert r.reasons[0].startswith("call fill_sections first") and "Engineering Honors" in r.reasons[0], r.reasons


@pytest.mark.asyncio
async def test_required_country_and_city_are_filled_from_his_own_places(stored):
    """Berkshire Hathaway Energy (2026-10-22): Save was refused over Employer Country on a work entry and Country / City on
    his degree; the model then edited the degree by hand and left its start year blank. The required boxes take his
    entry's own place: a remote role is in his country, Texas A&M is where his research role there is (College Station)."""
    ex = await _open(dict(GM, layout="timeline", requireCountry=True, currentFlag=True))
    try:
        rep = await _filler(ex).run()
        assert rep.failed == [], rep.failed
        st = await _state(ex)
        _assert_his_work(st, current_flag=True)
        assert {w["employer"]: w["country"] for w in st["work"]} == {e["company"]: "United States" for e in ENTRIES}
        assert [(d["school"], d["sm"], d["sy"], d["country"], d["city"]) for d in st["education"]] == [
            ("Texas A&M University", "August", "2025", "United States", "College Station")]
        srcs = {r["source"] for r in records.decisions(1)}
        assert "standing:experience.work.1.location" in srcs and "standing:education.school" in srcs
    finally:
        await ex.close()


GM_STEP4 = """<html><body><ul class="apply-flow-page-list">
<li style="display:none"><div class="apply-flow-block apply-flow-block--profile-import"><h3>Import your profile</h3>
  <input type="file" data-goat-ref="e3" aria-label="Import your profile from resume"><button>RESUME</button></div>
  <div class="apply-flow-block"><h3>Supporting Documents and URLs</h3>%s</div></li>
<li><div class="apply-flow-block"><h3>More About You</h3><label>Full Name <input id="sig"></label></div></li>
</ul></body></html>"""


@pytest.mark.asyncio
@pytest.mark.parametrize("attached", [True, False])
async def test_an_upload_into_step_ones_hidden_import_box_is_refused(stored, attached):
    """GM Financial (2026-10-22): on step 4 the model uploaded the résumé into step 1's hidden 'Import your profile' box (its
    ref was in no snapshot of that step) and Oracle rewrote his Work History with the import's broken entries again."""
    from goat.agent.tools import RunContext, ToolDispatcher
    from goat.budget import Budget
    slot = ('<button>Surya_Thirukonda_Resume_SWE.pdf</button><button>REMOVE RESUME</button>' if attached
            else '<input type="file" data-goat-ref="e9" aria-label="Upload Resume">')
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(GM_STEP4 % slot)
        job = {"id": 1, "listing_id": "l1", "company": "Acme", "title": "Intern", "ats": "oracle", "url": "https://acme.fa.oraclecloud.com/job/1",
               "resume_file": None}
        ctx = RunContext(job=job, ex=ex, budget=Budget(1, "oracle"), classifier=None)
        out, _ = await ToolDispatcher(ctx).t_upload_resume(ref="e3", document="resume")
        if attached:
            assert out["status"] == "set" and "already attached" in out["note"], out
            assert ctx.resume_file == "Surya_Thirukonda_Resume_SWE.pdf"
        else:
            assert "Supporting Documents" in out["error"], out
        assert await ex.page.evaluate("() => document.querySelector('[data-goat-ref=e3]').files.length") == 0
    finally:
        await ex.close()
