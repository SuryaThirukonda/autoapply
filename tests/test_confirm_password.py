"""Password-reset pages refused the new password as 'passwords do not match' (Micron, GDIT, Westinghouse, 2026-10-23):
the verify box held a retype appended to a partial first one. The confirm box is compared with the password box in the
page and set again from it when they differ."""
from pathlib import Path

import pytest

from goat import db
from goat.agent.tools import match_confirm_password
from goat.browser.executor import Executor, HostPolicy

FIX = Path(__file__).parent / "fixtures" / "workday" / "password_reset.html"


async def _open(tmp_path) -> Executor:
    db.reset_for_tests(tmp_path / "t.db")
    url = FIX.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    await ex.page.goto(url)
    await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_a_confirm_box_that_differs_is_set_from_the_password_box(tmp_path):
    ex = await _open(tmp_path)
    try:
        assert await match_confirm_password(ex, "e2", "e3") == "repaired"
        lens = await ex.page.evaluate("() => [document.getElementById('input-4').value.length, document.getElementById('input-5').value.length]")
        assert lens == [10, 10]
        assert await ex.page.evaluate("() => window.__seen['input-5']") == 10          # the form saw it as typed input
        assert await match_confirm_password(ex, "e2", "e3") == "same"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_empty_password_box_is_never_copied(tmp_path):
    ex = await _open(tmp_path)
    try:
        await ex.page.evaluate("() => { document.getElementById('input-4').value = ''; }")
        assert await match_confirm_password(ex, "e2", "e3") == "differs"
        assert await match_confirm_password(ex, "e2", "e9") == "differs"              # a box that is not on the page
    finally:
        await ex.close(close_browser=True)
