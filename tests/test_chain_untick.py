"""Cisco (Workday, 2026-10-23): 'Other Diploma/Degree/Certification' in "Which post-secondary education opportunity are you
currently pursuing?" was ticked by the consent pass; every attempt to clear it under education.degree_level_picker was
refused as a tick ("the stored answer is an ordered preference ... tick only that option"), and the judge failed the
draft. Clearing an option his chain does not pick is allowed; ticking one still is not."""
import pytest

from goat import answers
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px">
<fieldset><legend>Which post-secondary education opportunity are you currently pursuing?</legend>
  <label><input type="checkbox" id="as"> Associate's Degree (AA/AS/etc)</label>
  <label><input type="checkbox" id="ba" checked> Bachelor's Degree (BA/BS /etc)</label>
  <label><input type="checkbox" id="ot" checked> Other Diploma/Degree/Certification</label>
  <label><input type="checkbox" id="na"> None of the Above</label></fieldset>
</body></html>"""
KEY = "education.degree_level_picker"


async def _open(tmp_path):
    page = tmp_path / "pursuing.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


async def _setup(tmp_path):
    answers.save_answer(KEY, "Bachelor's Degree; Bachelor of Science; Bachelors", answer_type="choice_chain", source="questionnaire")
    ex = await _open(tmp_path)
    d, ctx = _dispatcher(_job())
    ctx.ex = ex
    ctx.last_snapshot = await snapshot.take(ex)
    refs = {o["label"].strip()[:5]: o["ref"] for g in ctx.last_snapshot.groups for o in g["options"]}
    return ex, d, refs


async def _checked(ex, dom_id):
    return await ex.page.evaluate(f"() => document.getElementById('{dom_id}').checked")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [{"value": "false", "source": "part"}, {"value": None, "source": "clear"}])
async def test_an_option_his_chain_does_not_pick_can_be_cleared(store, tmp_path, field):
    ex, d, refs = await _setup(tmp_path)
    try:
        out, _ = await d.t_fill_batch([{"ref": refs["Other"], "answer_key": KEY, "label": "Other Diploma/Degree/Certification", **field}])
        assert out["results"][0]["status"] == "set", out
        assert await _checked(ex, "ot") is False and await _checked(ex, "ba") is True
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_ticking_an_option_his_chain_does_not_pick_is_still_refused(store, tmp_path):
    ex, d, refs = await _setup(tmp_path)
    try:
        out, _ = await d.t_fill_batch([{"ref": refs["Assoc"], "answer_key": KEY, "value": None, "source": None, "label": "Associate's"}])
        assert out["results"][0]["status"] == "failed" and "Bachelor's Degree" in out["results"][0]["error"]
        assert await _checked(ex, "as") is False
    finally:
        await ex.close(close_browser=True)


DISABILITY = """<!doctype html><html><body style="margin:20px">
<h2>Voluntary Self-Identification of Disability</h2>
<div><input type="checkbox" id="y"><label for="y">Yes, I have a disability, or have had one in the past</label></div>
<div><input type="checkbox" id="n"><label for="n">No, I do not have a disability and have not had one in the past</label></div>
<div><input type="checkbox" id="d"><label for="d">I do not want to answer</label></div>
</body></html>"""


@pytest.mark.asyncio
async def test_his_disability_answer_ticks_no_and_never_the_decline_fallback(store, tmp_path):
    """Micron (Workday, 2026-10-23): self_id.disability is stored as 'choice' (delegation.yaml), so the ordered-preference
    rule never ran; 'I do not want to answer' was ticked beside 'No, I do not have a disability ...' and the decline went in."""
    answers.save_answer("self_id.disability", "No, I do not have a disability; No, I do not have a disability and have not had "
                        "one in the past; No; I do not want to answer; I do not wish to answer", answer_type="choice",
                        source="seed:delegation.yaml")
    page = tmp_path / "disability.html"
    page.write_text(DISABILITY, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        refs = {i["label"].strip()[:3]: i["ref"] for i in ctx.last_snapshot.items if i.get("type") == "checkbox"}
        key = "self_id.disability"
        out, _ = await d.t_fill_batch([{"ref": refs[r], "answer_key": key, "value": None, "source": None, "label": r}
                                       for r in ("Yes", "No,", "I d")])
        st = {r["ref"]: r for r in out["results"]}
        assert st[refs["No,"]]["status"] == "set", out
        assert st[refs["I d"]]["status"] == "failed" and "No, I do not have a disability" in st[refs["I d"]]["error"], out
        assert await _checked(ex, "n") is True and await _checked(ex, "d") is False and await _checked(ex, "y") is False
        # the choose tool hands a box to fill_batch: the decline is refused there too
        out, _ = await d.t_choose(refs["I d"], key, ["I do not want to answer"], "Disability", "answer_key")
        assert out["results"][0]["status"] == "failed", out
        assert await _checked(ex, "d") is False
    finally:
        await ex.close(close_browser=True)
