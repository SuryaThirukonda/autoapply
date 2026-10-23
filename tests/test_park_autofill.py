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
