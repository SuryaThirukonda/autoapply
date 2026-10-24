"""SharkNinja (Greenhouse, 2026-10-23): with Texas A&M picked as his school, the required 'If you selected other, please
specify which one' box went to Surya as a question; it takes N/A when nothing on the page was answered 'Other'."""
import pytest

from goat import records
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px"><form>
<label for="sc">School *</label><input id="sc" type="text" value="Texas A&M University">
<label for="ot">If you selected other, please specify which one *</label><input id="ot" type="text" required>
</form></body></html>"""


async def _page(tmp_path, name):
    page = tmp_path / name
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_an_if_other_box_takes_na_when_nothing_was_other(store, tmp_path):
    ex = await _page(tmp_path, "sn.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        records.record_decision(ctx.job_id, ctx.page_no, "School", "Texas A&M University", "standing:education.school", "e1")
        ref = next(i["ref"] for i in snap.items if "selected other" in (i.get("label") or ""))
        out, _ = await d.t_fill_batch([{"ref": ref, "answer_key": None, "value": "N/A", "source": "generated", "label": "If other"}])
        assert out["results"][0]["status"] == "set", out
        assert await ex.page.evaluate("() => document.getElementById('ot').value") == "N/A"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_if_other_box_is_no_na_when_other_was_picked(store, tmp_path):
    ex = await _page(tmp_path, "sn2.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        records.record_decision(ctx.job_id, ctx.page_no, "How did you hear about us?", "Other", "standing:how_did_you_hear.preferred_option", "e1")
        ref = next(i["ref"] for i in snap.items if "selected other" in (i.get("label") or ""))
        out, _ = await d.t_fill_batch([{"ref": ref, "answer_key": None, "value": "N/A", "source": "generated", "label": "If other"}])
        assert out["results"][0]["status"] != "set", out
    finally:
        await ex.close(close_browser=True)
