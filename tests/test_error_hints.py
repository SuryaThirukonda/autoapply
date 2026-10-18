"""Greenhouse helper text under a FILLED picker ("Please choose the closest conversion" below a GPA select
showing "3.7 - 4.0") is a hint, not a validation error; the same text beside an EMPTY picker still is one
(Verkada, 2026-10-18)."""
from __future__ import annotations

from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body>
<div class="field"><label id="g">What is your GPA?*</label>
  <div class="select__control"><div class="select__value-container"><div class="select__single-value">%s</div>
    <input id="gpa" role="combobox" aria-labelledby="g" class="select__input" style="width:2px"></div></div>
  <div class="helper-text">Please choose the closest conversion</div></div>
<div class="field"><label for="n">Name</label><input id="n" value="Surya"></div>
</body></html>"""


async def _errors(value: str) -> list[str]:
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE % value)
        snap = await snapshot.take(ex)
        return [e.get("text") or "" for e in snap.errors["error_text"] + snap.errors["alerts"]]
    finally:
        await ex.close()


async def test_please_choose_under_a_filled_picker_is_not_an_error():
    assert not any("closest conversion" in t for t in await _errors("3.7 - 4.0"))


async def test_please_choose_beside_an_empty_picker_still_is():
    assert any("closest conversion" in t for t in await _errors("Select..."))
