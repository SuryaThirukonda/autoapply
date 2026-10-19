"""A react-select-like picker whose options are sentences (Lyft's 'Work Authorization'): a stored Yes takes the
one affirmative sentence, and a typed wording that filters the list to nothing is cleared again."""
import pytest

from goat import db
from goat.browser import fill

PAGE = """<!doctype html><html><body style="margin:40px">
<div class="field"><label for="wa">Work Authorization*</label>
<div class="select__control" id="ctl" style="width:600px;border:1px solid #888;padding:6px">
  <span id="shown" class="select__single-value">Select...</span>
  <input id="wa" data-goat-ref="e34" role="combobox" aria-autocomplete="list" style="width:4px;border:0;outline:0">
</div></div>
<script>
  const OPTS = ['I am authorized to work for any employer in the country in which this position is based.',
                "I require/will require Lyft's sponsorship to obtain work authorization.",
                'My status to work in this country is unknown.'];
  const inp = document.getElementById('wa'); let menu = null;
  function render() {
    const q = inp.value.toLowerCase();
    if (!menu) { menu = document.createElement('div'); menu.className = 'select__menu'; menu.style.cssText = 'border:1px solid #888;width:600px'; document.getElementById('ctl').after(menu); }
    menu.innerHTML = '';
    for (const t of OPTS.filter(t => t.toLowerCase().includes(q))) {
      const d = document.createElement('div'); d.className = 'select__option'; d.setAttribute('role', 'option'); d.textContent = t; d.style.cssText = 'padding:6px;height:20px';
      d.addEventListener('mousedown', (e) => { e.preventDefault(); document.getElementById('shown').textContent = t; inp.value = ''; close(); });
      menu.appendChild(d);
    }
  }
  function close() { if (menu) { menu.remove(); menu = null; } }
  document.getElementById('ctl').addEventListener('mousedown', (e) => { e.preventDefault(); inp.focus(); menu ? close() : render(); });
  inp.addEventListener('input', render);
  inp.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  document.addEventListener('mousedown', (e) => { if (!e.target.closest('#ctl') && !e.target.closest('.select__menu')) close(); });
</script></body></html>"""


def test_yes_maps_to_the_affirmative_sentence_and_no_to_the_refusal():
    opts = [{"text": t} for t in ("I am authorized to work for any employer in the country in which this position is based.",
                                  "I require/will require Lyft's sponsorship to obtain work authorization.", "My status to work in this country is unknown.")]
    assert fill.best_option(opts, ["Yes"])["text"].startswith("I am authorized")
    assert fill.best_option(opts, ["No"]) is None                       # no single refusal sentence: never a guess
    spons = [{"text": "I will not require sponsorship"}, {"text": "I will require sponsorship"}]
    assert fill.best_option(spons, ["No"])["text"] == "I will not require sponsorship"


@pytest.mark.asyncio
async def test_choose_clears_a_filter_that_matched_nothing_and_picks_the_affirmative(tmp_path):
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "rs.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        r = await fill.choose(ex, "e34", ["Yes", "Authorized to work in the United States"])
        assert r.ok, r.error
        assert (await ex.page.inner_text("#shown")).startswith("I am authorized")
    finally:
        await ex.close(close_browser=True)
