"""Epic Games (Greenhouse, 2026-10-23): the education entry's 'Start Date (Year)' (input educations[0].start_date.year)
was filled 2027 from bank.start-date, his internship start date, because the label alone matched that answer."""
import pytest

from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <div><label for="educations[0].start_date.year">Start Date (Year)*:</label>
    <input id="edy" name="educations[0].start_date.year" type="number" placeholder="YYYY"></div>
  <div><label for="avail">Start Date (Year)*:</label><input id="avail" name="questions.internship_start" type="text"></div>
  <button type="submit">Submit</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_an_education_start_year_takes_his_school_start_not_the_internship_start(store, tmp_path):
    from goat import answers
    answers.save_answer("bank.start-date", "05/18/2027", answer_type="short", source="surya_chat")
    answers.save_answer("education.start_year", "2025", answer_type="text", source="seed:standing_answers.json")
    answers.save_answer("education.start", "August 2025", answer_type="text", source="seed:standing_answers.json")
    page = tmp_path / "epic.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        boxes = [i for i in snap.items if i.get("tag") == "input"]
        assert boxes[0].get("name") == "educations[0].start_date.year" and not boxes[1].get("name")
        refs = {"educations[0].start_date.year": boxes[0]["ref"], "questions.internship_start": boxes[1]["ref"]}
        out, _ = await d.t_fill_batch([
            {"ref": refs["educations[0].start_date.year"], "answer_key": "bank.start-date", "value": None, "source": None,
             "label": "Start Date (Year)"},
            {"ref": refs["questions.internship_start"], "answer_key": "bank.start-date", "value": None, "source": None,
             "label": "Start Date (Year)"}])
        assert [r["status"] for r in out["results"]] == ["set", "set"], out
        assert await ex.page.evaluate("() => document.getElementById('edy').value") == "2025"
        assert await ex.page.evaluate("() => document.getElementById('avail').value") == "05/18/2027"
    finally:
        await ex.close(close_browser=True)
