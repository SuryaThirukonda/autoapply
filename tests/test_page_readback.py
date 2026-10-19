"""The per-page read-back runs BEFORE the click that leaves a page, compares only that page's decisions, and blocks
the click at most once; radio decisions name the option that was ticked (ABA on Workday, 2026-10-18)."""
import pytest

from goat import answers, db, readback, records
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures
from tests.test_readback import _dec, _snap

PAGE = """<!doctype html><html><body style="margin:20px">
<div class="field"><label for="fn">First Name*</label><input id="fn" name="first"></div>
<fieldset><legend>Were you previously employed at ABA?</legend>
  <label><input type="radio" name="prior" id="py" value="yes"> Yes</label>
  <label><input type="radio" name="prior" id="pn" value="no"> No</label></fieldset>
<button type="button" id="next" onclick="window.__nexts = (window.__nexts || 0) + 1">Next</button>
</body></html>"""


def test_a_ref_reused_on_another_page_never_pairs():
    """Workday gave e27 to First Name on page 1 and to School on page 3: page 1's decision must not judge page 3's box."""
    snap = _snap([{"ref": "e27", "tag": "input", "type": "text", "label": "School or University*", "value": ""}])
    assert readback.diff([_dec("First Name*", "Surya", "e27", page=1)], snap, 3) == []
    assert readback.diff([_dec("School or University*", "Texas A&M", "e27", page=3)], snap, 3) != []


async def _open(tmp_path):
    page = tmp_path / "aba.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


def _ref(snap, label_start):
    return next(i["ref"] for i in snap.items if (i.get("label") or "").startswith(label_start))


@pytest.mark.asyncio
async def test_next_is_stopped_once_when_a_field_drifted_and_goes_through_the_second_time(store, tmp_path):
    job = _job()
    ex = await _open(tmp_path)
    try:
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        fn = _ref(snap, "First Name")
        await ex.page.fill("#fn", "Surya")
        records.record_decision(job["id"], ctx.page_no, "First Name*", "Surya", "standing:identity.first_name", fn)
        await ex.page.fill("#fn", "Changed")               # something cleared / rewrote the box
        nxt = _ref(snap, "Next")
        out, _ = await d.t_click(nxt, "Next")
        assert "not clicked" in out["error"] and "First Name" in out["error"] and ctx.page_no == 1
        assert await ex.page.evaluate("() => window.__nexts || 0") == 0
        out, _ = await d.t_click(nxt, "Next")              # never blocks twice on the same page
        assert "error" not in out and ctx.page_no == 2
        assert await ex.page.evaluate("() => window.__nexts || 0") == 1
        # the second click re-checks: what still differs is kept, and the verified auto-submit refuses while it remains
        evs = db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='gate:readback'").fetchone()[0]
        assert evs == 2
        assert list(ctx.readback_open) == [1] and "First Name" in ctx.readback_open[1][0]
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_clean_page_is_left_at_once(store, tmp_path):
    job = _job()
    ex = await _open(tmp_path)
    try:
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        await ex.page.fill("#fn", "Surya")
        records.record_decision(job["id"], ctx.page_no, "First Name*", "Surya", "standing:identity.first_name", _ref(snap, "First Name"))
        ctx.readback_open = {1: ["stale"]}                 # a page fixed before it was left clears its entry
        out, _ = await d.t_click(_ref(snap, "Next"), "Next")
        assert "error" not in out and ctx.page_no == 2 and ctx.readback_open == {}
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_radio_decision_names_the_option_that_was_ticked(store, tmp_path):
    job = _job()
    answers.save_answer("bank.prior-employee", "No", answer_type="choice", source="questionnaire")
    ex = await _open(tmp_path)
    try:
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        no_ref = next(o["ref"] for g in snap.groups for o in g["options"] if o["label"].strip() == "No")
        out, _ = await d.t_fill_batch([{"ref": no_ref, "answer_key": "bank.prior-employee", "value": None, "source": None,
                                        "label": "Were you previously employed at ABA?"}])
        assert out["results"][0]["status"] == "set", out
        dec = records.decisions(job["id"])[-1]
        assert dec["field_label"] == "Were you previously employed at ABA? / No" and dec["value"] == "checked"
    finally:
        await ex.close(close_browser=True)


def test_a_newer_pick_in_a_radio_group_supersedes_the_older_sibling():
    """American Family (2026-10-18): Yes was ticked, then corrected to No; the stale Yes decision is not a mismatch."""
    from goat.browser.snapshot import Snapshot
    groups = [{"kind": "radio", "label": "Previously employed?", "options": [{"ref": "e4", "label": "Yes", "checked": False},
                                                                              {"ref": "e5", "label": "No", "checked": True}]}]
    s = Snapshot(url="u", title="t", headings=[], items=[], groups=groups, errors={}, challenge={})
    for g in groups:
        for o in g["options"]:
            s.refs[o["ref"]] = {**o, "tag": "input", "type": "radio", "group": g["label"]}
    decs = [_dec("Previously employed? / Yes", "checked", "e4"), _dec("Previously employed? / No", "checked", "e5")]
    assert readback.diff(decs, s, 1) == []
    # the newest pick still has to hold: if the page shows Yes after No was decided, that is a mismatch
    s.refs["e5"]["checked"] = False
    s.refs["e4"]["checked"] = True
    assert readback.diff(decs, s, 1) == ["field No [e5] shows '' but decision was 'checked'"]


@pytest.mark.asyncio
async def test_a_yes_no_answer_ticks_only_the_matching_radio_option(store, tmp_path):
    """A stored No sent to the Yes option is refused (it used to tick Yes); sent to the No option it ticks No;
    a stored Yes is refused on the No option."""
    job = _job()
    answers.save_answer("background.previously_employed_at_this_company", "No", answer_type="text", source="questionnaire")
    answers.save_answer("work_authorization.authorized_to_work_in_us", "Yes", answer_type="text", source="questionnaire")
    ex = await _open(tmp_path)
    try:
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        opts = {o["label"].strip(): o["ref"] for g in snap.groups for o in g["options"]}
        key = "background.previously_employed_at_this_company"
        out, _ = await d.t_fill_batch([{"ref": opts["Yes"], "answer_key": key, "value": None, "source": None, "label": "Previously employed?"}])
        assert out["results"][0]["status"] == "failed" and "tick the No option" in out["results"][0]["error"]
        assert await ex.page.evaluate("() => document.getElementById('py').checked") is False
        out, _ = await d.t_fill_batch([{"ref": opts["No"], "answer_key": key, "value": None, "source": None, "label": "Previously employed?"}])
        assert out["results"][0]["status"] == "set"
        assert await ex.page.evaluate("() => document.getElementById('pn').checked") is True
        out, _ = await d.t_fill_batch([{"ref": opts["No"], "answer_key": "work_authorization.authorized_to_work_in_us", "value": None, "source": None, "label": "x"}])
        assert out["results"][0]["status"] == "failed" and "tick the Yes option" in out["results"][0]["error"]
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_the_fill_script_never_clicks_a_radio_for_a_false_value(tmp_path):
    from goat.browser import fill
    ex = await _open(tmp_path)
    try:
        snap = await snapshot.take(ex)
        yes_ref = next(o["ref"] for g in snap.groups for o in g["options"] if o["label"].strip() == "Yes")
        r = (await fill.fill_batch(ex, [{"ref": yes_ref, "value": "No"}]))[0]
        assert not r.ok and "cannot be unticked" in r.error
        assert await ex.page.evaluate("() => document.getElementById('py').checked") is False
    finally:
        await ex.close(close_browser=True)


HARVEY = """<!doctype html><html><body style="margin:20px">
<fieldset><legend>Will you now or will you in the future require employment visa sponsorship?</legend>
  <label><input type="radio" name="sp" id="sy"> Yes, I will require Harvey to sponsor my employment</label>
  <label><input type="radio" name="sp" id="sn"> No, I do not require sponsorship to work in the country where this role is located</label></fieldset>
</body></html>"""


@pytest.mark.asyncio
async def test_a_no_option_written_as_a_sentence_takes_a_stored_no(store, tmp_path):
    """Harvey (2026-10-18): 'No, I do not require sponsorship ...' was refused as not-a-No (the pattern wanted 'no ')."""
    job = _job()
    answers.save_answer("work_authorization.requires_sponsorship_future", "No", answer_type="text", source="questionnaire")
    page = tmp_path / "harvey.html"
    page.write_text(HARVEY, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        opts = {o["label"].strip()[:3]: o["ref"] for g in snap.groups for o in g["options"]}
        key = "work_authorization.requires_sponsorship_future"
        out, _ = await d.t_fill_batch([{"ref": opts["Yes"], "answer_key": key, "value": None, "source": None, "label": "Sponsorship"}])
        assert out["results"][0]["status"] == "failed" and "tick the No option" in out["results"][0]["error"]
        out, _ = await d.t_fill_batch([{"ref": opts["No,"], "answer_key": key, "value": None, "source": None, "label": "Sponsorship"}])
        assert out["results"][0]["status"] == "set", out
        assert await ex.page.evaluate("() => document.getElementById('sn').checked") is True
        assert await ex.page.evaluate("() => document.getElementById('sy').checked") is False
    finally:
        await ex.close(close_browser=True)


SEASONS = """<!doctype html><html><body style="margin:20px">
<fieldset><legend>What is your anticipated graduation season?</legend>
  <label><input type="radio" name="gs" id="s28"> Spring 2028</label>
  <label><input type="radio" name="gs" id="f28"> Fall 2028</label></fieldset>
</body></html>"""


@pytest.mark.asyncio
async def test_a_stored_answer_with_a_bracketed_note_names_its_option(store, tmp_path):
    """Sierra (2026-10-18): 'Spring 2028 (May 2028)' was refused on the option 'Spring 2028'."""
    job = _job()
    answers.save_answer("bank.grad-term", "Spring 2028 (May 2028)", answer_type="text", source="hermes")
    page = tmp_path / "seasons.html"
    page.write_text(SEASONS, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        opts = {o["label"].strip(): o["ref"] for g in snap.groups for o in g["options"]}
        out, _ = await d.t_fill_batch([{"ref": opts["Fall 2028"], "answer_key": "bank.grad-term", "value": None, "source": None, "label": "Season"}])
        assert out["results"][0]["status"] == "failed"
        out, _ = await d.t_fill_batch([{"ref": opts["Spring 2028"], "answer_key": "bank.grad-term", "value": None, "source": None, "label": "Season"}])
        assert out["results"][0]["status"] == "set", out
        assert await ex.page.evaluate("() => document.getElementById('s28').checked") is True
    finally:
        await ex.close(close_browser=True)
