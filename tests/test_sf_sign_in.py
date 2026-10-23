"""Westinghouse (SuccessFactors, 2026-10-23): the requeued run re-attached to its old tab, a sign-in page at a used session
crumb ('career?_s.crb=...') that only repeated the old 'Invalid email address or password'; and a Sign In click read no
failure because SuccessFactors draws the message after the reload settles, so the reset was refused as 'sign in first'."""
from types import SimpleNamespace

import pytest

from goat import db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.worker import stale_sign_in_tab
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures


def _tab(url, title):
    async def get_title():
        return title
    return SimpleNamespace(url=url, page=SimpleNamespace(title=get_title))


@pytest.mark.asyncio
async def test_a_sign_in_page_at_a_used_session_crumb_is_stale():
    crumb = "https://career4.successfactors.com/career?_s.crb=jnl9lvJgGIw%252fQiJ2Ip"
    assert await stale_sign_in_tab(_tab(crumb, "Career Opportunities: Sign In"))
    assert await stale_sign_in_tab(_tab(crumb, "Career Opportunities: Password Help"))
    assert not await stale_sign_in_tab(_tab(crumb, "Career Opportunities: Application"))         # a held form stays
    assert not await stale_sign_in_tab(_tab("https://career4.successfactors.com/careers?company=WestinghouseP",
                                            "Career Opportunities: Sign In"))


SIGN_IN = """<!doctype html><html><head><title>Career Opportunities: Sign In</title></head><body style="margin:20px">
<h1>Career Opportunities: Sign In</h1>
<div id="err" role="alert"></div>
<label for="e">Email Address:*</label><input id="e" type="text">
<label for="p">Password:*</label><input id="p" type="password">
<button type="button" id="go" onclick="setTimeout(() => { document.getElementById('err').textContent =
  'Invalid email address or password. Please re-enter your login info.'; document.getElementById('p').value = ''; }, 3200)">Sign In</button>
</body></html>"""


@pytest.mark.asyncio
async def test_a_late_sign_in_failure_is_still_read_as_failed(store, tmp_path):
    page = tmp_path / "signin.html"
    page.write_text(SIGN_IN, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.creds_mode = "login_universal"
        ctx.last_snapshot = await snapshot.take(ex)
        ref = next(i["ref"] for i in ctx.last_snapshot.items if (i.get("label") or "").strip() == "Sign In")
        out, _ = await d.t_click(ref, "Sign In")
        assert out.get("sign_in") == "failed" and "open_password_reset_link" in out.get("next", ""), out
        assert ctx.reset_needed
        kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=?", (ctx.job_id,))]
        assert "creds:sign_in_failed" in kinds
    finally:
        await ex.close(close_browser=True)


def test_a_navigation_under_the_prefill_is_no_run_error():
    """Westinghouse (2026-10-23): the page navigated while the prefill picked a dropdown and the run was held as an error."""
    from goat.worker import prefill_navigation_error
    assert prefill_navigation_error(Exception("Page.evaluate: Execution context was destroyed, most likely because of a navigation"))
    assert prefill_navigation_error(Exception("Frame was detached"))
    assert not prefill_navigation_error(Exception("KeyError: 'ref'"))


POSTING = """<!doctype html><html><head><title>Summer Intern - Software Engineering Job Details</title></head><body>
<h1>Summer Intern - Software Engineering</h1><a href="#apply" id="apply">Apply now</a></body></html>"""
PROFILE = """<!doctype html><html><head><title>Career Opportunities: My Profile</title></head><body>
<h1>Candidate Profile</h1><button type="button">Save</button></body></html>"""
SIGN_IN_TO_PROFILE = """<!doctype html><html><head><title>Career Opportunities: Sign In</title></head><body>
<h1>Career Opportunities: Sign In</h1>
<label for="e">Email Address:*</label><input id="e" type="text">
<label for="p">Password:*</label><input id="p" type="password">
<button type="button" onclick="location.href='profile.html?navBarLevel=MY_PROFILE'">Sign In</button>
</body></html>"""


@pytest.mark.asyncio
async def test_a_sign_in_that_lands_on_my_profile_reopens_the_posting(store, tmp_path):
    """Westinghouse (2026-10-23): SuccessFactors signed in to 'My Profile'; the run never found the application again."""
    for name, html in (("posting.html", POSTING), ("profile.html", PROFILE), ("signin.html", SIGN_IN_TO_PROFILE)):
        (tmp_path / name).write_text(html, encoding="utf-8")
    posting = (tmp_path / "posting.html").resolve().as_uri()
    url = (tmp_path / "signin.html").resolve().as_uri()
    ex = Executor(policy=HostPolicy([url, posting]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        job = _job()
        job["url"] = posting
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        ctx.creds_mode = "login_universal"

        async def goto(u):                  # file:// pages have no host for the allowlist
            await ex.page.goto(u)
        ex.goto = goto
        ctx.last_snapshot = await snapshot.take(ex)
        ref = next(i["ref"] for i in ctx.last_snapshot.items if (i.get("label") or "").strip() == "Sign In")
        out, _ = await d.t_click(ref, "Sign In")
        assert out.get("signed_in") and "Apply now" in out.get("next", ""), out
        assert ex.url.split("?")[0].endswith("posting.html")
        kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=?", (ctx.job_id,))]
        assert "creds:signed_in" in kinds
    finally:
        await ex.close(close_browser=True)
