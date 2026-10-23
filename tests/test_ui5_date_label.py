"""Westinghouse (SuccessFactors, 2026-10-23): 'When are you available to start work?' is a UI5 date picker whose input sits two
shadow roots deep; only the outer host names it (accessible-name / title). The input had no label, the run left it empty,
and the judge failed the draft."""
import pytest

from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<!doctype html><html><body style="margin:20px">
<div class="RCMFormField rcmFormElement"><label for="499:_datepicker" class="rcmFormFieldLabel">When are you available to start work?</label>
<div id="499:_datepicker"><x-date-picker accessible-name="When are you available to start work?"
  title="When are you available to start work?"></x-date-picker></div></div>
<script>
customElements.define('x-datetime-input', class extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: 'open' }).innerHTML = '<input type="text" placeholder="MM/DD/YYYY">'; } });
customElements.define('x-date-picker', class extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: 'open' }).innerHTML = '<x-datetime-input></x-datetime-input>'; } });
</script></body></html>"""


@pytest.mark.asyncio
async def test_a_ui5_date_inputs_label_comes_from_its_outer_host(tmp_path):
    page = tmp_path / "ui5.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        inputs = [i for i in snap.items if i.get("tag") == "input"]
        assert inputs and inputs[0]["label"] == "When are you available to start work?", [(i.get("label"), i.get("tag")) for i in snap.items]
    finally:
        await ex.close(close_browser=True)
