"""L3Harris (SuccessFactors, 2026-10-23): "Are you aware of anything that may prevent you from obtaining a government
security clearance?" matched compliance.clearance_willing's ['obtain', 'clearance'] rule and took its Yes, which says the
opposite of his answer. A question about what would prevent or delay a clearance takes No."""
import pytest

from goat import answers, db
from goat.agent.tools import CLEARANCE_OBSTACLE_KEY, clearance_obstacle_question
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

L3 = "Are you aware of anything that may prevent you from obtaining a government security clearance?"


def test_questions_about_what_would_prevent_a_clearance():
    assert clearance_obstacle_question(L3)
    assert clearance_obstacle_question("Do any of the listed security clearance delays apply to you?")
    assert clearance_obstacle_question("Have you ever had a security clearance denied or revoked?")
    assert not clearance_obstacle_question("Are you willing to obtain a security clearance?")
    assert not clearance_obstacle_question("Are you able to obtain a security clearance if required?")
    assert not clearance_obstacle_question("Do you currently hold an active security clearance?")


@pytest.mark.asyncio
async def test_lookup_answers_no(store):
    answers.save_answer("compliance.clearance_willing", "Yes", answer_type="choice", source="questionnaire")
    d, ctx = _dispatcher(_job())
    out, _ = await d.t_lookup_answer(L3, ["No", "Yes"])
    assert out["key"] == CLEARANCE_OBSTACLE_KEY and out["answer"] == "No", out
    assert answers.get(CLEARANCE_OBSTACLE_KEY).source == "derived"


PAGE = f"""<!doctype html><html><body style="margin:20px">
<label for="q">* {L3}</label>
<select id="q"><option value="">Select One</option><option>No</option><option>Yes</option></select>
</body></html>"""


@pytest.mark.asyncio
async def test_choose_with_the_willing_key_on_such_a_question_picks_no(store, tmp_path):
    answers.save_answer("compliance.clearance_willing", "Yes", answer_type="choice", source="questionnaire")
    page = tmp_path / "clearance.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        ref = next(i["ref"] for i in ctx.last_snapshot.items if i.get("tag") == "select")
        out, _ = await d.t_choose(ref, "compliance.clearance_willing", ["No", "Yes"], "Aware of anything preventing clearance", "answer_key")
        assert out.get("readback") == "No", out
        kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=?", (ctx.job_id,))]
        assert "answer:clearance_obstacle" in kinds
        srcs = [r[0] for r in db.connect().execute("SELECT source FROM decisions WHERE job_id=?", (ctx.job_id,))]
        assert any(CLEARANCE_OBSTACLE_KEY in (s or "") for s in srcs), srcs
    finally:
        await ex.close(close_browser=True)
