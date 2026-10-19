"""Oracle Recruiting Cloud (onsemi, 2026-10-19): the cx-select picker input holds its own pick, and the row around it
holds the label and the error text."""
from __future__ import annotations

from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body>
<div class="input-row__control">
  <label id="lm">Start Date Month</label>
  <input id="m" class="cx-select-input" role="combobox" aria-expanded="false" aria-labelledby="lm" value="">
  <button id="m-toggle" aria-label="Open the drop-down list for Month.">v</button>
  <span>Month Year</span>
  <div class="input-row__error">You need to enter the whole date otherwise the date will not be saved.</div>
</div>
<div class="input-row__control">
  <label id="ly">Start Date Year</label>
  <input id="y" class="cx-select-input" role="combobox" aria-expanded="false" aria-labelledby="ly" value="2026">
</div>
</body></html>"""


async def test_an_oracle_picker_reads_its_own_value_not_its_row():
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        snap = await snapshot.take(ex)
        month = next(i for i in snap.items if (i.get("label") or "").startswith("Start Date Month"))
        year = next(i for i in snap.items if (i.get("label") or "").startswith("Start Date Year"))
        assert month.get("combo") and not month.get("value") and not month.get("shown"), month
        assert year.get("value") == "2026", year
        info = await ex.read_ref(month["ref"])
        assert not info.get("shown") and info.get("value") == "", info
        await ex.page.evaluate("() => { document.getElementById('m').value = 'October'; }")
        info = await ex.read_ref(month["ref"])
        assert info.get("value") == "October" and not info.get("shown"), info
    finally:
        await ex.close()
