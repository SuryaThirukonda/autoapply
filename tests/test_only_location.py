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


@pytest.mark.asyncio
async def test_the_fallback_evidence_comes_from_what_choose_saw_when_the_list_cannot_be_peeked(store, tmp_path, monkeypatch):
    """General Motors (Workday, 2026-10-24): peek_options read nothing from the Workday prompt once 'Other' was picked, so
    the five drafts were held again with no evidence."""
    from goat.browser import fill
    answers.save_answer("how_did_you_hear.preferred_option", "LinkedIn; Job Board; Agency / Search Firm; Other",
                        answer_type="choice_chain", source="operator")
    page = tmp_path / "gm2.html"
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

        async def nothing(ex_, ref_):
            return [], None
        monkeypatch.setattr(fill, "peek_options", nothing)
        real_choose = fill.choose

        async def choose_and_note(ex_, ref_, wants, **kw):
            fill.note_seen(ref_, [{"text": t} for t in ("Career Fair", "Employee Referral", "Other")])
            return await real_choose(ex_, ref_, wants, **kw)
        monkeypatch.setattr(fill, "choose", choose_and_note)
        out, _ = await d.t_choose(ref, "how_did_you_hear.preferred_option", [], "How Did You Hear About Us?", "answer_key")
        assert out.get("readback") == "Other", out
        src = db.connect().execute("SELECT source FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert "offered:" in src and "Employee Referral" in src, src
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_better_ranked_option_the_list_held_is_picked_over_the_fallback(store, tmp_path, monkeypatch):
    """General Motors (Workday, 2026-10-24): the prompt settled on 'Other' while its list held 'Internet Job Board / Job
    Search', his second choice."""
    from goat.browser import fill
    answers.save_answer("how_did_you_hear.preferred_option", "LinkedIn; Job Board; Agency / Search Firm; Other",
                        answer_type="choice_chain", source="operator")
    html = HEARD_PAGE.replace("<option>Other</option>", "<option>Internet Job Board / Job Search</option><option>Other</option>")
    page = tmp_path / "gm3.html"
    page.write_text(html, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if i.get("tag") == "select")
        real_choose = fill.choose
        calls = []

        async def settles_on_other_first(ex_, ref_, wants, **kw):
            calls.append(list(wants))
            if len(calls) == 1:                  # the Workday prompt flow that ended on 'Other'
                fill.note_seen(ref_, [{"text": t} for t in ("Career Fair", "Internet Job Board / Job Search", "Other")])
                return await real_choose(ex_, ref_, ["Other"], **kw)
            return await real_choose(ex_, ref_, wants, **kw)
        monkeypatch.setattr(fill, "choose", settles_on_other_first)
        out, _ = await d.t_choose(ref, "how_did_you_hear.preferred_option", [], "How Did You Hear About Us?", "answer_key")
        assert out.get("readback") == "Internet Job Board / Job Search", out
        assert db.connect().execute("SELECT 1 FROM events WHERE kind='choose:repicked'").fetchone()
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_other_inside_the_job_board_category_is_his_job_board_answer(store, tmp_path, monkeypatch):
    """General Motors (Workday, 2026-10-24): the list's only 'Other' sits inside 'Internet Job Board / Job Search'. The chip
    names only the leaf, so the pick read as a fallback, the re-pick of its own category failed on every draft and the judge
    failed the draft for 'Other'."""
    from goat.browser import fill
    answers.save_answer("how_did_you_hear.preferred_option", "LinkedIn; Job Board; Agency / Search Firm; Other",
                        answer_type="choice_chain", source="operator")
    page = tmp_path / "gm4.html"
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
        real_choose = fill.choose
        calls = []

        async def walks_into_the_category(ex_, ref_, wants, **kw):
            calls.append(list(wants))
            fill.note_seen(ref_, [{"text": t} for t in ("Career Fair", "Internet Job Board /  Job Search", "Other")])
            fill.CATEGORY_PATH.setdefault(ref_, []).append("Internet Job Board / Job Search")
            return await real_choose(ex_, ref_, ["Other"], **kw)
        monkeypatch.setattr(fill, "choose", walks_into_the_category)
        out, _ = await d.t_choose(ref, "how_did_you_hear.preferred_option", [], "How Did You Hear About Us?", "answer_key")
        assert out.get("readback") == "Other", out
        assert len(calls) == 1, calls                    # no re-pick of the category it already sits in
        con = db.connect()
        assert not con.execute("SELECT 1 FROM events WHERE kind IN ('choose:fallback','choose:repick_failed')").fetchone()
        src = con.execute("SELECT source FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert "inside the category 'Internet Job Board / Job Search'" in src, src
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_saved_other_chip_is_placed_by_the_repicks_walk_into_the_category(store, tmp_path, monkeypatch):
    """General Motors 551 (Workday, 2026-10-24): the rerun found its 'Other' chip already saved, so choose never walked into
    'Internet Job Board / Job Search'. The re-pick's walk showed that same 'Other' inside the category, yet the pick was logged
    as a fallback and the model went back from Review to fix it, twice."""
    from goat.browser import fill
    from goat.browser.fill import FieldResult
    answers.save_answer("how_did_you_hear.preferred_option", "LinkedIn; Job Board; Agency / Search Firm; Other",
                        answer_type="choice_chain", source="operator")
    page = tmp_path / "gm5.html"
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
        real_choose = fill.choose
        calls = []

        async def nothing(ex_, ref_):
            return [], None
        monkeypatch.setattr(fill, "peek_options", nothing)     # a picked Workday prompt cannot be peeked

        async def saved_chip_then_walk(ex_, ref_, wants, **kw):
            calls.append(list(wants))
            if len(calls) == 1:                  # the chip was already there: no walk
                fill.note_seen(ref_, [{"text": t} for t in ("Career Fair", "Internet Job Board /  Job Search")])
                return await real_choose(ex_, ref_, ["Other"], **kw)
            fill.note_category(ref_, "Internet Job Board /  Job Search", [{"text": "Other"}, {"text": "Other"}])
            return FieldResult(ref_, False, "Other", "inside 'Internet Job Board / Job Search' the list shows: ['Other', 'Other']")
        monkeypatch.setattr(fill, "choose", saved_chip_then_walk)
        out, _ = await d.t_choose(ref, "how_did_you_hear.preferred_option", [], "How Did You Hear About Us?", "answer_key")
        assert out.get("readback") == "Other" and "sits inside the category" in out.get("note", ""), out
        con = db.connect()
        assert not con.execute("SELECT 1 FROM events WHERE kind IN ('choose:fallback','choose:repick_failed')").fetchone()
        src = con.execute("SELECT source FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert "inside the category 'Internet Job Board / Job Search'" in src, src
    finally:
        await ex.close(close_browser=True)
