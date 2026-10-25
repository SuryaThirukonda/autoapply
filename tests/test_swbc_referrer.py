"""SWBC (Workday, 2026-10-23): the run typed his own email into the referrer box 'What's their name or email address?' from
answer_key identity.email, and later parked 'answer_timeout' with no question asked, right after drafting its availability
answer instead of filling it."""
import pytest

from goat import db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <label for="hd">How Did You Hear About Us?*</label><input id="hd" type="text" value="LinkedIn"><br>
  <label for="ref">What's their name or email address?*</label><input id="ref" type="text" required><br>
  <label for="em">Email Address*</label><input id="em" type="text" required><br>
  <button type="submit">Save and Continue</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_his_email_never_goes_into_a_question_about_someone_else(store, tmp_path):
    from goat import answers
    answers.save_answer("identity.email", "surya@example.com", answer_type="text", source="seed:standing_answers.json")
    page = tmp_path / "swbc.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        refs = {i["label"]: i["ref"] for i in snap.items if i.get("tag") == "input"}
        out, _ = await d.t_fill_batch([
            {"ref": refs["What's their name or email address?*"], "answer_key": "identity.email", "value": None, "source": None,
             "label": "Referrer"},
            {"ref": refs["Email Address*"], "answer_key": "identity.email", "value": None, "source": None, "label": "Email Address"}])
        assert [r["status"] for r in out["results"]] == ["failed", "set"], out
        assert "someone else" in out["results"][0]["error"]
        assert await ex.page.evaluate("() => document.getElementById('ref').value") == ""
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_answer_timeout_park_with_nothing_asked_is_refused(store):
    d, ctx = _dispatcher(_job())

    async def page_text(n=0):
        return "Application Questions List days and times you are available to work, please specify AM or PM."
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("answer_timeout: required work-availability days and times question has no stored answer")
    assert out and "no question is waiting on Surya" in out["error"], out
    assert db.connect().execute("SELECT 1 FROM events WHERE kind='park:bounced' AND detail LIKE '%nothing asked%'").fetchone()


@pytest.mark.asyncio
async def test_a_question_left_from_an_earlier_attempt_does_not_back_an_answer_timeout_park(store):
    """Penn State (2026-10-25): its SERS question was still unanswered from an earlier attempt, so a made-up answer_timeout
    park after a Workday error page stood although nothing was asked in this attempt."""
    job = _job()
    with db.tx() as con:
        con.execute("UPDATE jobs SET claimed_at=? WHERE id=?", (2_000, job["id"]))
        con.execute("INSERT INTO pending_questions(job_id,key,question,asked_at,deadline_at) VALUES(?,?,?,?,?)",
                    (job["id"], "q.sers", "Have you ever participated in SERS or PSERS?", 1_000, 1_900))
    d, ctx = _dispatcher(job)

    async def page_text(n=0):
        return "Application Questions"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("answer_timeout: prior attempts were held for unanswered SERS/PSERS questions")
    assert out and "no question is waiting on Surya" in out["error"], out
