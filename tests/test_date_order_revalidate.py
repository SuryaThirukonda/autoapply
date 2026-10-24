"""GlobalFoundries (Workday, 2026-10-23): the certification's Expiration Date was validated while Issued Date read 06/26/6262,
kept 'Must end after start date' after Issued was fixed to 06/26/2026, and the run parked on it."""
import pytest

from goat.browser import fill, snapshot
from goat.browser.executor import Executor, HostPolicy


def _widget(name, m, d, y, err=""):
    return (f'<div data-automation-id="formField-{name}"><fieldset><legend><label>{name}</label></legend>'
            f'<div data-automation-id="dateInputWrapper" role="group">'
            f'<input role="spinbutton" aria-label="Month" data-automation-id="dateSectionMonth-input" value="{m}" size="2">'
            f'<input role="spinbutton" aria-label="Day" data-automation-id="dateSectionDay-input" value="{d}" size="2">'
            f'<input role="spinbutton" aria-label="Year" data-automation-id="dateSectionYear-input" value="{y}" size="4"></div>'
            f'<p data-automation-id="inputAlert" class="err">{err}</p></fieldset></div>')


PAGE = ("<!doctype html><html><body style='margin:20px'><form>" + _widget("issuedDate", "6", "26", "6262")
        + _widget("expirationDate", "6", "26", "2029", "Error: Must end after start date") + """</form>
<script>
function val(f) { const i = f.querySelectorAll('input'); return Number(i[2].value) * 10000 + Number(i[0].value) * 100 + Number(i[1].value); }
const s = document.querySelector('[data-automation-id="formField-issuedDate"]'), e = document.querySelector('[data-automation-id="formField-expirationDate"]');
e.querySelectorAll('input').forEach(i => i.addEventListener('blur', () => {
  e.querySelector('.err').textContent = val(e) > val(s) ? '' : 'Error: Must end after start date'; }));
</script></body></html>""")


@pytest.mark.asyncio
async def test_a_later_dates_order_error_is_revalidated_after_the_start_date_is_fixed(tmp_path):
    page = tmp_path / "gf.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        month = next(i for i in snap.items if (i.get("label") or "").startswith("Month") or "issuedDate" in (i.get("label") or ""))
        r = await fill.set_date(ex, month["ref"], "06/26/2026")
        assert r.ok, r.error
        err = await ex.page.evaluate("() => document.querySelector('[data-automation-id=\"formField-expirationDate\"] .err').textContent")
        assert err == "", err
    finally:
        await ex.close(close_browser=True)
