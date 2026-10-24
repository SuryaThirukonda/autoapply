"""HPE (Workday, 2026-10-23): 'indicate all locations or countries for which you ... require sponsorship ... If this does not
apply, indicate N/A.' got his yes/no 'No' from bank.sponsorship-now, and the judge held the draft."""
import pytest

from goat import answers
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px"><form>
<label for="sp">For those locations or countries in which you are seeking employment, indicate all locations or countries for
which you now, or in the future, require sponsorship for employment with HPE. If this does not apply, indicate N/A.</label>
<textarea id="sp"></textarea>
<label for="q2">Will you require sponsorship?</label><input id="q2" type="text">
</form></body></html>"""


@pytest.mark.asyncio
async def test_a_sponsorship_locations_box_takes_na(store, tmp_path):
    answers.save_answer("bank.sponsorship-now", "No", answer_type="choice", source="seed:answer_bank.json")
    page = tmp_path / "hpe.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        boxes = [i for i in snap.items if i.get("tag") in ("textarea", "input")]
        out, _ = await d.t_fill_batch([
            {"ref": boxes[0]["ref"], "answer_key": "bank.sponsorship-now", "value": None, "source": None, "label": "Sponsorship locations"},
            {"ref": boxes[1]["ref"], "answer_key": "bank.sponsorship-now", "value": None, "source": None, "label": "Sponsorship"}])
        assert [r["status"] for r in out["results"]] == ["set", "set"], out
        assert await ex.page.evaluate("() => document.getElementById('sp').value") == "N/A"
        assert await ex.page.evaluate("() => document.getElementById('q2').value") == "No"
    finally:
        await ex.close(close_browser=True)
