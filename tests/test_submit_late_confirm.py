"""L3Harris (SuccessFactors, 2026-10-23): 'Successfully Applied to Image Processing Engineer Intern' appeared about 20 s
after the Apply click, the submit was called failed at 15 s, and the run held 'stuck' over an application that had gone
in. A confirmation that comes after the wait still counts."""
import pytest

from goat.browser import submit as submit_mod
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><head><title>Career Opportunities: Apply for Image Processing Engineer Intern (46603)</title></head><body>
<input value='Surya'><input value='Thirukonda'>
<button type='submit' style='width:100px;height:30px' onclick="setTimeout(() => {
  document.title = 'Career Opportunities: Successfully Applied to Image Processing Engineer Intern (46603)';
  document.body.innerHTML = '<h1>Successfully Applied to Image Processing Engineer Intern (46603)</h1>'; }, 3500)">Submit Application</button>
</body></html>"""
INERT = PAGE.replace("onclick=", "data-x=")


async def _run(html, monkeypatch, late_s):
    monkeypatch.setattr(submit_mod, "LATE_CONFIRM_S", late_s)
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(html)
        return await submit_mod.submit(ex, None, label="Submit Application", wait_s=1.5)
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_a_confirmation_after_the_wait_is_a_submission(monkeypatch):
    res = await _run(PAGE, monkeypatch, 8.0)
    assert res.status == "submitted" and "Successfully Applied" in res.signal, res


@pytest.mark.asyncio
async def test_a_control_that_never_moves_still_fails(monkeypatch):
    res = await _run(INERT, monkeypatch, 3.0)
    assert res.status == "failed" and "inert" in " ".join(res.why), res
