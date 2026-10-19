"""Epic Games (Greenhouse questions on its own careers site, 2026-10-19): the school search opened on the first
100 schools A to Z and never offered Texas A&M; the start / end month dropdowns took text and failed silently;
the Gender Identity boxes ("Man") were sent to a picker and his stored "Male" had no "Man"."""
import pytest

from goat import answers
from goat.browser import fill, snapshot
from goat.browser.fill import FieldResult
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures
from tests.test_page_readback import _race

SCHOOL_SEARCH = """<!doctype html><html><body style="margin:20px">
<div class="field"><label for="sch">School</label>
  <input id="sch" role="combobox" aria-autocomplete="list" autocomplete="off"><ul id="res" role="listbox"></ul></div>
<script>
  const DB = ["Aalborg University", "Aalto University", "Aarhus University", "Texas A&M University",
              "Texas A&M University - Commerce", "Texas State University"];
  const inp = document.getElementById('sch'), res = document.getElementById('res');
  function show(list) {
    res.innerHTML = '';
    for (const u of list) {
      const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = u;
      li.addEventListener('mousedown', (e) => { e.preventDefault(); inp.value = u; res.innerHTML = ''; });
      res.appendChild(li);
    }
  }
  const firstPage = () => { if (!inp.value) show(DB.slice(0, 3)); };     // opens on the first schools, A to Z
  inp.addEventListener('focus', firstPage); inp.addEventListener('click', firstPage);
  let t = null;
  inp.addEventListener('input', () => {                                   // a server search, one substring
    clearTimeout(t); res.innerHTML = '';
    const q = inp.value.trim().toLowerCase();
    t = setTimeout(() => show(q ? DB.filter(u => u.toLowerCase().includes(q)) : DB.slice(0, 3)), 300);
  });
</script></body></html>"""


@pytest.mark.asyncio
async def test_a_search_that_opens_on_its_first_page_gets_the_shorter_queries(tmp_path):
    ex = await _race(tmp_path, SCHOOL_SEARCH, "school.html")
    try:
        snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if (i.get("label") or "").startswith("School"))
        r = await fill.choose(ex, ref, ["Texas A & M University", "Texas A&M University - College Station", "TX-Texas A&M University"])
        assert r.ok, r.error
        assert await ex.page.evaluate("() => document.getElementById('sch').value") == "Texas A&M University"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_the_variants_key_puts_the_answer_itself_first(store, monkeypatch):
    answers.save_answer("education.school", "Texas A&M University", source="questionnaire")
    answers.save_answer("education.school_variants", "Texas A & M University; Texas A&M", answer_type="choice_chain", source="operator")
    seen = {}

    async def fake_choose(ex, ref, wants, **kw):
        seen["wants"] = list(wants)
        return FieldResult(ref, True, "Texas A&M University")

    monkeypatch.setattr(fill, "choose", fake_choose)
    d, ctx = _dispatcher(_job())
    out, _ = await d.t_choose("e9", "education.school_variants", [], "School", None)
    assert out["status"] == "set"
    assert seen["wants"][:3] == ["Texas A&M University", "Texas A & M University", "Texas A&M"]


GENDER = """<!doctype html><html><body style="margin:20px">
<fieldset><legend>Gender Identity *</legend>
  <label><input type="checkbox" name="g" value="man"> Man</label>
  <label><input type="checkbox" name="g" value="woman"> Woman</label>
  <label><input type="checkbox" name="g" value="nb"> Non-Binary</label>
  <label><input type="checkbox" name="g" value="decline"> I don't wish to answer</label></fieldset>
</body></html>"""


@pytest.mark.asyncio
async def test_choose_on_a_group_box_ticks_the_chain_option(store, tmp_path):
    answers.save_answer("self_id.gender", "Male; Man", answer_type="choice_chain", source="seed:delegation.yaml")
    ex = await _race(tmp_path, GENDER, "gender.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        refs = {o["label"].strip(): o["ref"] for g in snap.groups for o in g["options"]}
        # the model offers its own wording: the self-ID guard drops it, the stored chain decides
        out, _ = await d.t_choose(refs["Woman"], "self_id.gender", ["Woman"], "Gender Identity", None)
        res = out["results"][0]
        assert res["status"] == "failed" and "Man" in res["error"]
        out, _ = await d.t_choose(refs["Man"], "self_id.gender", [], "Gender Identity", None)
        assert out["results"][0]["status"] == "set"
        ticked = await ex.page.evaluate("() => Array.from(document.querySelectorAll('input:checked')).map(i => i.value)")
        assert ticked == ["man"]
    finally:
        await ex.close(close_browser=True)


def test_date_wants_only_reads_dates():
    assert fill.date_wants(["August 2025"]) == ["August", "Aug", "2025"]
    assert fill.date_wants(["08/2025"]) == ["August", "Aug", "2025"]
    assert fill.date_wants(["2028-05"]) == ["May", "2028"]
    assert fill.date_wants(["Sept. 2027"]) == ["September", "Sep", "2027"]
    assert fill.date_wants(["Marketing", "Texas A&M University", "LinkedIn"]) == []


MONTHS = """<!doctype html><html><body style="margin:20px">
<label for="m">Start Date (Month)</label>
<select id="m"><option value="">Select</option>""" + "".join(
    f"<option>{m}</option>" for m in ["January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"]) + """</select>
<label for="rs">End Date (Month)</label>
<input id="react-select-7-input" role="combobox" aria-autocomplete="list">
<script>
  // a react-select's text box: it keeps only what is picked from its list
  const rs = document.getElementById('react-select-7-input');
  rs.addEventListener('input', () => setTimeout(() => { rs.value = ''; }, 0));
  rs.addEventListener('change', () => { rs.value = ''; });
</script></body></html>"""


@pytest.mark.asyncio
async def test_a_month_list_takes_the_month_of_a_month_year_answer(tmp_path):
    ex = await _race(tmp_path, MONTHS, "months.html")
    try:
        snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if i.get("tag") == "select")
        r = await fill.choose(ex, ref, ["August 2025"])
        assert r.ok, r.error
        assert await ex.page.evaluate("() => document.getElementById('m').value") == "August"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_text_sent_to_a_dropdown_box_says_to_use_choose(tmp_path):
    ex = await _race(tmp_path, MONTHS, "months2.html")
    try:
        snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if (i.get("role") == "combobox" or "react-select" in (i.get("id") or "")) and i.get("tag") == "input")
        out = await fill.fill_batch(ex, [{"ref": ref, "value": "05"}])
        assert not out[0].ok and "use choose" in out[0].error
    finally:
        await ex.close(close_browser=True)


WORKDAY_PROMPT = """<!doctype html><html><body style="margin:20px">
<div data-automation-id="formField-fieldOfStudy"><label for="education-253--fieldOfStudy"><span>Field of Study<abbr>*</abbr></span></label>
 <div data-automation-id="multiSelectContainer" data-uxi-widget-type="multiselect"><div data-automation-id="multiselectInputContainer">
  <input placeholder="Search" aria-required="true" autocomplete="off" data-uxi-widget-type="selectinput" id="education-253--fieldOfStudy" value="">
  <div data-automation-id="promptAriaInstruction" aria-hidden="true">0 items selected</div></div></div></div>
<script>document.getElementById('education-253--fieldOfStudy').addEventListener('blur', e => { e.target.value = ''; });</script>
</body></html>"""


@pytest.mark.asyncio
async def test_a_workday_prompt_is_a_combobox_and_refuses_typed_text(tmp_path):
    """Aerospace Corporation (2026-10-19): 'Computer Science' typed into Field of Study never committed."""
    ex = await _race(tmp_path, WORKDAY_PROMPT, "prompt.html")
    try:
        snap = await snapshot.take(ex)
        item = next(i for i in snap.items if (i.get("label") or "").startswith("Field of Study"))
        assert item.get("combo")
        assert "combobox" in snapshot.render(snap)
        out = await fill.fill_batch(ex, [{"ref": item["ref"], "value": "Computer Science"}])
        assert not out[0].ok and "use choose" in out[0].error
    finally:
        await ex.close(close_browser=True)


ENROLL = """<!doctype html><html><body style="margin:20px">
<label for="st">Current college enrollment status</label>
<select id="st"><option>Select One</option><option>Full-time Status</option><option>Part-time Status</option></select>
</body></html>"""


@pytest.mark.asyncio
async def test_a_computer_pick_is_kept_only_when_it_shows_the_stored_answer(store, tmp_path):
    """Aerospace Corporation (2026-10-19): after the picker failed, the model chose 'Part-time Status' with the computer
    tool and recorded it 'as_is', i.e. as Surya's own choice."""
    from goat import db
    answers.save_answer("education.enrollment_status", "Full-time; Full-time Status", answer_type="choice_chain", source="questionnaire")
    ex = await _race(tmp_path, ENROLL, "enroll.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        ref = next(i["ref"] for i in snap.items if i.get("tag") == "select")
        await ex.page.select_option("#st", label="Part-time Status")
        row = {"ref": ref, "answer_key": "education.enrollment_status", "value": None, "source": "computer", "label": "Enrollment status"}
        out, _ = await d.t_fill_batch([row])
        assert out["results"][0]["status"] == "failed" and "Part-time" in out["results"][0]["error"]
        await ex.page.select_option("#st", label="Full-time Status")
        out, _ = await d.t_fill_batch([row])
        assert out["results"][0]["status"] == "kept"
        # 'as_is' is Surya's own fill: refused until he said so on this job
        out, _ = await d.t_fill_batch([{**row, "answer_key": None, "source": "as_is"}])
        assert out["results"][0]["status"] == "failed" and "computer" in out["results"][0]["error"]
        with db.tx() as con:
            con.execute("INSERT INTO pending_questions(job_id, key, question, options, field_type, asked_at, deadline_at, answered_at, answer)"
                        " VALUES(?,?,?,?,?,?,?,?,?)", (ctx.job_id, "q.x", "?", "[]", "text", 1, 2, 3, "AS_IS"))
        out, _ = await d.t_fill_batch([{**row, "answer_key": None, "source": "as_is"}])
        assert out["results"][0]["status"] == "kept"
    finally:
        await ex.close(close_browser=True)


EPIC_STARS = """<!doctype html><html><body style="margin:20px">
<div><div><label for="Email"><span>Email\u2060*\u2060:</span></label></div><div><div><input name="questions.email" type="text" placeholder="Enter" value=""></div></div></div>
<div><div><label for="Nick"><span>Preferred First Name\u2060\u2060:</span></label></div><div><div><input name="questions.nick" type="text" value=""></div></div></div>
<div><div><label for="educations[0].school_name_id"><span>School\u2060\u2060\u2060*\u2060:</span></label></div>
  <div><div><div><div><div>Select</div><div><div><input id="react-select-13-input" type="text" aria-autocomplete="list" value=""></div></div></div></div></div></div></div>
<div><div><label for="Gender Identity"><span>Gender Identity\u2060*\u2060:</span></label></div><div></div>
  <div><label><input name="g" type="radio" value="1"> Man</label></div><div><label><input name="g" type="radio" value="2"> Woman</label></div></div>
</body></html>"""


@pytest.mark.asyncio
async def test_a_star_in_the_visible_label_makes_a_field_required(tmp_path):
    """Epic Games (2026-10-19): Email, School and Gender Identity carried '*' only in their labels (between word joiners);
    the gate passed with all of them blank and only the judge noticed."""
    ex = await _race(tmp_path, EPIC_STARS, "stars.html")
    try:
        snap = await snapshot.take(ex)
        req = {(i.get("label") or "").split(":")[0]: bool(i.get("required")) for i in snap.items}
        assert req.get("Email*") and req.get("School*") and not req.get("Preferred First Name"), req
        assert "\u2060" not in " ".join(i.get("label") or "" for i in snap.items)
        empty = " | ".join(str(e) for e in snap.required_empty())
        assert "questions.email" in empty or "Email" in empty, empty
        assert "react-select-13-input" in empty or "School" in empty, empty
        assert "nick" not in empty.lower()
        assert any(g.get("required") for g in snap.groups if "Gender" in (g.get("label") or "")), snap.groups
    finally:
        await ex.close(close_browser=True)


STATUS = """<!doctype html><html><body style="margin:20px">
<label for="im">What is your current immigration status?</label>
<select id="im"><option>Select One</option><option>U.S. Citizen</option><option>Permanent Resident</option><option>Not Applicable</option></select>
<label for="mx">If applicable, when does your current U.S immigration status max-out?</label>
<select id="mx"><option>Select One</option><option>Within 6 months</option><option>Not Applicable</option></select>
</body></html>"""


@pytest.mark.asyncio
async def test_an_option_only_the_model_offered_is_recorded_as_judgment(store, tmp_path):
    """Nike (2026-10-19): choose put 'Not Applicable' under work_authorization.us_citizen = Yes and called it the
    portal's wording for Yes. Such a pick is now the model's judgment, which the fact check verifies."""
    from goat import records
    answers.save_answer("work_authorization.us_citizen", "Yes", source="questionnaire")
    answers.save_answer("q.current_u_s_immigration_status", "U.S. Citizen; US Citizen; Not Applicable", answer_type="choice_chain", source="operator")
    ex = await _race(tmp_path, STATUS, "status.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        refs = [i["ref"] for i in snap.items if i.get("tag") == "select"]
        out, _ = await d.t_choose(refs[0], "q.current_u_s_immigration_status", [], "Immigration status", None)
        assert out["readback"] == "U.S. Citizen" and "came from your options" not in out.get("note", "")
        out, _ = await d.t_choose(refs[1], "work_authorization.us_citizen", ["Not Applicable"], "Status max-out", None)
        assert out["readback"] == "Not Applicable" and "came from your options" in out["note"]
        srcs = {r["field_label"]: r["source"] for r in records.decisions(ctx.job_id)}
        assert srcs["Immigration status"] == "standing:q.current_u_s_immigration_status"
        assert srcs["Status max-out"] == "judgment:work_authorization.us_citizen"
        flag, ev = answers.unconfirmed_source("judgment:work_authorization.us_citizen")
        assert flag and "work_authorization.us_citizen = 'Yes'" in ev
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_government_question_that_counts_public_universities_goes_to_surya(store):
    """Booz Allen (2026-10-19): question 6 counts public universities as Government Entities; the stored No (federal
    government) was about to settle it although he worked in a Texas A&M research group."""
    answers.save_answer("bank.gov-employee", "No", source="seed:answer_bank.json")
    d, ctx = _dispatcher(_job())
    q = ("6. State, Local, & Non-U.S. Government Employment: employment by a Government Entity includes public universities "
         "and state agencies.")
    out, _ = await d.t_lookup_answer(q, ["I have never been employed by a Government Entity", "I am currently employed by a Government Entity"], None, "e49")
    assert out.get("miss") and "ask_user" in out["hint"] and "public" in out["hint"]
    out, _ = await d.t_lookup_answer("Are you a current or former U.S. federal government employee?", ["Yes", "No"], None, "e50")
    assert "public / state universities" not in str(out.get("hint", ""))


EPIC_DEMO = """<!doctype html><html><body style="margin:20px"><div class="DemographicQuestions__Styled">
<div><div><div><label for="Race/Ethnicity"><span>Race/Ethnicity\u2060*\u2060:</span></label></div><div></div>
  <div><label><input name="r" type="radio" value="1"> Hispanic or Latino</label></div>
  <div><label><input name="r" type="radio" value="2"> Asian (Not Hispanic or Latino)</label></div>
  <div><label><input name="r" type="radio" value="3"> I don't wish to answer</label></div></div></div>
<div><div><div><label for="Veteran Status"><span>Veteran Status\u2060*\u2060:</span></label></div><div></div>
  <div><label><input name="v" type="radio" value="1"> Yes</label></div>
  <div><label><input name="v" type="radio" value="2"> No</label></div></div></div>
</div></body></html>"""


@pytest.mark.asyncio
async def test_each_radio_group_takes_its_own_question_label(tmp_path):
    """Epic Games (2026-10-19): a container classed '...Questions' held every demographic question, so each group got
    the first label inside it; 'Hispanic or Latino' (an option) read as a question and Veteran Status had no label."""
    ex = await _race(tmp_path, EPIC_DEMO, "demo.html")
    try:
        snap = await snapshot.take(ex)
        labels = sorted((g.get("label") or "").split(":")[0] for g in snap.groups)
        assert labels == ["Race/Ethnicity*", "Veteran Status*"], labels
        assert all(g.get("required") for g in snap.groups)
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_wording_in_his_chain_stays_his_even_when_the_model_offers_it_too(store, tmp_path):
    """Nike (2026-10-19): 'Not Applicable' is the last entry of his immigration-status chain; the model offered it as well
    and the pick was logged as the model's judgment."""
    from goat import records
    answers.save_answer("q.current_u_s_immigration_status", "U.S. Citizen; Not Applicable", answer_type="choice_chain", source="operator")
    ex = await _race(tmp_path, STATUS.replace("<option>U.S. Citizen</option>", ""), "status2.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        ref = next(i["ref"] for i in snap.items if i.get("tag") == "select")
        out, _ = await d.t_choose(ref, "q.current_u_s_immigration_status", ["Not Applicable"], "Immigration status", None)
        assert out["readback"] == "Not Applicable" and "came from your options" not in out.get("note", "")
        assert {r["source"] for r in records.decisions(ctx.job_id)} == {"standing:q.current_u_s_immigration_status"}
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_needs_you_park_that_lists_work_gets_the_gate_first(store, tmp_path):
    """CSX (2026-10-19) parked 'needs_you: Complete CSX's Experience and Education entries ... then submit' after the
    same wording under ready_for_you was bounced."""
    from tests.test_gate_pages import MID_FORM, _page
    ex = await _page(tmp_path, MID_FORM.replace('<input id="fn">', '<input id="fn" required aria-required="true">'))
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        out, _ = await d.t_park("needs_you: Complete CSX's Experience and Education entries, remaining required questions and signature, then submit.")
        assert "not parked" in out["error"] and out.get("blockers")
    finally:
        await ex.close(close_browser=True)


NUM_MONTHS = """<!doctype html><html><body style="margin:20px">
<label for="sm">Start Date (Month)</label>
<select id="sm"><option value="">Select</option>""" + "".join(f"<option>{i:02d}</option>" for i in range(1, 13)) + """</select>
</body></html>"""


@pytest.mark.asyncio
async def test_a_numeric_month_list_takes_the_month_number(tmp_path):
    """Epic Games (2026-10-19): 'Start Date (Month)' lists 01-12; 'August 2025' found no option."""
    ex = await _race(tmp_path, NUM_MONTHS, "nummonths.html")
    try:
        snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if i.get("tag") == "select")
        r = await fill.choose(ex, ref, ["August 2025"])
        assert r.ok, r.error
        assert await ex.page.evaluate("() => document.getElementById('sm').value") == "08"
    finally:
        await ex.close(close_browser=True)
