"""ADT (Oracle, 2026-10-23): the verified Submit asked for an emailed code; the run entered it and clicked Verify, Oracle
showed 'Thank you for your job application' and listed the application, and the run was held as stuck: only the submit
tool could record a submission."""
import pytest

from goat import db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.agent.tools import COMPLETED_TEXT
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px">
<div id="box"><p>Enter verification code</p><input id="c1" type="text" value="1">
<button type="button" id="v" onclick="document.getElementById('box').innerHTML='<h1>Thank you for your job application</h1>'">VERIFY</button></div>
</body></html>"""


def test_thank_you_for_your_job_application_is_a_completed_page():
    assert COMPLETED_TEXT.search("Thank you for your job application")


@pytest.mark.asyncio
async def test_a_click_that_finishes_a_verified_submit_records_the_submission(store, tmp_path):
    page = tmp_path / "adt.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.auto_submitted = True
        ctx.last_snapshot = snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if i.get("label") == "VERIFY")
        out, _ = await d.t_click(ref, "VERIFY")
        assert out.get("status") == "submitted", out
        assert ctx.submitted and ctx.submitted.get("status") == "submitted"
        assert db.connect().execute("SELECT 1 FROM events WHERE kind='submit:submitted'").fetchone()
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_click_before_any_submit_records_nothing(store, tmp_path):
    page = tmp_path / "adt2.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if i.get("label") == "VERIFY")
        out, _ = await d.t_click(ref, "VERIFY")
        assert out.get("status") != "submitted" and not ctx.submitted, out
    finally:
        await ex.close(close_browser=True)
