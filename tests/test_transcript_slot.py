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


GH_FILES = """<!doctype html><html><body style="margin:20px"><form>
<div class="file-upload"><div id="upload-label-question_1" class="upload-label">Please upload a writing sample (a class paper or report).*</div>
  <label for="question_1">Attach</label><input id="question_1" name="question_1" type="file" style="display:none">
  <p>Accepted file types: pdf, doc, docx, txt, rtf</p></div>
<div class="file-upload"><div id="upload-label-question_2" class="upload-label">Please upload a copy of an unofficial undergraduate transcript.*</div>
  <label for="question_2">Attach</label><input id="question_2" name="question_2" type="file" style="display:none">
  <p>Accepted file types: pdf, doc, docx, txt, rtf</p></div>
</form></body></html>"""


@pytest.mark.asyncio
async def test_greenhouse_file_boxes_carry_their_question_and_the_transcript_finds_its_own(store, tmp_path):
    """Appian (Greenhouse, 2026-10-24): both file boxes read 'question_N (Attach)'; the transcript went into the writing-sample
    box and the transcript box stayed empty."""
    page = tmp_path / "appian.html"
    page.write_text(GH_FILES, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        files = [i for i in snap.items if i.get("type") == "file"]
        assert "writing sample" in files[0]["label"] and "transcript" in files[1]["label"], [f["label"] for f in files]
        out, _ = await d.t_upload_resume(files[0]["ref"], "transcript")
        assert "transcript" in (out or {}).get("error", "") and files[1]["ref"] in out["error"], out
        out, _ = await d.t_upload_resume(files[1]["ref"], "transcript")
        assert "this box asks" not in str(out), out       # the transcript box itself is never refused
    finally:
        await ex.close(close_browser=True)
