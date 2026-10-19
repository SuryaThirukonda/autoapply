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
        evs = db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='gate:readback'").fetchone()[0]
        assert evs == 1
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
        out, _ = await d.t_click(_ref(snap, "Next"), "Next")
        assert "error" not in out and ctx.page_no == 2
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
