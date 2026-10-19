"""A combobox that opens only from its own button (Ashby's university search on Sierra, 2026-10-18: clicking or typing
into the input showed nothing; the "Open university dropdown" button beside it did)."""
import pytest

from goat import readback
from goat.browser import fill, snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<!doctype html><html><body style="margin:20px">
<div class="field"><label for="uni">What University do you currently attend?</label>
  <div class="combo"><input id="uni" role="combobox" aria-expanded="false" aria-autocomplete="list" autocomplete="off">
  <button type="button" aria-label="Open university dropdown" id="tog">v</button></div>
  <ul id="list" role="listbox" style="display:none"></ul></div>
<div class="field"><label for="gpa">What is your GPA?</label><input id="gpa" type="number" step="0.01"></div>
<button type="button" id="other" aria-label="Clear form">Clear</button>
<script>
  const UNIS = ["Texas A&M University", "Texas A&M University - Commerce", "University of Texas at Austin"];
  const inp = document.getElementById('uni'), list = document.getElementById('list');
  function render() {
    const q = inp.value.toLowerCase(); list.innerHTML = '';
    UNIS.filter(u => u.toLowerCase().includes(q)).forEach(u => {
      const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = u;
      li.addEventListener('mousedown', (e) => { e.preventDefault(); inp.value = u; list.style.display = 'none'; inp.setAttribute('aria-expanded', 'false'); });
      list.appendChild(li);
    });
  }
  document.getElementById('tog').addEventListener('click', () => { render(); list.style.display = 'block'; inp.setAttribute('aria-expanded', 'true'); });
  inp.addEventListener('input', () => { if (list.style.display === 'block') render(); });   // typing filters only an OPEN list
</script></body></html>"""


async def _open(tmp_path):
    page = tmp_path / "sierra.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_choose_opens_a_combobox_from_its_own_button(tmp_path):
    ex = await _open(tmp_path)
    try:
        snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if (i.get("label") or "").startswith("What University"))
        r = await fill.choose(ex, ref, ["Texas A&M University", "Texas A & M University"])
        assert r.ok, r.error
        assert await ex.page.evaluate("() => document.getElementById('uni').value") == "Texas A&M University"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_the_open_button_finder_ignores_unrelated_buttons(tmp_path):
    from goat.browser import js
    ex = await _open(tmp_path)
    try:
        snap = await snapshot.take(ex)
        gpa = next(i["ref"] for i in snap.items if (i.get("label") or "").startswith("What is your GPA"))
        uni = next(i["ref"] for i in snap.items if (i.get("label") or "").startswith("What University"))
        assert (await ex.eval(js.JS_OPEN_TOGGLE, uni))["label"] == "Open university dropdown"
        assert (await ex.eval(js.JS_OPEN_TOGGLE, gpa))["ok"] is False      # 'Clear form' is not a dropdown button
    finally:
        await ex.close(close_browser=True)


def test_a_number_box_that_normalizes_the_value_still_reads_back_equal():
    assert readback.values_equal("4.0", "4", "text")
    assert readback.values_equal("3.90", "3.9", "text")
    assert not readback.values_equal("4.0", "3.9", "text")
    assert not readback.values_equal("4.0", "", "text")


SCHOOLS = ["Texas A & M University", "Texas A&M University - College Station", "Texas A&M University-College Station", "Texas A&M"]


@pytest.mark.parametrize("offered,want", [
    (["Texas A&M", "Texas A&M University - Commerce"], "Texas A&M"),                      # Sierra's curated list
    (["Texas A&M University - Central Texas", "Texas A&M University - College Station"], "Texas A&M University - College Station"),
    (["Texas A&M University - Commerce"], None),                                       # another campus: never
    (["Texas A&M University-Kingsville"], None),
    (["Texas A & M University"], "Texas A & M University"),
])
def test_a_school_pick_never_lands_on_another_campus(offered, want):
    got = fill.best_option([{"text": t} for t in offered], SCHOOLS)
    assert (got or {}).get("text") == want


def test_dash_answers_outside_schools_still_match():
    assert fill.best_option([{"text": "Yes - I am authorized"}, {"text": "No - I am not"}], ["Yes"])["text"] == "Yes - I am authorized"
    assert fill.best_option([{"text": "Houston, TX"}], ["Houston"])["text"] == "Houston, TX"


def test_a_class_only_toggle_button_is_found():
    """Ashby's real markup (Sierra, 2026-10-18): the open button has no label, only a _toggleButton_ class."""
    import re
    from goat.browser import js
    m = re.search(r"const TOGGLE = (/.*?/i);", js.JS_OPEN_TOGGLE)
    assert m and "toggle[-_ ]?button" in m.group(1)
