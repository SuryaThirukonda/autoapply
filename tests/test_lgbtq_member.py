"""SeatGeek and Red Ventures (Greenhouse, 2026-10-24): 'I consider myself a member of the LGBTQ+ community' (Yes / No / Prefer
not to say) could not be set: his sexual-orientation answer is 'Heterosexual ...', the self-ID guard dropped 'No', and a
hand-picked 'No' was refused. Both drafts stuck at submit."""
import pytest

from goat import answers, db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px"><form>
<label for="lg">I consider myself a member of the LGBTQ+ community</label>
<select id="lg"><option value="">Select...</option><option>Yes</option><option>No</option><option>Prefer not to say</option></select>
</form></body></html>"""


@pytest.mark.asyncio
async def test_an_lgbtq_membership_question_takes_no_from_his_orientation(store, tmp_path):
    answers.save_answer("self_id.sexual_orientation", "Heterosexual; Straight; I do not identify as a member of the LGBTQ+ community",
                        answer_type="choice_chain", source="questionnaire")
    page = tmp_path / "sg.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if i.get("tag") == "select")
        out, _ = await d.t_choose(ref, "self_id.sexual_orientation", ["No"], "LGBTQ+ community", "answer_key")
        assert out.get("status") == "set" and out.get("readback") == "No", out
        src = db.connect().execute("SELECT source FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert src.startswith("standing:self_id.lgbtq_member"), src
    finally:
        await ex.close(close_browser=True)
