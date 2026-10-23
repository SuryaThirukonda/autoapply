"""Westinghouse and Smith+Nephew (2026-10-23).

- The park check filled the new employment row the run had added for his Rathinam Group entry with Southwest Airlines and
  his internship start date: a label the page repeats belongs to one row of a repeated section and is never filled by a
  lookup of its words.
- 'Date Obtained 1' in the citizenship block was filled from his date of birth with nothing saying why; the judge refused
  it. The date now comes from compliance.citizenship_obtained_date, whose note says he is a citizen by birth.
- Smith+Nephew's postings that take expressions of interest until they formally open were held as eligibility."""
import pytest

from goat import answers, db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

ROWS = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <h3>Previous Employment</h3>
  <label for="c1">* Company Name</label><input id="c1" type="text" required value="Revio"><br>
  <label for="t1">* Title</label><input id="t1" type="text" required value="Founding Software Engineer"><br>
  <label for="c2">* Company Name</label><input id="c2" type="text" required><br>
  <label for="t2">* Title</label><input id="t2" type="text" required><br>
  <label for="sig">* Signature</label><input id="sig" type="text" required><br>
  <button type="submit">Apply</button>
</form></body></html>"""

CITIZEN = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <label for="cn">* Country 1 Name</label><input id="cn" type="text" required value="United States"><br>
  <label for="ts">* Type of Status 1</label><input id="ts" type="text" required value="Citizen"><br>
  <label for="do">* Date Obtained 1</label><input id="do" type="text" required placeholder="MM/DD/YYYY"><br>
  <button type="submit">Apply</button>
</form></body></html>"""


async def _open(tmp_path, html, name):
    page = tmp_path / name
    page.write_text(html, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_a_park_never_fills_a_row_of_a_repeated_section(store, tmp_path):
    answers.save_answer("experience.work.5.company", "Southwest Airlines", answer_type="text", source="questionnaire",
                        aliases=["Company Name"])
    answers.save_answer("bank.signature", "Surya Thirukonda", answer_type="text", source="seed:answer_bank.json",
                        aliases=["Signature"])
    ex = await _open(tmp_path, ROWS, "rows.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        await d.t_park("needs_you: the second employment row and the signature are empty")
        vals = await ex.page.evaluate("() => ['c2', 't2', 'sig'].map(i => document.getElementById(i).value)")
        assert vals == ["", "", "Surya Thirukonda"], vals
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_the_citizenship_date_carries_its_reason(store, tmp_path):
    answers.save_answer("compliance.date_of_birth", "01/02/2003", answer_type="text", source="questionnaire")
    ex = await _open(tmp_path, CITIZEN, "citizen.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if "Date Obtained" in (i.get("label") or ""))
        out, _ = await d.t_lookup_answer("Date Obtained 1", [], None, ref)
        assert out["key"] == "compliance.citizenship_obtained_date" and out["answer"] == "01/02/2003", out
        res, _ = await d.t_fill_batch([{"ref": ref, "answer_key": "compliance.date_of_birth", "value": None, "source": None,
                                        "label": "Date Obtained 1"}])
        assert res["results"][0]["status"] == "set", res
        src = db.connect().execute("SELECT source FROM decisions WHERE ref=? ORDER BY id DESC LIMIT 1", (ref,)).fetchone()[0]
        assert src == "standing:compliance.citizenship_obtained_date"
        assert "citizen by birth" in answers.get("compliance.citizenship_obtained_date").note
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_expression_of_interest_posting_is_applied_to(store):
    d, ctx = _dispatcher(_job())

    async def page_text(n=0):
        return "Intern Embedded Software Engineering Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: The posting states that the position is not currently available for hiring and is only "
                            "accepting expressions of interest until it formally opens.")
    assert "expression of interest" in out["error"], out
