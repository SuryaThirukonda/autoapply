"""Workday-style prompts that show the picked text without committing it (GE Vernova "How Did You Hear",
Nasdaq "Field of Study"): choose() must confirm a selected-item chip, recover through the option's own radio
or the keyboard, and say so honestly when nothing commits."""
from __future__ import annotations

import pytest

from goat import db
from goat.browser import fill, snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body style="font:14px sans-serif">
<div data-automation-id="formField-source" style="position:relative">
  <label id="l1">How Did You Hear About Us?</label>
  <div data-automation-id="multiSelectContainer">
    <div id="chips"></div>
    <input id="q" aria-labelledby="l1" role="combobox" aria-expanded="false" style="width:300px;height:28px">
    <span data-automation-id="promptIcon">v</span>
  </div>
  <div data-automation-id="errorMessage" id="err" style="color:#c00">The field How Did You Hear About Us? is required and must have a value.</div>
</div>
<ul id="lb" role="listbox" style="display:none;list-style:none;padding:0;margin:0;width:420px">
  <li role="option" data-automation-label="LinkedIn" style="height:30px"><span class="t" style="display:inline-block;width:320px">LinkedIn</span><input type="radio" name="src"></li>
  <li role="option" data-automation-label="Indeed" style="height:30px"><span class="t" style="display:inline-block;width:320px">Indeed</span><input type="radio" name="src"></li>
</ul>
<script>
  const q = document.getElementById('q'), lb = document.getElementById('lb'), chips = document.getElementById('chips'), err = document.getElementById('err');
  const items = () => Array.from(lb.querySelectorAll('li'));
  let hi = -1;
  function open() { lb.style.display = 'block'; q.setAttribute('aria-expanded', 'true'); }
  function commit(t) { chips.innerHTML = '<div data-automation-id="selectedItem">' + t + '</div>'; err.style.display = 'none';
                       lb.style.display = 'none'; q.value = ''; q.setAttribute('aria-expanded', 'false'); }
  q.addEventListener('click', open);
  q.addEventListener('input', open);
  items().forEach(li => {
    li.querySelector('.t').addEventListener('click', () => { q.value = li.dataset.automationLabel; });   // the bug: text only
    li.querySelector('input').addEventListener('click', () => { if (window.MODE === 'radio') commit(li.dataset.automationLabel); });
  });
  q.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { hi = Math.min(hi + 1, items().length - 1); e.preventDefault(); }
    if (e.key === 'Enter' && lb.style.display === 'block' && hi >= 0) { if (window.MODE === 'keyboard') commit(items()[hi].dataset.automationLabel); e.preventDefault(); }
    if (e.key === 'Escape') { lb.style.display = 'none'; hi = -1; }
  });
</script></body></html>"""


async def _open(mode: str):
    ex = Executor(policy=HostPolicy(["about:blank"]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    await ex.page.set_content(PAGE)
    await ex.page.evaluate(f"window.MODE = {mode!r}")
    snap = await snapshot.take(ex)
    ref = next(i["ref"] for i in snap.items if "How Did You Hear" in (i.get("label") or "") and i.get("tag") in ("input", "combobox", "dropdown"))
    return ex, ref


@pytest.mark.parametrize("mode", ["radio", "keyboard"])
async def test_a_prompt_that_only_shows_text_is_committed_another_way(tmp_path, mode):
    db.reset_for_tests(tmp_path / "t.db")
    ex, ref = await _open(mode)
    try:
        r = await fill.choose(ex, ref, ["LinkedIn"], type_to_filter=False)
        assert r.ok, r.error
        chip = await ex.page.evaluate("() => (document.querySelector('[data-automation-id=selectedItem]') || {}).textContent")
        assert chip == "LinkedIn"
        assert await ex.page.evaluate("() => getComputedStyle(document.getElementById('err')).display") == "none"
    finally:
        await ex.close()


async def test_a_prompt_that_never_commits_is_reported_honestly(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    ex, ref = await _open("never")
    try:
        r = await fill.choose(ex, ref, ["LinkedIn"], type_to_filter=False)
        assert not r.ok and "did not commit" in r.error
    finally:
        await ex.close()


EXPANDED = """<html><body>
<div data-automation-id="multiSelectContainer" aria-expanded="true">
  <label id="l2">How Did You Hear About Us?</label>
  <input id="q2" aria-labelledby="l2" value="LinkedIn" style="width:300px;height:28px">
</div>
<span data-automation-id="promptIcon">v</span>
</body></html>"""


async def test_an_expanded_prompt_is_still_a_prompt(tmp_path):
    """Gordon Food Service (2026-10-20): with a category open, the container was the closest box, the field no longer
    read as a prompt, and the typed search text 'LinkedIn' passed as its value."""
    from goat.browser import js
    db.reset_for_tests(tmp_path / "t.db")
    ex = Executor(policy=HostPolicy(["about:blank"]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page()
        await ex.page.set_content(EXPANDED)
        await ex.page.evaluate("() => document.getElementById('q2').setAttribute('data-goat-ref', 'e7')")
        st = await ex.eval(js.JS_FIELD_COMMIT, "e7")
        assert st["prompt"] is True and st["chips"] == []
    finally:
        await ex.close()


NESTED = """<html><body style="font:14px sans-serif">
<div data-automation-id="formField-source">
  <label id="l3">How Did You Hear About Us?</label>
  <div data-automation-id="multiSelectContainer">
    <div id="chips3"></div>
    <input id="q3" aria-labelledby="l3" role="combobox" style="width:300px;height:28px">
    <span data-automation-id="promptIcon">v</span>
  </div>
</div>
<ul id="lb3" role="listbox" style="display:none;list-style:none;padding:0;margin:0;width:420px">
  <li role="option" data-automation-id="promptOption" style="height:30px">Employment Agency / Search Firm</li>
  <li role="option" data-automation-id="promptOption" style="height:30px">Job Board</li>
</ul>
<script>
  const q3 = document.getElementById('q3'), lb3 = document.getElementById('lb3'), chips3 = document.getElementById('chips3');
  q3.addEventListener('click', () => { lb3.style.display = 'block'; });
  q3.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault();
      if (q3.value.trim().toLowerCase() === 'linkedin') { chips3.innerHTML = '<div data-automation-id="selectedItem">LinkedIn</div>'; q3.value = ''; lb3.style.display = 'none'; } }
    if (e.key === 'Escape') { lb3.style.display = 'none'; }
  });
  lb3.querySelectorAll('li').forEach(li => li.addEventListener('click', () => {
    chips3.innerHTML = '<div data-automation-id="selectedItem">' + li.textContent + '</div>'; lb3.style.display = 'none'; }));
</script></body></html>"""


async def test_a_nested_first_preference_is_searched_before_a_lower_one(tmp_path):
    """Ecolab (2026-10-20): LinkedIn sat inside a category and the chain settled for 'Employment Agency / Search Firm'."""
    db.reset_for_tests(tmp_path / "t.db")
    ex = Executor(policy=HostPolicy(["about:blank"]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page()
        await ex.page.set_content(NESTED)
        snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if "How Did You Hear" in (i.get("label") or ""))
        r = await fill.choose(ex, ref, ["LinkedIn", "Job Board", "Agency / Search Firm", "Other"])
        assert r.ok and r.readback == "LinkedIn", r
        chip = await ex.page.evaluate("() => (document.querySelector('[data-automation-id=selectedItem]') || {}).textContent")
        assert chip == "LinkedIn"
    finally:
        await ex.close()
