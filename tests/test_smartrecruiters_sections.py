"""SmartRecruiters' one-click form (CapTech, Wellmark, ServiceNow 2026-10-21): every control lives in a custom element's
shadow root. The snapshot names the buttons by their host, the click guards still recognise a Submit drawn that way, and
the harness writes the Experience / Education cards from the stored entries (parsed project cards removed)."""
import json
from pathlib import Path

import pytest

from goat import db, records
from goat.adapters import smartrecruiters_sections as sr
from goat.browser import js, snapshot
from goat.browser.executor import Executor, HostPolicy

FIX = Path(__file__).parent / "fixtures" / "smartrecruiters" / "oneclick.html"
NL = chr(10)

ENTRIES = [
    {"n": 1, "company": "Revio", "title": "Founding Software Engineer (Part-time)", "location": "Remote", "start": "02/01/2026", "end": "",
     "current": True, "description": "• Launched a beta. • Kept documents intact."},
    {"n": 2, "company": "DeKalb Capital Management", "title": "Software Engineering Intern", "location": "Remote", "start": "06/01/2026",
     "end": "08/20/2026", "current": False, "description": "• Built analytics; synced positions. • Synced positions."},
    {"n": 3, "company": "Mai Shan Yun (WaiterDock)", "title": "Contract Software Engineer", "location": "College Station, TX", "start": "02/01/2026",
     "end": "06/30/2026", "current": False, "description": "• Won the challenge."},
    {"n": 4, "company": "Rathinam Group, Texas A&M", "title": "Undergraduate Research Assistant", "location": "College Station, TX",
     "start": "09/01/2026", "end": "", "current": True, "description": ""},
    {"n": 5, "company": "Southwest Airlines", "title": "Data Analyst", "location": "College Station, TX", "start": "10/01/2025",
     "end": "12/15/2025", "current": False, "description": "• Forecast baggage revenue."},
]
EDU = {"education.school": "Texas A&M University", "education.school_variants": "Texas A & M University; Texas A&M University - College Station",
       "education.high_school": "Clements High School", "education.major": "Computer Science", "education.degree": "Bachelor of Science",
       "education.start": "August 2025", "education.graduation": "May 2028", "identity.city": "Sugar Land", "identity.state": "Texas"}


@pytest.fixture()
def stored(monkeypatch, tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(sr, "stored_work_entries", lambda company="", portal="": [dict(e) for e in ENTRIES])
    monkeypatch.setattr(sr.SmartRecruitersSections, "_answer", lambda self, key: EDU.get(key, ""))
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Acme','Intern','https://jobs.smartrecruiters.com/Acme/1','smartrecruiters')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','no_account',0)")


async def _open(html: str | None = None) -> Executor:
    url = FIX.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    if html is None:
        await ex.page.goto(url)
    else:
        await ex.page.set_content(html)
    await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_the_filler_writes_every_stored_entry_and_removes_parsed_cards(stored):
    ex = await _open()
    try:
        f = sr.SmartRecruitersSections(ex, {"id": 1, "company": "Acme"}, 1)
        assert await f.applies()
        rep = await f.run()
        assert rep.failed == [], rep.failed
        assert rep.present == ["experience: Revio"]
        assert [x for x in rep.updated if "Tempo" in x] and [x for x in rep.updated if "Engineering Honors" in x]
        assert len([x for x in rep.added if x.startswith("experience:")]) == 4 and "education: Texas A&M University" in rep.added
        cards = await f.cards("experience")
        assert sorted(c["company"] for c in cards) == sorted(e["company"] for e in ENTRIES)
        by = {c["company"]: c for c in cards}
        assert by["DeKalb Capital Management"]["date"] == "2026 - 2026" and by["Rathinam Group, Texas A&M"]["date"] == "2026 - Present"
        assert by["Southwest Airlines"]["name"] == "Data Analyst" and by["Southwest Airlines"]["date"] == "2025 - 2025"
        edu = await f.cards("education")
        assert [(c["name"], c["date"], c["major"], c["degree"]) for c in edu] == [
            ("Texas A&M University", "2025 - 2028", "Computer Science", "Bachelor of Science")]
        # descriptions go in with real line breaks, one bullet per line
        descs = await ex.page.evaluate("() => window.__descs")
        assert ("• Built analytics, synced positions." + NL + "• Synced positions.") in descs      # no semicolon (Wellmark)
        # every added value is recorded with its stored key
        srcs = {r["source"] for r in records.decisions(1)}
        assert {"standing:experience.work.2.title", "standing:experience.work.2.company", "standing:experience.work.2.start",
                "standing:experience.work.2.end", "standing:experience.work.4.current", "standing:education.school",
                "standing:education.graduation"} <= srcs
        # a second pass changes nothing
        again = await sr.SmartRecruitersSections(ex, {"id": 1, "company": "Acme"}, 1).run()
        assert again.added == [] and again.updated == [] and again.failed == []
        assert len([x for x in again.present if x != "city"]) == 6
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_an_editor_left_open_is_cancelled_before_the_cards_are_written(stored):
    ex = await _open()
    try:
        await ex.page.locator('[data-test="add-experience"] button').first.click()
        await ex.page.locator('oc-experience-edit-form [data-test="job-title-autocomplete"] input').first.fill("Half typed")
        rep = await sr.SmartRecruitersSections(ex, {"id": 1, "company": "Acme"}, 1).run()
        assert rep.failed == []
        assert await ex.page.locator("oc-experience-edit-form").count() == 0
        assert len(await sr.SmartRecruitersSections(ex, {"id": 1, "company": "Acme"}, 1).cards("experience")) == 5
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_the_snapshot_names_shadow_root_buttons_by_their_host():
    ex = await _open()
    try:
        snap = await snapshot.take(ex)
        labels = {i.get("label"): i for i in snap.items}
        for name in ("Add experience entry", "Add education entry", "Next", "Delete experience Tempo"):
            assert name in labels, sorted(k for k in labels if k)
        assert not labels["Next"].get("submitish")
        await ex.page.locator('[data-test="add-experience"] button').first.click()
        snap = await snapshot.take(ex)
        labels = {i.get("label"): i for i in snap.items}
        assert "Save experience entry" in labels and labels["I currently work here"]["type"] == "checkbox"
    finally:
        await ex.close()


SHADOW_SUBMIT = """<html><body>
<input value="Surya"><input value="Thirukonda"><input value="x@y.z">
<oc-button><spl-button id="s">Submit</spl-button></oc-button>
<script>customElements.define('spl-button', class extends HTMLElement { connectedCallback() {
  this.attachShadow({ mode: 'open' }).innerHTML = '<button type="button" style="min-width:80px;min-height:30px"><slot></slot></button>'; } });</script>
</body></html>"""


@pytest.mark.asyncio
async def test_a_submit_drawn_inside_a_shadow_root_is_still_a_submit_control():
    ex = await _open(SHADOW_SUBMIT)
    try:
        snap = await snapshot.take(ex)
        item = next(i for i in snap.items if i.get("label") == "Submit")
        assert item.get("submitish")
        x, y = item["bbox"][0], item["bbox"][1]
        el = await ex.element_at(x, y)
        assert el.submit, el
        guard = await ex.eval(js.JS_TARGET_GUARD, item["ref"])
        assert guard.get("found") and guard.get("submit"), guard
        found = await ex.eval(js.JS_FIND_SUBMIT, "")
        assert found.get("ok") and found.get("text") == "Submit", found
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_the_autocomplete_resume_box_is_told_apart_from_the_resume_box():
    ex = await _open()
    try:
        snap = await snapshot.take(ex)
        files = sorted((i for i in snap.items if i.get("type") == "file"), key=lambda i: i["bbox"][1])
        assert len(files) == 2
        top, bottom = files
        assert await ex.eval(js.JS_INSIDE_TAG, [top["ref"], ["oc-apply-with-resume"]]) == "oc-apply-with-resume"
        assert await ex.eval(js.JS_INSIDE_TAG, [bottom["ref"], ["oc-apply-with-resume"]]) == ""
    finally:
        await ex.close()


def test_month_year_from_every_stored_date_shape():
    assert sr.month_year("02/01/2026") == "02/2026"
    assert sr.month_year("August 2025") == "08/2025"
    assert sr.month_year("May 2028") == "05/2028"
    assert sr.month_year("") == ""
    assert json.loads(json.dumps(sr.month_year("2025-10-01"))) == "10/2025"


@pytest.mark.asyncio
async def test_an_education_editor_without_dates_still_takes_his_school_and_is_not_redone(stored):
    """Wellmark (2026-10-21): the Education editor has no From / To; the filler waited 30 s for one and raised."""
    ex = await _open()
    try:
        await ex.page.evaluate("() => { window.EDU_NO_DATES = true; }")
        rep = await sr.SmartRecruitersSections(ex, {"id": 1, "company": "Acme"}, 1).run()
        assert "education: Texas A&M University" in rep.added and not [f for f in rep.failed if f.startswith("education")], rep.failed
        edu = await sr.SmartRecruitersSections(ex, {"id": 1, "company": "Acme"}, 1).cards("education")
        assert [(c["name"], c["date"]) for c in edu] == [("Texas A&M University", "")]
        again = await sr.SmartRecruitersSections(ex, {"id": 1, "company": "Acme"}, 1).run()
        assert again.added == [] and again.updated == [] and "education: Texas A&M University" in again.present
    finally:
        await ex.close()


DROPZONE = """<html><body><oc-resume-upload><spl-dropzone></spl-dropzone></oc-resume-upload>
<script>customElements.define('spl-dropzone', class extends HTMLElement { connectedCallback() {
  this.attachShadow({ mode: 'open' }).innerHTML = '<span>Surya_Thirukonda_Resume_SWE.pdf</span><button aria-label="Delete file Surya_Thirukonda_Resume_SWE.pdf">x</button>'; } });</script>
</body></html>"""


@pytest.mark.asyncio
async def test_a_file_shown_inside_a_shadow_root_counts_as_attached():
    """Wellmark (2026-10-21): 'page does not show Surya_Thirukonda_Resume_SWE.pdf' although the drop zone showed it."""
    from goat.browser import fill
    ex = await _open(DROPZONE)
    try:
        assert await fill._shows_file(ex, Path("Surya_Thirukonda_Resume_SWE.pdf"))
        assert not await fill._shows_file(ex, Path("Other_File.pdf"))
    finally:
        await ex.close()



@pytest.mark.asyncio
async def test_the_city_box_takes_his_city_in_his_state(stored):
    """ServiceNow (2026-10-21): the personal-information City keeps only a picked suggestion; the model could not set it."""
    ex = await _open()
    try:
        rep = await sr.SmartRecruitersSections(ex, {"id": 1, "company": "Acme"}, 1).run()
        assert "city: Sugar Land, TX, US" in rep.added, rep.added
        again = await sr.SmartRecruitersSections(ex, {"id": 1, "company": "Acme"}, 1).run()
        assert "city" in again.present
    finally:
        await ex.close()



HOST_PICK = """<html><body><oc-personal-information>
<spl-autocomplete id="city" label="City*"></spl-autocomplete>
<spl-dropzone></spl-dropzone></oc-personal-information>
<script>
customElements.define('spl-input', class extends HTMLElement { connectedCallback() {
  this.attachShadow({ mode: 'open' }).innerHTML = '<div class="field"><label>City*</label> <input type="text" required></div>'; } });
customElements.define('spl-autocomplete', class extends HTMLElement { connectedCallback() {
  this.value = { displayString: 'Sugar Land, TX, US', city: 'Sugar Land' };
  this.attachShadow({ mode: 'open' }).innerHTML = '<spl-input></spl-input>'; } });
customElements.define('spl-dropzone', class extends HTMLElement { connectedCallback() {
  this.attachShadow({ mode: 'open' }).innerHTML = '<div>Surya_Thirukonda_Resume_SWE.pdf</div>'; } });
</script></body></html>"""


@pytest.mark.asyncio
async def test_a_pick_kept_on_the_host_is_the_fields_value_and_shadow_text_is_page_text():
    """ServiceNow (2026-10-21): City showed '' after a page change (the host held the pick), and the gate called the
    attached résumé missing (its name only inside a shadow root)."""
    ex = await _open(HOST_PICK)
    try:
        snap = await snapshot.take(ex)
        city = next(i for i in snap.items if (i.get("label") or "").startswith("City"))
        assert city.get("shown") == "Sugar Land, TX, US", city
        assert not any("City" in (r.get("label") or "") for r in snap.errors.get("empty_required", [])), snap.errors
        assert "Surya_Thirukonda_Resume_SWE.pdf" in await ex.page_text(30000)
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_the_read_back_takes_a_pick_kept_on_the_host():
    """ServiceNow (2026-10-21): the read-back before Next called City '' while its host held the pick."""
    ex = await _open(HOST_PICK)
    try:
        snap = await snapshot.take(ex)
        city = next(i for i in snap.items if (i.get("label") or "").startswith("City"))
        live = await ex.eval(js.JS_READ_REFS, [city["ref"]])
        assert live[city["ref"]].get("shown") == "Sugar Land, TX, US", live
        one = await ex.read_ref(city["ref"])
        assert one.get("shown") == "Sugar Land, TX, US", one
    finally:
        await ex.close()


SHADOW_NEXT = """<html><body>
<div id="out">none</div>
<oc-footer><spl-button id="n"><span>Next</span></spl-button></oc-footer>
<oc-footer><spl-button id="c" style="position:relative"><span>Back</span></spl-button></oc-footer>
<div id="veil" style="position:absolute;left:0;top:0;width:100%;height:0"></div>
<script>customElements.define('spl-button', class extends HTMLElement { connectedCallback() {
  const r = this.attachShadow({ mode: 'open' });
  r.innerHTML = '<button type="button" style="min-width:80px;min-height:30px"><slot></slot></button>';
  r.querySelector('button').addEventListener('click', () => { document.getElementById('out').textContent = this.id; }); } });</script>
</body></html>"""


@pytest.mark.asyncio
async def test_a_shadow_button_with_a_slotted_label_takes_the_agents_click():
    """ServiceNow (SmartRecruiters, 2026-10-21): the point hit Next's own slotted span, the hit test called the button
    covered, and the fallback looked the ref up outside the shadow root: every Next click was reported done and did
    nothing."""
    ex = await _open(SHADOW_NEXT)
    try:
        snap = await snapshot.take(ex)
        nxt = next(i for i in snap.items if i.get("label") == "Next")
        info = await ex.read_ref(nxt["ref"])
        assert await ex._hit(nxt["ref"], int(info["x"]), int(info["y"])) == "ok"
        out = await ex.click_ref(nxt["ref"])
        assert not out.get("covered_by")
        assert await ex.page.evaluate("() => document.getElementById('out').textContent") == "n"
        # covered by something else: the direct click still reaches the button inside its shadow root
        back = next(i for i in snap.items if i.get("label") == "Back")
        await ex.page.evaluate("""() => { const b = document.getElementById('c').getBoundingClientRect();
          const v = document.getElementById('veil'); v.style.top = (b.top - 5) + 'px'; v.style.height = (b.height + 10) + 'px'; }""")
        out = await ex.click_ref(back["ref"])
        assert out.get("covered_by") and await ex.page.evaluate("() => document.getElementById('out').textContent") == "c"
    finally:
        await ex.close()
