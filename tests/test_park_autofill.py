"""L3Harris (SuccessFactors, 2026-10-23): on a long form the run filled one or two required fields between parks and gave up
with ten blockers left that his stored answers covered. The park check now fills those itself."""
import pytest

from goat import answers, db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

FELONY = ("Have you ever been convicted of fraud or any felony arising out of a contract with any agency of the U.S. "
          "Government?")
PAGE = f"""<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <label for="q1">* {FELONY}</label>
  <select id="q1" required><option value="">Select One</option><option>No</option><option>Yes</option></select><br><br>
  <label for="sig">* Signature</label><input id="sig" type="text" required><br><br>
  <label for="other">* Favorite flavor of ice cream</label><input id="other" type="text" required><br><br>
  <button type="submit">Submit Application</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_a_park_fills_the_blockers_his_stored_answers_cover(store, tmp_path):
    answers.save_answer("compliance.criminal_history", "No", answer_type="choice", source="questionnaire", aliases=[FELONY])
    answers.save_answer("bank.signature", "Surya Thirukonda", answer_type="text", source="seed:answer_bank.json",
                        aliases=["Signature"])
    page = tmp_path / "long.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        out, _ = await d.t_park("needs_you: the long form still has required compliance and signature fields")
        assert "the harness filled 2 required field(s)" in out["error"], out
        vals = await ex.page.evaluate("() => [document.getElementById('q1').value, document.getElementById('sig').value, "
                                      "document.getElementById('other').value]")
        assert vals == ["No", "Surya Thirukonda", ""]
        kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=?", (ctx.job_id,))]
        assert "park:autofill" in kinds
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_incomplete_form_park_naming_a_stopper_word_is_still_checked(store, tmp_path):
    """L3Harris (2026-10-23): 'ready_for_you: SuccessFactors form remains incomplete; required security-clearance, ...
    questions need to be finished' stood at once because 'clearance' counts as a stopper word."""
    answers.save_answer("compliance.criminal_history", "No", answer_type="choice", source="questionnaire", aliases=[FELONY])
    page = tmp_path / "long2.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        out, _ = await d.t_park("ready_for_you: SuccessFactors form remains incomplete; required security-clearance, compliance, "
                                "demographic, and signature questions need to be finished before Apply.")
        assert out and "not parked" in out["error"], out
        assert await ex.page.evaluate("() => document.getElementById('q1').value") == "No"
    finally:
        await ex.close(close_browser=True)


LEFT = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <label for="d1">Key Responsibilities</label><input id="d1" type="text" maxlength="4000" value="&#8226; Launched a live manuscript-formatting beta; 40+ journal profiles &#8226; 200+ conversions"><br>
  <label for="ph">* Home Phone #</label><input id="ph" type="text" value="8321205"><br>
  <label for="nick">Nickname</label><input id="nick" type="text" value="Captain Hook"><br>
  <button type="submit">Submit Application</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_values_an_earlier_attempt_left_are_recorded_when_they_are_his_answers(store, tmp_path):
    """Westinghouse (SuccessFactors, 2026-10-23): after sign-in the form still held the last attempt's work descriptions and
    phone number, all his own stored values; the gate refused them as unsourced (a description holding ';' never matches its
    split stored variants, a phone box holds only the local part) and the run stopped."""
    answers.save_answer("experience.work.1.description", "• Launched a live manuscript-formatting beta; 40+ journal profiles \\n• 200+ conversions",
                        answer_type="text", source="questionnaire")
    answers.save_answer("identity.phone", "281-832-1205", answer_type="text", source="questionnaire")
    page = tmp_path / "left.html"
    page.write_text(LEFT, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        out, _ = await d.t_park("needs_you: the form still shows values from an earlier attempt")
        assert "experience.work.1.description" in out["error"] and "identity.phone" in out["error"], out
        assert "Nickname" not in out["error"]                     # not his answer: left for the model
        srcs = {r[0]: r[1] for r in db.connect().execute("SELECT field_label, source FROM decisions WHERE job_id=?", (ctx.job_id,))}
        assert srcs.get("Key Responsibilities") == "standing:experience.work.1.description"
        assert srcs.get("Home Phone #") == "standing:identity.phone"
    finally:
        await ex.close(close_browser=True)


def test_ways_of_saying_the_form_is_unfinished():
    from goat.agent.tools import ToolDispatcher
    pat = ToolDispatcher.RFY_INCOMPLETE
    for reason in ("needs_you: prior SuccessFactors form has required compliance, clearance, demographic and signature fields "
                   "remaining; continue filling required fields before submission.",
                   "ready_for_you: SuccessFactors form remains incomplete; required security-clearance questions need to be finished",
                   "needs_you: several required questions remain unanswered",
                   "needs_you: remaining required fields: clearance and signature"):
        assert pat.search(reason), reason
    for reason in ("ready_for_you: hCaptcha then Submit", "eligibility: requires an active security clearance",
                   "needs_you: requires three professional references"):
        assert not pat.search(reason), reason


SF_LEFT = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <div class="RCMFormField"><label for="381:_input">Graduation Year, if applicable</label>
    <div class="paginatedPicklistContainer fd-input-group fd-input-group--control">
      <div class="sr-only"><div role="status" aria-live="polite">One or more results available. Press Up or Down Arrow Keys to navigate the selection list.</div></div>
      <input id="381:_input" type="text" role="combobox" aria-label="Graduation Year, if applicable" placeholder="No Selection" value="">
    </div></div>
  <div role="radiogroup" aria-labelledby="g1"><span id="g1">Gender</span>
    <span role="radio" aria-checked="true" tabindex="0">Male</span><span role="radio" aria-checked="false" tabindex="-1">Female</span></div>
  <button type="submit">Submit Application</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_a_picklists_status_line_is_no_value_and_a_pressed_gender_is_recorded(store, tmp_path):
    """Westinghouse (SuccessFactors, 2026-10-23): the empty Graduation Year read as its screen-reader status line, and a
    pressed 'Male' under Gender was left unsourced; the gate blocked on both and the run held."""
    answers.save_answer("self_id.gender", "Male; Man", answer_type="choice", source="seed:delegation.yaml", aliases=["Gender"])
    page = tmp_path / "sfleft.html"
    page.write_text(SF_LEFT, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        year = next(i for i in snap.items if "Graduation Year" in (i.get("label") or ""))
        assert not (year.get("shown") or "").startswith("One or more results"), year
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap
        out, _ = await d.t_park("needs_you: two fields are present without sourced decisions")
        srcs = {r[0]: r[1] for r in db.connect().execute("SELECT field_label, source FROM decisions WHERE job_id=?", (ctx.job_id,))}
        assert any(lab.startswith("Gender / Male") and s == "standing:self_id.gender" for lab, s in srcs.items()), (out, srcs)
    finally:
        await ex.close(close_browser=True)


YEARS = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <div role="radiogroup" aria-labelledby="y1"><span id="y1">How many years of related work experience do you have?</span>
    <span role="radio" id="o0" aria-checked="true" tabindex="0" onclick="for (const s of this.parentElement.querySelectorAll('[role=radio]')) s.setAttribute('aria-checked', s === this ? 'true' : 'false')">None</span>
    <span role="radio" id="o1" aria-checked="false" tabindex="-1" onclick="for (const s of this.parentElement.querySelectorAll('[role=radio]')) s.setAttribute('aria-checked', s === this ? 'true' : 'false')">0-1 year</span>
    <span role="radio" id="o2" aria-checked="false" tabindex="-1" onclick="for (const s of this.parentElement.querySelectorAll('[role=radio]')) s.setAttribute('aria-checked', s === this ? 'true' : 'false')">1-3 years</span></div>
  <button type="submit">Submit Application</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_a_pressed_option_that_is_not_his_answer_gives_way_to_the_one_that_is(store, tmp_path):
    """Westinghouse (2026-10-23): 'None' was left pressed under years of experience; his answer is '0-1 year'."""
    answers.save_answer("experience.years_professional", "0-1 year; 1", answer_type="choice", source="questionnaire",
                        aliases=["How many years of related work experience do you have?"])
    page = tmp_path / "years.html"
    page.write_text(YEARS, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        out, _ = await d.t_park("needs_you: one field is present without a sourced decision")
        assert "0-1 year" in out["error"], out
        states = await ex.page.evaluate("() => ['o0','o1','o2'].map(i => document.getElementById(i).getAttribute('aria-checked'))")
        assert states == ["false", "true", "false"], states
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_park_presses_the_empty_yes_no_questions_his_answers_cover(store, tmp_path):
    """Walrus (Ashby, 2026-10-23): a required Yes / No pair with neither pressed is filled from his stored answer, the
    No of the sponsorship pair included (the empty-field scan names the pair by its first button, Yes)."""
    from tests.test_ashby_yesno import WALRUS
    answers.save_answer("work_authorization.authorized_to_work_in_us", "Yes", answer_type="choice", source="questionnaire",
                        aliases=["Are you authorized to work in the United States?"])
    answers.save_answer("work_authorization.requires_sponsorship_future", "No", answer_type="choice", source="questionnaire",
                        aliases=["Will you now or in the future require employment visa sponsorship?"])
    page = tmp_path / "walrus.html"
    page.write_text(WALRUS, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        out, _ = await d.t_park("needs_you: two required questions are unanswered")
        pressed = await ex.page.evaluate("() => [...document.querySelectorAll('button[aria-pressed]')].map(b => b.getAttribute('aria-pressed'))")
        assert pressed == ["true", "false", "false", "true"], (pressed, out)
    finally:
        await ex.close(close_browser=True)
