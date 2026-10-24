"""Anduril (Greenhouse, 2026-10-23): 'What is your top location preference?' offered only 'Costa Mesa, CA (HQ)'; the pick
was recorded as the model's judgment against 'All offered locations; Dallas; ...' and the judge failed it as unsupported."""
import pytest

from goat import answers, db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px"><form>
<label for="loc">What is your top location preference? *</label>
<select id="loc"><option value="">Select...</option><option>Costa Mesa, CA (HQ)</option></select>
</form></body></html>"""


@pytest.mark.asyncio
async def test_the_only_offered_location_is_his_answer(store, tmp_path):
    answers.save_answer("availability.location_ranking", "All offered locations; Dallas; Houston; Austin; New York; Los Angeles",
                        answer_type="choice_chain", source="questionnaire")
    page = tmp_path / "anduril.html"
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
        out, _ = await d.t_choose(ref, "availability.location_ranking", ["Costa Mesa, CA (HQ)"], "Top location preference", "answer")
        assert out.get("status") == "set", out
        src = db.connect().execute("SELECT source FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert src.startswith("standing:availability.location_ranking") and "only option" in src, src
    finally:
        await ex.close(close_browser=True)


HEARD_PAGE = """<!doctype html><html><body style="margin:20px"><form>
<label for="src">How Did You Hear About Us? *</label>
<select id="src"><option value="">Select...</option><option>Career Fair</option><option>Employee Referral</option><option>Other</option></select>
</form></body></html>"""


@pytest.mark.asyncio
async def test_a_late_how_heard_fallback_records_what_the_list_offered(store, tmp_path):
    """General Motors (Workday, 2026-10-24): five drafts held because 'Other' is his last how-heard preference and nothing
    showed that LinkedIn, a job board or an agency was not offered."""
    answers.save_answer("how_did_you_hear.preferred_option", "LinkedIn; Job Board; Agency / Search Firm; Other",
                        answer_type="choice_chain", source="operator")
    page = tmp_path / "gm.html"
    page.write_text(HEARD_PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if i.get("tag") == "select")
        out, _ = await d.t_choose(ref, "how_did_you_hear.preferred_option", [], "How Did You Hear About Us?", "answer_key")
        assert out.get("readback") == "Other", out
        src = db.connect().execute("SELECT source FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert "offered:" in src and "Career Fair" in src and "Employee Referral" in src, src
    finally:
        await ex.close(close_browser=True)
