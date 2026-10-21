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


async def _open_html(html: str):
    ex = Executor(policy=HostPolicy(["about:blank"]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.set_content(html)
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


@pytest.mark.asyncio
async def test_a_resumed_page_gets_its_present_entries_sourced_again(stored):
    """Aerospace Corporation (2026-10-19): after a restart the earlier attempt's decisions were archived and the filled
    My Experience page it re-attached to held dates, descriptions and current-role boxes with no decision."""
    ex = await _open(FIX / "workday" / "sections.html")
    try:
        f = _filler("workday", ex)
        await snapshot.take(ex)
        await f.run()
        with db.tx() as con:
            con.execute("DELETE FROM decisions WHERE job_id=1")
        rep2 = await _filler("workday", ex).run()
        assert rep2.added == [] and rep2.failed == []
        recs = records.decisions(1)
        labels = " | ".join(r["field_label"] for r in recs)
        assert "start date" in labels and "role description" in labels and "currently work here" in labels
        refs = {r["ref"] for r in recs}
        info = await f.scan()
        rev = next(e for e in _work(info["entries"]) if (e["fields"].get("companyName") or {}).get("value") == "Revio")
        for name in ("startDate-dateSectionMonth-input", "startDate-dateSectionYear-input", "roleDescription", "currentlyWorkHere"):
            assert rev["fields"][name]["ref"] in refs, name
    finally:
        await ex.close(close_browser=True)


SKILLS_BOX = """<!doctype html><html><body style="margin:20px">
<div data-automation-id="multiSelectContainer"><div data-automation-id="selectedItem">Python (Programming Language)</div>
 <input id="skills--skills" type="text" value=""></div>
<ul id="list" role="listbox"><li role="option">FastReports</li></ul>
<script>
  const box = document.getElementById('skills--skills');
  box.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.getElementById('list').style.display = 'none';
    if (e.key === 'Backspace' && !box.value) { const c = document.querySelector('[data-automation-id="selectedItem"]'); if (c) c.remove(); }
  });
</script></body></html>"""


@pytest.mark.asyncio
async def test_the_skills_search_box_is_left_empty_and_closed(stored):
    """Moderna (2026-10-19): 'FastAPI' (not offered) stayed typed with its suggestions open; the agent's next click
    landed on one and added 'FastReports'. An empty box never gets a Backspace (it deletes the last chip)."""
    ex = await _open_html(SKILLS_BOX)
    try:
        f = _filler("workday", ex)
        await ex.page.focus("#skills--skills")
        await ex.page.keyboard.type("FastAPI")
        await f._clear_skills_search()
        st = await ex.page.evaluate("""() => ({v: document.getElementById('skills--skills').value,
            open: document.getElementById('list').style.display !== 'none',
            chips: document.querySelectorAll('[data-automation-id="selectedItem"]').length})""")
        assert st == {"v": "", "open": False, "chips": 1}
        await ex.page.focus("#skills--skills")
        await f._clear_skills_search()
        assert await ex.page.evaluate("() => document.querySelectorAll('[data-automation-id=\"selectedItem\"]').length") == 1
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_entry_whose_company_was_typed_over_gets_his_company_back(stored):
    """T-Mobile (2026-10-19): the model typed 'Aggie Data Science Club' over work entry 5's 'Southwest Airlines'. The
    filler finds that block by its job title and puts the stored company back instead of adding a sixth entry."""
    ex = await _open(FIX / "workday" / "sections.html")
    try:
        f = _filler("workday", ex)
        await snapshot.take(ex)
        await f.run()
        await ex.page.evaluate("""() => {
          const el = Array.from(document.querySelectorAll('input')).find(i => i.value === 'Southwest Airlines');
          const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          set.call(el, 'Aggie Data Science Club'); el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        f2 = _filler("workday", ex)
        rep = await f2.run()
        vals = _vals((await f2.scan())["entries"])
        assert set(vals) == {e["company"] for e in ENTRIES}, vals.keys()
        assert len(_work((await f2.scan())["entries"])) == 5
        assert "Southwest Airlines" in rep.updated and rep.added == [a for a in rep.added if a.startswith(("http", "skill"))]
    finally:
        await ex.close(close_browser=True)



@pytest.mark.asyncio
async def test_untouched_workday_entries_are_re_added_when_another_one_changes(stored):
    """Ryan (2026-10-19): on a resumed Workday draft, Save keeps only the entries the visit touched. When one entry is
    fixed, the four untouched ones are deleted and added again so that all five belong to this visit."""
    ex = await _open(FIX / "workday" / "sections.html")
    try:
        f = _filler("workday", ex)
        await snapshot.take(ex)
        await f.run()
        await ex.page.evaluate("""() => {
          const el = Array.from(document.querySelectorAll('input')).find(i => i.value === 'Southwest Airlines');
          const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          set.call(el, 'Aggie Data Science Club'); el.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        f2 = _filler("workday", ex)
        rep = await f2.run()
        readded = [u for u in rep.updated if "re-added" in u]
        assert len(readded) == 4, rep.summary()
        info = await f2.scan()
        vals = _vals(info["entries"])
        assert set(vals) == {e["company"] for e in ENTRIES} and len(_work(info["entries"])) == 5
        rep3 = await _filler("workday", ex).run()                         # nothing changes now: nothing is re-added
        assert not [u for u in rep3.updated if "re-added" in u]
    finally:
        await ex.close(close_browser=True)



def test_a_question_mentioning_work_experience_is_no_work_section(stored):
    """Aptiv (2026-10-19): its My Experience step has only Resume/CV and Websites; 'Which industry best describes your
    most recent work experience?' made the Review gate demand work entries 2-5."""
    from goat.adapters.workday_sections import review_gaps
    text = ("Review\nMy Experience\nResume/CV\nSurya_Thirukonda_Resume_SWE.pdf\nWebsites\nURL\nhttps://github.com/SuryaThirukonda\n"
            "Which industry best describes your most recent work experience? (Select one)*\nSoftware / SaaS / Cloud / AI\n"
            "Who is your current or most recent employer?\nRevio")
    assert not [g for g in review_gaps(text) if g.startswith("work entry")]
    with_section = text.replace("Websites\n", "Work Experience\nRevio\nWebsites\n")
    assert [g for g in review_gaps(with_section) if g.startswith("work entry")]


@pytest.mark.asyncio
async def test_an_untouched_entry_without_a_delete_button_is_retyped(stored):
    """Adobe (2026-10-20): the first Workday block had no Delete button, so it was never re-added and Workday dropped
    it on every Save. Its title is re-typed instead."""
    ex = await _open(FIX / "workday" / "sections.html")
    try:
        f = _filler("workday", ex)
        await snapshot.take(ex)
        await f.run()
        await ex.page.evaluate("""() => {
          const el = Array.from(document.querySelectorAll('input')).find(i => i.value === 'Southwest Airlines');
          const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          set.call(el, 'Aggie Data Science Club'); el.dispatchEvent(new Event('input', {bubbles: true}));
          const first = document.querySelector('input[id$="--jobTitle"]');
          let node = first; for (let i = 0; i < 14 && node; i++, node = node.parentElement) {
            const del = Array.from(node.querySelectorAll('button')).find(b => /^delete$/i.test((b.textContent || '').trim()) || /^delete$/i.test(b.getAttribute('aria-label') || ''));
            if (del) { del.remove(); break; }
          }
        }""")
        rep = await _filler("workday", ex).run()
        assert any("re-typed" in u for u in rep.updated), rep.summary()
        assert not any("no Delete button" in x for x in rep.skipped), rep.summary()
    finally:
        await ex.close(close_browser=True)



def test_an_empty_languages_section_on_review_is_a_gap(monkeypatch):
    """Cigna, DFW, Intel (2026-10-20): 'Languages No Response' on Workday's Review page."""
    from goat.adapters import workday_sections as ws
    monkeypatch.setattr(ws, "stored_work_entries", lambda company="": [])
    monkeypatch.setattr(ws, "stored_websites", lambda company="": [])
    page = "My Experience\nWork Experience\nRevio\nLanguages\nNo Response\nApplication Questions"
    assert ws.review_gaps(page) == [ws.LANGUAGES_GAP]
    assert ws.review_gaps("My Experience\nLanguages\nEnglish\nFluent") == []



def test_a_skill_never_takes_another_technology_by_prefix():
    """Solar Turbines, Q2 (2026-10-20): 'React VR' and 'React Native' were taken for React."""
    from goat.adapters.workday_sections import pick_skill
    assert pick_skill("React", ["React VR"]) is None
    assert pick_skill("React", ["React Native"]) is None
    assert pick_skill("React", ["React.js", "React Native"]) == "React.js"
    assert pick_skill("Python", ["Python 3"]) == "Python 3"
    assert pick_skill("Docker", ["Docker Swarm"]) is None
    assert pick_skill("SQL", ["Structured Query Language (SQL)", "SQLCLR"]) == "Structured Query Language (SQL)"



def test_react_never_takes_the_react_framework():
    """Solar Turbines (2026-10-20): 'ReAct Framework' (an AI agent pattern) was taken for React."""
    from goat.adapters.workday_sections import pick_skill
    assert pick_skill("React", ["ReAct Framework"]) is None
    assert pick_skill("React", ["ReAct Framework", "React.js"]) == "React.js"



@pytest.mark.asyncio
async def test_a_professional_experience_section_gets_every_entry(stored):
    """Rogue Fitness (2026-10-20): the section is headed 'Professional Experience'; only the pre-rendered block was filled
    and entries 2-5 failed with 'Add button not found'."""
    html = (FIX / "workday" / "sections.html").read_text(encoding="utf-8").replace("Work Experience", "Professional Experience")
    ex = await _open_html(html)
    try:
        f = _filler("workday", ex)
        await snapshot.take(ex)
        rep = await f.run()
        assert not [x for x in rep.failed if "Add button" in x], rep.summary()
        info = await f.scan()
        assert set(_vals(info["entries"])) == {e["company"] for e in ENTRIES}
    finally:
        await ex.close(close_browser=True)


def test_the_review_page_names_an_empty_professional_experience_section():
    assert ws.WORK_HEADING.search("Professional Experience") and ws.WORK_HEADING.search("Work Experience 3")
    assert ws.WORK_HEADING.search("Employment History") and not ws.WORK_HEADING.search("My Experience")
    assert ws.is_experience_page("", ["Professional Experience"])
