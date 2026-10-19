"""Ashby's Yes/No questions (Niantic, 2026-10-18): two toggle buttons plus an untabbable checkbox that mirrors them. The
snapshot listed the mirror as its own field, the agent set it too, the answer went out of sync and Ashby refused the
submit with "Missing entry for required field: ..." while our post-click scan saw no error."""
import pytest

from goat.browser import snapshot
from goat.browser.errors import scan_errors, why_blocked
from goat.browser.executor import Executor, HostPolicy

PAGE = """<!doctype html><html><body style="margin:20px">
<div class="ashby-application-form-field-entry"><label for="q1">Are you authorized to work without restrictions?</label>
  <div class="ashby-application-form-input-yesno">
    <button type="button" aria-pressed="false" data-option="yes" onclick="this.setAttribute('aria-pressed','true')">Yes</button>
    <button type="button" aria-pressed="false" data-option="no">No</button>
    <input type="checkbox" tabindex="-1" name="q1"></div></div>
<div id="banner" style="display:none"><p>Your form needs corrections</p>
  <p>Missing entry for required field: Are you authorized to work without restrictions?</p></div>
<button type="submit">Submit Application</button>
</body></html>"""


async def _open(tmp_path):
    page = tmp_path / "ashby.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_the_yes_no_mirror_checkbox_is_not_a_field(tmp_path):
    ex = await _open(tmp_path)
    try:
        snap = await snapshot.take(ex)
        assert not [i for i in snap.items if i.get("type") == "checkbox"], [i.get("label") for i in snap.items]
        assert [i["label"] for i in snap.items if i.get("tag") == "toggle"] == ["Yes", "No"]
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_refused_submit_summary_is_reported_apart_and_never_blocks_the_gate(tmp_path):
    """The summary proves a click was refused (submit reports it), but Ashby keeps it on screen until the next submit,
    so the gate's error list must not hold it, or the corrected resubmit could never happen."""
    ex = await _open(tmp_path)
    try:
        scan = await scan_errors(ex)
        assert not why_blocked(scan) and not scan.get("summaries")
        await ex.page.evaluate("() => { document.getElementById('banner').style.display = 'block'; }")
        scan = await scan_errors(ex)
        assert any("Missing entry for required field" in x["text"] for x in scan["summaries"]), scan
        assert not why_blocked(scan)
    finally:
        await ex.close(close_browser=True)
