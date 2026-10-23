"""Auto-Owners Insurance (Workday, 2026-10-23): the Application Questions page's file box for 'Please upload unofficial
transcripts from your most recently completed level of education' had no label in the snapshot; the run uploaded the
résumé into it, and a transcript added later sat beside the résumé, so the judge held the draft."""
import pytest

from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <div data-automation-id="formField-transcript">
    <div data-automation-id="formLabel">Please upload unofficial transcripts from your most recently completed level of education</div>
    <div><button type="button">Select files</button><input id="tr" type="file" style="display:none"></div>
  </div>
  <div data-automation-id="formField-resume">
    <div data-automation-id="formLabel">Resume/CV</div>
    <div><button type="button">Select files</button><input id="cv" type="file" style="display:none"></div>
  </div>
  <button type="submit">Submit</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_the_resume_never_goes_into_a_transcript_slot(store, tmp_path):
    page = tmp_path / "ao.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        files = [i for i in snap.items if i.get("type") == "file"]
        assert len(files) == 2, snap.items
        out, _ = await d.t_upload_resume(files[0]["ref"], "resume")
        assert "transcript" in (out or {}).get("error", ""), out
        assert await ex.page.evaluate("() => document.getElementById('tr').files.length") == 0
        out, _ = await d.t_upload_resume(files[1]["ref"], "resume")
        assert out.get("status") == "set", out
    finally:
        await ex.close(close_browser=True)
