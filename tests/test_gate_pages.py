"""The gate passes only where an application can be submitted (Northrop Grumman, 2026-10-18: Workday's maintenance
page, "Workday is currently unavailable.", had no controls at all; the gate passed and a portal_error park was
bounced into a submit)."""
import pytest

from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.browser.snapshot import Snapshot
from goat.budget import Parked
from goat.gate import NOT_APPLICATION, has_submit_control, portal_error, run_gate
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

MAINTENANCE = """<!doctype html><html><head><title>Workday is currently unavailable.</title></head><body>
<h1>Workday is currently unavailable.</h1><p>We apologize for the inconvenience. Please try again later.</p></body></html>"""

MID_FORM = """<!doctype html><html><body><h2>My Information</h2>
<div class="field"><label for="fn">First Name *</label><input id="fn"></div>
<button type="button">Save and Continue</button></body></html>"""

REVIEW = """<!doctype html><html><body><h2>Review</h2><p>Everything looks right.</p>
<button type="button">Submit Your Application</button></body></html>"""


def _snap(items):
    s = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        s.refs[i["ref"]] = i
    return s


def test_what_counts_as_a_submit_control():
    assert has_submit_control(_snap([{"ref": "e1", "tag": "button", "label": "Submit", "submitish": True}]))
    # a final button the snapshot's exact list does not know, but whose label reads like a submit
    assert has_submit_control(_snap([{"ref": "e1", "tag": "button", "label": "Submit Your Application", "submitish": False}]))
    assert has_submit_control(_snap([{"ref": "e1", "tag": "div", "label": "Send application", "submitish": False}]))
    assert not has_submit_control(_snap([{"ref": "e1", "tag": "button", "label": "Save and Continue", "submitish": False}]))
    assert not has_submit_control(_snap([{"ref": "e1", "tag": "input", "type": "text", "label": "Submit date", "value": ""}]))
    assert not has_submit_control(_snap([]))


def test_portal_error_knows_the_workday_maintenance_page_and_not_a_job_description():
    assert portal_error("Workday is currently unavailable. We apologize for the inconvenience.")
    assert portal_error("The site is temporarily unavailable")
    assert portal_error("You will minimise service interruption and run planned maintenance on the fleet.") is None


async def _page(tmp_path, html):
    page = tmp_path / "p.html"
    page.write_text(html, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


def _gate(job, snap, text):
    return run_gate(job=job, snapshot=snap, page_text=text, expected_resume=None, free_text_answers=[], portal="workday", page_no=1)


@pytest.mark.asyncio
async def test_the_gate_never_passes_a_maintenance_page_or_a_mid_form_page(store, tmp_path):
    job = _job()
    ex = await _page(tmp_path, MAINTENANCE)
    try:
        g = _gate(job, await snapshot.take(ex), await ex.page_text(5000))
        assert g.result == "blocked" and g.reasons[0].startswith(NOT_APPLICATION) and "Workday is currently unavailable" in g.reasons[0]
        await ex.page.set_content(MID_FORM)
        g = _gate(job, await snapshot.take(ex), await ex.page_text(5000))
        assert g.result == "blocked" and any(r.startswith(NOT_APPLICATION) and "no submit control" in r for r in g.reasons)
        await ex.page.set_content(REVIEW)
        assert _gate(job, await snapshot.take(ex), await ex.page_text(5000)).result == "pass_shadow"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_portal_error_park_on_a_maintenance_page_stands(store, tmp_path):
    job = _job()
    ex = await _page(tmp_path, MAINTENANCE)
    try:
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        with pytest.raises(Parked) as pi:
            await d.t_park("portal_error: Workday is currently unavailable (maintenance page)")
        assert pi.value.reason.startswith("portal_error")
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_eager_park_on_a_clean_final_page_is_still_sent_to_submit(store, tmp_path):
    job = _job()
    ex = await _page(tmp_path, REVIEW)
    try:
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        out, _ = await d.t_park("unclear: not sure the form is finished")
        assert "the code gate PASSES" in out["error"]
    finally:
        await ex.close(close_browser=True)


def test_a_workday_review_page_saying_he_is_not_fluent_in_english_is_blocked(store):
    """American Family (2026-10-18): the proficiency dropdowns were set, the fluency box was not."""
    job = _job()
    snap = _snap([{"ref": "e1", "tag": "button", "label": "Submit", "submitish": True}])
    snap.headings = ["Review"]
    text = "Languages\nLanguage\nEnglish\nI am fluent in this language\nNo\nOverall\n5 - Fluent\nSubmit"
    g = run_gate(job=job, snapshot=snap, page_text=text, expected_resume=None, free_text_answers=[], portal="workday", page_no=1)
    assert g.result == "blocked" and "experience.fluent_in_english" in g.reasons[0]
    ok = run_gate(job=job, snapshot=snap, page_text=text.replace("language\nNo", "language\nYes"), expected_resume=None,
                  free_text_answers=[], portal="workday", page_no=1)
    assert not any("fluent" in r for r in ok.reasons)


def test_a_disabled_control_with_a_default_value_needs_no_decision(store):
    """ibotta (2026-10-18): 'Still Student' disables Ashby's end-date year, which keeps a default 2026."""
    job = _job()
    items = [{"ref": "e38", "tag": "select", "label": "End Date", "value": "2026", "disabled": True},
             {"ref": "e1", "tag": "button", "label": "Submit Application", "submitish": True}]
    g = run_gate(job=job, snapshot=_snap(items), page_text="Education\nStill Student\nSubmit Application", expected_resume=None,
                 free_text_answers=[], portal="ashby", page_no=1)
    assert g.result == "pass_shadow", g.reasons
    items[0]["disabled"] = False
    g = run_gate(job=job, snapshot=_snap(items), page_text="Education\nSubmit Application", expected_resume=None,
                 free_text_answers=[], portal="ashby", page_no=1)
    assert g.result == "blocked" and "End Date" in g.reasons[0]


GONE = """<!doctype html><html><body><h1>Careers</h1><p>The page you are looking for doesn't exist.</p>
<a href="#">Search for Jobs</a></body></html>"""


@pytest.mark.asyncio
async def test_a_park_on_a_removed_posting_closes_the_job(store, tmp_path):
    """AtkinsRealis (2026-10-18): Workday's 'The page you are looking for doesn't exist' held as a portal error."""
    job = _job()
    ex = await _page(tmp_path, GONE)
    try:
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        with pytest.raises(Parked) as pi:
            await d.t_park("portal_error: the listing page does not exist")
        assert pi.value.state == "dead" and pi.value.reason.startswith("posting_closed:")
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_vague_ready_for_you_park_gets_the_gate_blockers(store, tmp_path):
    """Moderna (2026-10-19) parked 'ready_for_you: resume application fields' on My Information, where nothing needed
    Surya; Epic Games parked 'complete the demographic questions'. A CAPTCHA reason still parks."""
    job = _job()
    ex = await _page(tmp_path, MID_FORM.replace('<input id="fn">', '<input id="fn" required aria-required="true">'))
    try:
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        out, _ = await d.t_park("ready_for_you: resume application fields")
        assert "not parked" in out["error"] and out.get("blockers")
        ctx.last_snapshot.challenge = {"challenge": True, "kinds": ["hcaptcha"]}    # a real one on the page
        with pytest.raises(Parked) as p:
            await d.t_park("ready_for_you: hCaptcha then Submit")
        assert p.value.state == "ready_for_you"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_captcha_the_page_does_not_show_does_not_end_the_run(store, tmp_path):
    """Epic Games (2026-10-19): 'complete the required CAPTCHA/human verification if presented; required fields remain:
    phone, ...' on a page with no challenge."""
    job = _job()
    ex = await _page(tmp_path, MID_FORM.replace('<input id="fn">', '<input id="fn" required aria-required="true">'))
    try:
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        out, _ = await d.t_park("ready_for_you: complete the required CAPTCHA/human verification if presented; required fields remain: phone")
        assert "not parked" in out["error"] and out.get("blockers")
    finally:
        await ex.close(close_browser=True)



def test_a_placeholder_value_is_not_an_unsourced_value(tmp_path):
    """IXL's office picker shows '--' and a Greenhouse select showed its own question as the selection (2026-10-18):
    neither is a value the run has to source."""
    from goat import db, gate
    from goat.browser.snapshot import Snapshot
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Acme','Intern','https://x.example/job','greenhouse')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','no_account',0)")
    job = dict(db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=1").fetchone())
    snap = Snapshot(url="https://x.example/job", title="t", headings=["Apply"], items=[
        {"ref": "e18", "tag": "select", "label": "Which IXL office would you prefer?", "value": "--"},
        {"ref": "e25", "tag": "dropdown", "label": "What University do you currently attend?", "shown": "What University do you currently attend?"},
        {"ref": "e30", "tag": "input", "type": "text", "label": "Nickname", "value": "Sur"}], groups=[], errors={}, challenge={})
    for i in snap.items:
        snap.refs[i["ref"]] = i
    reasons = [r for r in gate.run_gate(job=job, snapshot=snap, page_text="", expected_resume=None, free_text_answers=[], portal="greenhouse", page_no=1).reasons if "no sourced decision" in r]
    assert any("Nickname" in r for r in reasons)
    assert not any("IXL office" in r or "University" in r for r in reasons), reasons



def test_only_the_newest_decision_on_a_control_is_judged(tmp_path):
    """Axiom Space (2026-10-19): an attestation first set without a key (source adapter), then re-set from the
    consent rule under another label on the same box, must not keep the gate blocked."""
    from goat import db, gate, records
    from goat.browser.snapshot import Snapshot
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Axiom Space','Intern','https://x.example/job','workday')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','account',0)")
    job = dict(db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=1").fetchone())
    records.record_decision(1, 1, "Acknowledge and agree to complete NASA export-control screening if selected", "Yes", "adapter", "e31")
    snap = Snapshot(url="https://x.example/job", title="t", headings=["Review"], items=[
        {"ref": "e40", "tag": "button", "label": "Submit", "submitish": True}], groups=[], errors={}, challenge={})
    for i in snap.items:
        snap.refs[i["ref"]] = i
    def reasons():
        return gate.run_gate(job=job, snapshot=snap, page_text="", expected_resume=None, free_text_answers=[], portal="workday", page_no=1).reasons
    assert any("attestation not from a standing answer" in r for r in reasons())
    records.record_decision(1, 1, "Acknowledge NASA export-control screening", "Yes", "standing:consent.privacy_notice", "e31")
    assert not any("attestation not from a standing answer" in r for r in reasons())



def test_an_oracle_resume_listed_beside_remove_resume_counts_as_attached(store):
    """onsemi and Vertiv (2026-10-19): Oracle empties 'Import your profile from resume' after the upload and lists the
    file under Supporting Documents beside 'Remove Resume'."""
    job = _job()
    items = [{"ref": "e3", "tag": "input", "type": "file", "label": "Import your profile from resume", "value": ""},
             {"ref": "e1", "tag": "button", "label": "Submit", "submitish": True}]
    text = "Supporting Documents and URLs\nSurya_Thirukonda_Resume_SWE.pdf\nRemove Resume\nUpload Cover Letter\nSubmit"
    g = run_gate(job=job, snapshot=_snap(items), page_text=text, expected_resume="Surya_Thirukonda_Resume_SWE.pdf",
                 free_text_answers=[], portal="oracle", page_no=1)
    assert not any("resume" in r.lower() for r in g.reasons), g.reasons
    g = run_gate(job=job, snapshot=_snap(items), page_text="Supporting Documents and URLs\nUpload Cover Letter\nSubmit",
                 expected_resume="Surya_Thirukonda_Resume_SWE.pdf", free_text_answers=[], portal="oracle", page_no=1)
    assert any("resume not attached" in r for r in g.reasons), g.reasons



def test_a_picker_placeholder_is_not_a_value_to_source(store):
    """Electronic Arts (2026-10-21): 'Secondary Country Code = Select an option' blocked the gate."""
    from goat.gate import PLACEHOLDER_VALUE
    for v in ("Select an option", "Select One", "Please select", "No Selection", "Choose...", "--", "Select country"):
        assert PLACEHOLDER_VALUE.match(v), v
    for v in ("Texas", "United States (+1)", "Selected Works", "Choose Life Inc"):
        assert not PLACEHOLDER_VALUE.match(v), v



def test_a_required_message_beside_a_filled_field_is_stale(store):
    """Westinghouse SuccessFactors (2026-10-21): 'Please complete your personal information.' stayed beside Salutation = Mr."""
    from goat.browser.snapshot import Snapshot
    from goat.gate import drop_stale_required
    errs = {"error_text": [{"near": "* Salutation", "text": "Please complete your personal information."},
                           {"near": "Email", "text": "Please enter a valid email address."}],
            "alerts": [{"near": "* Veteran Status", "how": "role=alert", "text": "Veteran Status is required"}]}
    items = [{"ref": "e20", "tag": "input", "label": "* Salutation", "value": "Mr", "combo": True},
             {"ref": "e21", "tag": "input", "label": "Email", "value": "x@"},
             {"ref": "e118", "tag": "input", "label": "* Veteran Status", "value": ""}]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors=errs, challenge={})
    out = drop_stale_required(errs, snap)
    assert out["error_text"] == [errs["error_text"][1]]            # a format error stays even with a value
    assert out["alerts"] == errs["alerts"]                          # the empty field keeps its required message



def test_a_prefilled_value_equal_to_his_stored_answer_is_sourced(store):
    """Electronic Arts (2026-10-21): the portal kept 'Are you willing to relocate? = Yes' from his earlier EA application,
    and the gate asked for a source the run never gave."""
    from goat import answers
    from goat.gate import run_gate
    answers.save_answer("availability.willing_to_relocate", "Yes", answer_type="choice", source="questionnaire",
                        aliases=["Are you willing to relocate?"])
    job = _job()
    items = [{"ref": "e26", "tag": "select", "label": "Are you willing to relocate?", "value": "Yes"},
             {"ref": "e1", "tag": "button", "label": "Submit", "submitish": True}]
    g = run_gate(job=job, snapshot=_snap(items), page_text="Are you willing to relocate?\nYes\nSubmit", expected_resume=None,
                 free_text_answers=[], portal="other", page_no=1)
    assert not any("e26" in r for r in g.reasons), g.reasons
    items[0]["value"] = "No"
    g = run_gate(job=job, snapshot=_snap(items), page_text="Are you willing to relocate?\nNo\nSubmit", expected_resume=None,
                 free_text_answers=[], portal="other", page_no=1)
    assert any("e26" in r for r in g.reasons), g.reasons



def test_a_password_help_page_is_never_the_end_of_an_application(store):
    """CenturyLink (2026-10-21): the run pressed 'Submit' on SuccessFactors' Password Help page and the gate passed."""
    from goat.browser.snapshot import Snapshot
    from goat.gate import run_gate
    job = _job()
    items = [{"ref": "e1", "tag": "input", "type": "email", "label": "Email Address", "value": "thirukondas@gmail.com"},
             {"ref": "e2", "tag": "button", "label": "Submit", "submitish": True}]
    snap = Snapshot(url="u", title="Career Opportunities: Password Help", headings=["Password Help"], items=items, groups=[],
                    errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    g = run_gate(job=job, snapshot=snap, page_text="Password Help\nEmail Address\nSubmit", expected_resume=None,
                 free_text_answers=[], portal="successfactors", page_no=1)
    assert g.result == "blocked" and "account page" in g.reasons[0]
    snap2 = Snapshot(url="u", title="Apply: Software Engineer Intern", headings=["Application Questions"], items=items, groups=[],
                     errors={}, challenge={})
    from goat.gate import ACCOUNT_PAGE
    assert not ACCOUNT_PAGE.search("Apply: Software Engineer Intern | Application Questions")



def test_a_section_level_complete_your_information_message_is_stale_when_nothing_is_empty(store):
    """L3Harris (2026-10-21): 'Please complete your personal information.' sat by the 'Login/Primary Email' heading."""
    from goat.browser.snapshot import Snapshot
    from goat.gate import drop_stale_required
    errs = {"error_text": [{"near": "Login/Primary Email", "text": "Please complete your personal information."}]}
    snap = Snapshot(url="u", title="t", headings=[], items=[{"ref": "e17", "tag": "input", "label": "* E-Mail", "value": "x@y.z"}],
                    groups=[], errors=errs, challenge={})
    assert drop_stale_required(errs, snap)["error_text"] == []
    errs2 = {**errs, "empty_required": [{"label": "* Phone", "type": "text", "ref": "e20"}]}
    assert drop_stale_required(errs2, snap)["error_text"] == errs["error_text"]



def test_the_sites_display_language_picker_is_not_a_question():
    """L3Harris (SuccessFactors, 2026-10-21): 'Language = English US (English US)' blocked the gate and every pick reloaded."""
    from goat.gate import is_display_language_picker
    assert is_display_language_picker("Language", "English US \u200e(English US)\u200e")
    assert is_display_language_picker("Language:", "Deutsch (Deutsch)")
    assert not is_display_language_picker("Language", "English")                       # a real answer
    assert not is_display_language_picker("Languages spoken", "English US (English US)")
    assert not is_display_language_picker("Preferred language for communication", "English (US)")



def test_a_cookie_settings_button_needs_no_sourced_decision():
    """Qualcomm (2026-10-21): OneTrust's 'Cookies Settings' button read as a dropdown valued 'Cookies Settings'."""
    from goat.gate import COOKIE_CONTROL
    assert COOKIE_CONTROL.search("Cookies Settings, Opens the preference center dialog")
    assert COOKIE_CONTROL.search("Manage cookies")
    assert not COOKIE_CONTROL.search("How did you hear about this position?")
    assert not COOKIE_CONTROL.search("Country")



ICON_ONLY = """<html><body><form>
<div class="field"><label for="g">* Gender</label><span class="ctl"><input id="g" type="text" role="combobox" aria-required="true" required value="">
<span class="icon">\ue1ef</span></span></div>
</form></body></html>"""


@pytest.mark.asyncio
async def test_a_dropdown_showing_only_its_icon_glyph_is_empty():
    """L3Harris (SuccessFactors, 2026-10-21): an empty dropdown 'showed' its arrow glyph U+E1EF and passed as answered."""
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(ICON_ONLY)
        snap = await snapshot.take(ex)
        item = next(i for i in snap.items if "Gender" in (i.get("label") or ""))
        assert not item.get("shown"), item
        assert any("Gender" in (r.get("label") or "") for r in snap.errors.get("empty_required", [])), snap.errors
    finally:
        await ex.close()



def test_a_search_pages_sort_picker_needs_no_sourced_decision():
    """UKG (2026-10-21): 'Sort by = Latest' on the careers search page was asked for a sourced decision."""
    from goat.gate import SEARCH_CONTROL
    assert SEARCH_CONTROL.match("Sort by") and SEARCH_CONTROL.match("Sort") and SEARCH_CONTROL.match("Results per page:")
    assert not SEARCH_CONTROL.match("Sort code") and not SEARCH_CONTROL.match("How did you hear about us?")



SF_RADIOS = """<html><body><div class="RCMFormField"><label id="q1" class="rcmFormQuestionLabel"><span class="requiredField">*</span>&nbsp;Will you now
or in the future require Visa sponsorship for full-time employment?</label>
<div id="g1" class="radioGroup" role="radiogroup" aria-labelledby="q1">
 <span id="r1" onclick="this.firstElementChild.setAttribute('aria-checked','true'); document.querySelector('#r2 span').setAttribute('aria-checked','false')">
  <span class="radioContainer" role="radio" aria-checked="false" aria-labelledby="l1" tabindex="0" style="display:inline-block;width:16px;height:16px"></span><label id="l1">Yes</label></span>
 <span id="r2" onclick="this.firstElementChild.setAttribute('aria-checked','true'); document.querySelector('#r1 span').setAttribute('aria-checked','false')">
  <span class="radioContainer" role="radio" aria-checked="false" aria-labelledby="l2" tabindex="-1" style="display:inline-block;width:16px;height:16px"></span><label id="l2">No</label></span>
</div></div></body></html>"""


@pytest.mark.asyncio
async def test_aria_radios_are_toggles_with_their_question_and_an_unanswered_required_group_is_empty():
    """CenturyLink (SuccessFactors, 2026-10-21): four required span radios showed no question, none was set, the gate passed."""
    from goat.browser import fill
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(SF_RADIOS)
        snap = await snapshot.take(ex)
        toggles = [i for i in snap.items if i.get("tag") == "toggle"]
        assert [t["label"] for t in toggles] == ["Yes", "No"], toggles
        assert "require Visa sponsorship" in toggles[0]["group"] and toggles[0].get("required")
        assert any("Visa sponsorship" in (r.get("label") or "") for r in snap.errors.get("empty_required", [])), snap.errors
        no = toggles[1]["ref"]
        out = await fill.fill_batch(ex, [{"ref": no, "value": "true"}])
        assert await ex.page.evaluate("() => document.querySelector('#r2 span').getAttribute('aria-checked')") == "true", out
        snap = await snapshot.take(ex)
        assert not snap.errors.get("empty_required"), snap.errors
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_submit_is_not_refused_on_a_stale_section_message():
    """CenturyLink (2026-10-21): gate, judge and check passed; submit refused on 'Please complete your personal information.'"""
    from goat.browser.submit import submit
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content("<html><body><div class='error' role='alert'>Please complete your personal information.</div>"
                                  "<input value='Surya'><input value='Thirukonda'><input value='x@y.z'>"
                                  "<button type='button' style='width:100px;height:30px'>Submit</button></body></html>")
        res = await submit(ex, None, wait_s=1.0)
        assert not any("Please complete your personal information" in w for w in res.why if res.status == "blocked"), res
    finally:
        await ex.close()


SF_FOLDED = """<html><body>
<a id="30:_expandAllSections" href="#">Expand all sections</a> <a id="30:_collapseAllSections" href="#">Collapse all sections</a>
<button id="87:topBar" aria-expanded="true" type="button">Profile Information</button>
<div><label for="city">* City</label><input id="city" value="Sugar Land"></div>
<button id="248:topBar" aria-expanded="false" type="button"
  onclick="this.setAttribute('aria-expanded', 'true'); document.getElementById('js').style.display = 'block';">Job-Specific Information</button>
<div id="js" style="display:none"><label for="elig">* Are you eligible to work in the country to which you are applying?</label>
  <input id="elig" type="text" required></div>
<button type="button">Apply</button>
</body></html>"""


@pytest.mark.asyncio
async def test_a_folded_successfactors_section_blocks_and_is_opened():
    """CenturyLink (SuccessFactors, 2026-10-21): the re-opened application had every section folded, the outline listed no
    field, and the gate passed it; the judge found the eligibility and enrollment questions unanswered."""
    from goat.browser import fill
    from goat.browser.errors import why_blocked
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(SF_FOLDED)
        snap = await snapshot.take(ex)
        assert snap.errors.get("collapsed_sections") == ["Job-Specific Information"], snap.errors
        assert any(w.startswith("collapsed_sections: Job-Specific Information") for w in why_blocked(snap.errors))
        assert not any("eligible" in (i.get("label") or "") for i in snap.items)
        assert await fill.unfold_sections(ex) == ["Job-Specific Information"]
        snap = await snapshot.take(ex)
        assert not snap.errors.get("collapsed_sections"), snap.errors
        assert any("eligible" in (r.get("label") or "") for r in snap.errors.get("empty_required", [])), snap.errors
    finally:
        await ex.close()
