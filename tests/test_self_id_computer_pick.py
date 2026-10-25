"""Badger Meter (Workday, 2026-10-23): the run ticked 'Asian' with the computer tool and recorded it with source 'computer';
the decline-first check skipped checkboxes, so whether the question offered a decline option was never looked at. A box
picked by hand is refused when the question offers one."""
import pytest

from goat import answers
from goat.browser import snapshot
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures
from tests.test_page_readback import RACE, RACE_NO_DECLINE, _race

CHAIN = "decline; I do not wish to answer; Prefer not to say; Asian; Asian (United States of America)"


async def _setup(tmp_path, html, name):
    answers.save_answer("self_id.race_ethnicity", CHAIN, answer_type="choice_chain", source="questionnaire")
    ex = await _race(tmp_path, html, name)
    d, ctx = _dispatcher(_job())
    ctx.ex = ex
    await ex.page.check("#as")                                  # what the computer tool did
    ctx.last_snapshot = await snapshot.take(ex)
    refs = {o["label"].strip()[:5]: o["ref"] for g in ctx.last_snapshot.groups for o in g["options"]}
    return ex, d, refs


@pytest.mark.asyncio
async def test_a_hand_picked_race_is_refused_when_a_decline_is_offered(store, tmp_path):
    ex, d, refs = await _setup(tmp_path, RACE, "race.html")
    try:
        out, _ = await d.t_fill_batch([{"ref": refs["Asian"], "answer_key": "self_id.race_ethnicity", "value": None,
                                        "source": "computer", "label": "Race"}])
        r = out["results"][0]
        assert r["status"] == "failed" and "I Do Not Wish to Answer" in r["error"], r
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_hand_picked_race_stands_where_no_decline_is_offered(store, tmp_path):
    ex, d, refs = await _setup(tmp_path, RACE_NO_DECLINE, "race2.html")
    try:
        out, _ = await d.t_fill_batch([{"ref": refs["Asian"], "answer_key": "self_id.race_ethnicity", "value": None,
                                        "source": "computer", "label": "Race"}])
        assert out["results"][0]["status"] == "kept", out
    finally:
        await ex.close(close_browser=True)


# ADT (Oracle, 2026-10-23): the races group had no decline option, but a separate 'I choose not to disclose my ethnicity'
# box sat beside it; 'Asian' was kept and went out.
RACE_SEPARATE_DECLINE = RACE_NO_DECLINE.replace("</fieldset>", "</fieldset>\n<div><label><input type=\"checkbox\" id=\"nd\"> I choose "
                                                              "not to disclose my ethnicity or race</label></div>")


@pytest.mark.asyncio
async def test_a_hand_picked_race_is_refused_when_a_separate_decline_box_is_offered(store, tmp_path):
    ex, d, refs = await _setup(tmp_path, RACE_SEPARATE_DECLINE, "race3.html")
    try:
        out, _ = await d.t_fill_batch([{"ref": refs["Asian"], "answer_key": "self_id.race_ethnicity", "value": None,
                                        "source": "computer", "label": "Race"}])
        r = out["results"][0]
        assert r["status"] == "failed" and "choose not to disclose" in r["error"], r
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_the_race_chain_ticks_a_separate_decline_box_not_his_fallback(store, tmp_path):
    answers.save_answer("self_id.race_ethnicity", CHAIN, answer_type="choice_chain", source="questionnaire")
    ex = await _race(tmp_path, RACE_SEPARATE_DECLINE, "race4.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        refs = {o["label"].strip()[:5]: o["ref"] for g in ctx.last_snapshot.groups for o in g["options"]}
        out, _ = await d.t_fill_batch([{"ref": refs["Asian"], "answer_key": "self_id.race_ethnicity", "value": None,
                                        "source": None, "label": "Race"}])
        r = out["results"][0]
        assert r["status"] == "failed" and "choose not to disclose" in r["error"], r
        assert not await ex.page.is_checked("#as")
        box = next(i for i in ctx.last_snapshot.items if "choose not to disclose" in (i.get("label") or ""))
        out, _ = await d.t_fill_batch([{"ref": box["ref"], "answer_key": "self_id.race_ethnicity", "value": None,
                                        "source": None, "label": "Race decline"}])
        assert out["results"][0]["status"] == "set", out
        assert await ex.page.is_checked("#nd")
    finally:
        await ex.close(close_browser=True)


PAY_BANDS = """<!doctype html><html><body style="margin:20px"><form>
<fieldset><legend>What is your desired salary range? (You may select a maximum of 2)</legend>
<label><input type="checkbox" id="b0"> Commission</label><br>
<label><input type="checkbox" id="b1"> $40,000 - $50,000 ($19.23 - $24.04)</label><br>
<label><input type="checkbox" id="b2"> $50,000 - $60,000 ($24.04 - $28.85)</label>
</fieldset></form></body></html>"""


@pytest.mark.asyncio
async def test_a_hand_ticked_pay_band_that_holds_his_figure_is_kept(store, tmp_path):
    """Northmarq (2026-10-24): '$40,000 - $50,000 ($19.23 - $24.04)' ticked with the computer tool for compensation.desired_annual
    (41600) was refused as not saying '41600', and the run asked Surya to tick it himself."""
    from goat.browser.executor import Executor, HostPolicy
    answers.save_answer("compensation.desired_annual", "41600", answer_type="text", source="questionnaire")
    page = tmp_path / "pay.html"
    page.write_text(PAY_BANDS, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        await ex.page.check("#b1")
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        ref = next(o["ref"] for g in ctx.last_snapshot.groups for o in g["options"] if "40,000" in o["label"])
        out, _ = await d.t_fill_batch([{"ref": ref, "answer_key": "compensation.desired_annual", "value": None,
                                        "source": "computer", "label": "Desired salary range"}])
        assert out["results"][0]["status"] == "kept", out
    finally:
        await ex.close(close_browser=True)
