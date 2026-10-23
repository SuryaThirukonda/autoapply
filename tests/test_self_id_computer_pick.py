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
