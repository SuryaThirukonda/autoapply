"""Workday-style date sections that show typed digits without committing them (GE Vernova and Cigna
work-experience "From" dates stuck on "Invalid Date"): set_date steps the spinbuttons with the arrow keys."""
from __future__ import annotations

from goat import db
from goat.browser import fill, snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body style="font:14px sans-serif">
<div data-automation-id="formField-startDate"><label id="lab">From*</label>
  <div data-automation-id="dateInputWrapper">
    <input data-automation-id="dateSectionMonth-input" aria-label="Month" role="spinbutton" style="width:40px;height:24px">
    <input data-automation-id="dateSectionYear-input" aria-label="Year" role="spinbutton" style="width:60px;height:24px">
  </div>
  <div id="msg"></div>
</div>
<input id="next" aria-label="Next field" style="margin-top:20px">
<script>
  const m = document.querySelector('[aria-label=Month]'), y = document.querySelector('[aria-label=Year]');
  let cm = null, cy = null;
  m.addEventListener('input', () => { cm = parseInt(m.value) || null; });          // the month commits when typed
  y.addEventListener('input', () => {});                                           // the year does not (the bug)
  function arrows(el, lo, hi, setC) { el.addEventListener('keydown', e => {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    const cur = parseInt(el.value) || (e.key === 'ArrowUp' ? lo - 1 : hi + 1);
    el.value = String(Math.min(hi, Math.max(lo, cur + (e.key === 'ArrowUp' ? 1 : -1)))); setC(parseInt(el.value)); e.preventDefault(); }); }
  arrows(m, 1, 12, v => cm = v); arrows(y, 1950, 2100, v => cy = v);
  function check() { document.getElementById('msg').textContent = (cm === parseInt(m.value) && cy && cy === parseInt(y.value)) ? '' : 'Invalid Date'; }
  [m, y].forEach(el => el.addEventListener('blur', check));
</script></body></html>"""


async def test_a_date_that_types_but_does_not_commit_is_committed_with_the_arrows(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if (i.get("label") or "").lower().startswith("month"))
        r = await fill.set_date(ex, ref, "02/2026")
        assert r.ok, r.error
        assert await ex.page.evaluate("() => document.getElementById('msg').textContent") == ""
        assert await ex.page.evaluate("() => document.querySelector('[aria-label=Year]').value") == "2026"
    finally:
        await ex.close()
