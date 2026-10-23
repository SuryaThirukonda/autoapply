"""L3Harris (SuccessFactors, 2026-10-23): on a long form the run filled one or two required fields between parks and gave up
with ten blockers left that his stored answers covered. The park check now fills those itself."""
import pytest

from goat import answers, db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

FELONY = ("Have you ever been convicted of fraud or any felony arising out of a contract with any agency of the U.S. "
          "Government?")
PAGE = f"""<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <label for="q1">* {FELONY}</label>
  <select id="q1" required><option value="">Select One</option><option>No</option><option>Yes</option></select><br><br>
  <label for="sig">* Signature</label><input id="sig" type="text" required><br><br>
  <label for="other">* Favorite flavor of ice cream</label><input id="other" type="text" required><br><br>
  <button type="submit">Submit Application</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_a_park_fills_the_blockers_his_stored_answers_cover(store, tmp_path):
    answers.save_answer("compliance.criminal_history", "No", answer_type="choice", source="questionnaire", aliases=[FELONY])
    answers.save_answer("bank.signature", "Surya Thirukonda", answer_type="text", source="seed:answer_bank.json",
                        aliases=["Signature"])
    page = tmp_path / "long.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        out, _ = await d.t_park("needs_you: the long form still has required compliance and signature fields")
        assert "the harness filled 2 required field(s)" in out["error"], out
        vals = await ex.page.evaluate("() => [document.getElementById('q1').value, document.getElementById('sig').value, "
                                      "document.getElementById('other').value]")
        assert vals == ["No", "Surya Thirukonda", ""]
        kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=?", (ctx.job_id,))]
        assert "park:autofill" in kinds
    finally:
        await ex.close(close_browser=True)
