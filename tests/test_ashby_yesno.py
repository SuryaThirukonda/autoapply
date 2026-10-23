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


WALRUS = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
<div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry"><label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 ashby-application-form-question-title" for="q1">Are you authorized to work in the United States?</label>
  <div class="_container_1svni_28 _yesno_1e3gg_148 ashby-application-form-input-yesno">
    <button type="button" class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="yes" onclick="for (const b of this.parentElement.querySelectorAll('button')) b.setAttribute('aria-pressed', b === this ? 'true' : 'false')">Yes</button>
    <button type="button" class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="no" onclick="for (const b of this.parentElement.querySelectorAll('button')) b.setAttribute('aria-pressed', b === this ? 'true' : 'false')">No</button>
    <input type="checkbox" class="_input_1svni_78" style="position:absolute;width:0;height:0;opacity:0" tabindex="-1" name="q1"></div></div>
<div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry"><label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 ashby-application-form-question-title" for="q2">Will you now or in the future require employment visa sponsorship?</label>
  <div class="_container_1svni_28 _yesno_1e3gg_148 ashby-application-form-input-yesno">
    <button type="button" class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="yes" onclick="for (const b of this.parentElement.querySelectorAll('button')) b.setAttribute('aria-pressed', b === this ? 'true' : 'false')">Yes</button>
    <button type="button" class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="no" onclick="for (const b of this.parentElement.querySelectorAll('button')) b.setAttribute('aria-pressed', b === this ? 'true' : 'false')">No</button>
    <input type="checkbox" class="_input_1svni_78" style="position:absolute;width:0;height:0;opacity:0" tabindex="-1" name="q2"></div></div>
<button type="submit">Submit Application</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_a_required_yes_no_with_neither_pressed_is_empty(tmp_path):
    """Walrus (2026-10-23): Ashby cleared the pressed Yes of 'Are you authorized to work in the United States?' and the
    gate passed a draft with the required question unanswered; only the judge saw it."""
    page = tmp_path / "walrus.html"
    page.write_text(WALRUS, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        empty = [(r["label"], r["type"]) for r in snap.required_empty()]
        assert empty == [("Are you authorized to work in the United States?", "yesno"),
                         ("Will you now or in the future require employment visa sponsorship?", "yesno")], empty
        await ex.page.evaluate("() => document.querySelectorAll('button[data-option=yes]')[0].click()")
        snap = await snapshot.take(ex)
        assert [r["label"] for r in snap.required_empty()] == ["Will you now or in the future require employment visa sponsorship?"]
    finally:
        await ex.close(close_browser=True)


def test_a_toggle_decision_named_by_its_question_is_read_back():
    """Walrus (2026-10-23): the decision 'Authorized to work' = Yes sat on the button labelled 'Yes'; with no word in
    common the read-back skipped it while the button showed unpressed."""
    from goat import readback
    from goat.browser.snapshot import Snapshot
    items = [{"ref": "e11", "tag": "toggle", "label": "Yes", "pressed": False, "group": "Are you authorized to work in the United States?"},
             {"ref": "e12", "tag": "toggle", "label": "No", "pressed": False, "group": "Are you authorized to work in the United States?"}]
    s = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    s.refs = {i["ref"]: i for i in items}
    out = readback.diff([{"ref": "e11", "field_label": "Authorized to work", "value": "Yes",
                          "source": "standing:work_authorization.authorized_to_work_in_us", "page": 1}], s, page_no=1)
    assert out and "[e11]" in out[0], out
