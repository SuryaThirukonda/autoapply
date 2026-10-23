"""PACCAR (2026-10-23): 'Forgot your password?' opened SuccessFactors' 'Password Help' form slowly, the run's snapshot
caught the page still loading, and it called open_password_reset_link without clicking Submit: three minutes of waiting for
an email that was never requested, then a credentials_unknown hold. The tool now names the send button first, once."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from goat import db
from goat.agent.tools import unsent_reset_form
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

FIX = Path(__file__).parent / "fixtures" / "successfactors"


async def _open(name: str) -> Executor:
    url = (FIX / name).resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    await ex.page.goto(url)
    await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_a_password_help_form_not_sent_names_its_button(store):
    ex = await _open("password_help.html")
    try:
        assert await unsent_reset_form(ex) == {"send": "Submit", "filled": False}
        await ex.page.fill("#email", "someone@example.com")
        assert (await unsent_reset_form(ex))["filled"] is True
        await ex.page.click("#send")                                   # sent: the form gives way to a notice
        assert await unsent_reset_form(ex) == {}
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_sign_in_page_is_no_reset_form(store):
    ex = await _open("sign_in.html")
    try:
        assert await unsent_reset_form(ex) == {}
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_the_reset_tool_sends_the_run_to_the_button_once_before_waiting_for_mail(store):
    ex = await _open("password_help.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        calls = []

        def fetch(portal, host, after, **k):
            calls.append(host)
            return SimpleNamespace(status="not_received", url=None)
        ctx.mail = SimpleNamespace(fetch_password_reset_link=fetch)
        out, _ = await d.t_open_password_reset_link()
        assert "Submit" in out["error"] and "identity.email" in out["error"] and calls == [] and not ctx.reset_link_tried
        kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=?", (ctx.job_id,))]
        assert "creds:reset_form_unsent" in kinds
        # a form that stays on the page after its send click never holds the run twice: the mail is polled
        out, _ = await d.t_open_password_reset_link()
        assert out["status"] == "not_received" and len(calls) == 1 and ctx.reset_link_tried
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_page_that_cannot_be_read_never_blocks_the_poll(store):
    d, ctx = _dispatcher(_job())
    ctx.ex = SimpleNamespace(page=None, url="https://career5.successfactors.eu/career")
    ctx.mail = SimpleNamespace(fetch_password_reset_link=lambda portal, host, after, **k: SimpleNamespace(status="not_received", url=None))
    out, _ = await d.t_open_password_reset_link()
    assert out["status"] == "not_received" and not ctx.reset_form_nudged
