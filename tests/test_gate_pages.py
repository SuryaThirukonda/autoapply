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
