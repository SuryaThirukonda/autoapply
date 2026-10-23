"""Cisco (Workday, 2026-10-23): "Which post-secondary education opportunity are you currently pursuing?" got 'None of the
Above' ticked from education.completed_college_program's No beside his Bachelor's; Workday cleared it again, the record kept
it, and the draft was held on the read-back. A No answer ticks a group's None box only when no other option is his."""
import pytest

from goat import answers
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.judge import JUDGE_INSTRUCTIONS
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px">
<fieldset><legend>Which post-secondary education opportunity are you currently pursuing?</legend>
  <label><input type="checkbox" id="as"> Associate's Degree (AA/AS/etc)</label>
  <label><input type="checkbox" id="ba" BA> Bachelor's Degree (BA/BS /etc)</label>
  <label><input type="checkbox" id="hs"> High School Diploma/GED</label>
  <label><input type="checkbox" id="no"> None of the Above</label></fieldset>
</body></html>"""
FIRMS = """<!doctype html><html><body style="margin:20px">
<fieldset><legend>Have you worked for any of these firms?</legend>
  <label><input type="checkbox" id="gt"> Grant Thornton</label>
  <label><input type="checkbox" id="dt"> Deloitte and Touche</label>
  <label><input type="checkbox" id="no"> None of the above</label></fieldset>
</body></html>"""


async def _open(tmp_path, html, name):
    page = tmp_path / name
    page.write_text(html, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    d, ctx = _dispatcher(_job())
    ctx.ex = ex
    ctx.last_snapshot = await snapshot.take(ex)
    refs = {o["label"].strip()[:5]: o["ref"] for g in ctx.last_snapshot.groups for o in g["options"]}
    return ex, d, refs


async def _checked(ex, dom_id):
    return await ex.page.evaluate(f"() => document.getElementById('{dom_id}').checked")


@pytest.mark.asyncio
async def test_none_of_the_above_is_refused_beside_a_ticked_answer(store, tmp_path):
    answers.save_answer("education.completed_college_program", "No", answer_type="choice", source="derived")
    ex, d, refs = await _open(tmp_path, PAGE.replace(" BA>", " checked>"), "pursuing.html")
    try:
        out, _ = await d.t_fill_batch([{"ref": refs["None "], "answer_key": "education.completed_college_program", "value": None,
                                        "source": None, "label": "None of the Above"}])
        r = out["results"][0]
        assert r["status"] == "failed" and "Bachelor's Degree" in r["error"], r
        assert await _checked(ex, "no") is False
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_none_of_the_above_is_refused_beside_an_option_the_same_batch_ticks(store, tmp_path):
    answers.save_answer("education.completed_college_program", "No", answer_type="choice", source="derived")
    answers.save_answer("education.enrolled", "Yes", answer_type="choice", source="questionnaire")
    ex, d, refs = await _open(tmp_path, PAGE.replace(" BA>", ">"), "pursuing2.html")
    try:
        out, _ = await d.t_fill_batch([
            {"ref": refs["None "], "answer_key": "education.completed_college_program", "value": None, "source": None, "label": "None"},
            {"ref": refs["Bache"], "answer_key": "education.enrolled", "value": "true", "source": "part", "label": "Bachelor's"}])
        st = {r["ref"]: r["status"] for r in out["results"]}
        assert st[refs["None "]] == "failed"
        assert await _checked(ex, "no") is False
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_no_answer_still_ticks_none_when_nothing_else_is_his(store, tmp_path):
    """American Century (2026-10-19): the firms question answered No ticks its own 'None of the above'."""
    answers.save_answer("q.worked_for_these_firms", "No", answer_type="choice", source="questionnaire")
    ex, d, refs = await _open(tmp_path, FIRMS, "firms.html")
    try:
        out, _ = await d.t_fill_batch([{"ref": refs["None "], "answer_key": "q.worked_for_these_firms", "value": None,
                                        "source": None, "label": "None of the above"}])
        assert out["results"][0]["status"] == "set", out
        assert await _checked(ex, "no") is True
    finally:
        await ex.close(close_browser=True)


def test_the_judge_knows_a_pursuing_question_takes_only_his_bachelors():
    assert "CURRENTLY PURSUING takes only his Bachelor's" in JUDGE_INSTRUCTIONS
