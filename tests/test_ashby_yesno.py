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


STOKE = """<!doctype html><html><body style="margin:20px">
<div class="select__container"><label id="q1-label" for="q1">Are you authorized to work lawfully in the United States?*</label>
  <input id="q1" value="Yes" aria-errormessage="q1-error"></div>
<div class="field"><label>In an essay of about 750 words, please answer: What is the hardest thing you have done?*</label>
  <div><button type="button">Attach</button><label class="visually-hidden" for="q2">Attach</label>
  <input id="q2" class="visually-hidden" type="file"></div>
  <p id="q2-error" style="color:#c00">This field is required.</p></div>
<button type="submit">Submit application</button>
</body></html>"""


@pytest.mark.asyncio
async def test_a_field_error_names_its_own_question_not_the_nearest_label(tmp_path):
    """Stoke Space (2026-10-18): the essay upload's 'This field is required.' was blamed on the authorization question."""
    page = tmp_path / "stoke.html"
    page.write_text(STOKE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        why = why_blocked(await scan_errors(ex))
        assert why and all("hardest thing you have done" in w for w in why if "This field is required" in w), why
        assert not any("authorized to work" in w for w in why), why
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_toggle_is_pressed_with_a_real_mouse_click(tmp_path):
    """The page below only answers to pointer events: a script click (el.click()) leaves it unpressed."""
    html = """<!doctype html><html><body style="margin:20px"><label>Are you authorized?</label>
      <button type="button" id="y" aria-pressed="false">Yes</button><button type="button" id="n" aria-pressed="false">No</button>
      <script>for (const b of document.querySelectorAll('button')) b.addEventListener('pointerdown', () => {
        for (const o of document.querySelectorAll('button')) o.setAttribute('aria-pressed', String(o === b)); });</script></body></html>"""
    from goat.browser import fill
    page = tmp_path / "toggle.html"
    page.write_text(html, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        yes = next(i["ref"] for i in snap.items if i.get("tag") == "toggle" and i.get("label") == "Yes")
        r = (await fill.fill_batch(ex, [{"ref": yes, "value": "true"}]))[0]
        assert r.ok and r.readback == "pressed", r
        assert await ex.page.evaluate("() => document.getElementById('y').getAttribute('aria-pressed')") == "true"
        r = (await fill.fill_batch(ex, [{"ref": yes, "value": "true"}]))[0]     # already pressed: no click, still pressed
        assert r.ok and r.readback == "pressed"
    finally:
        await ex.close(close_browser=True)
