"""Palantir (Lever, 2026-10-23): 'All offered locations; ...' ticked every office in two groups that allow 1-3 picks, and
the run's unticks came back ticked; the judge held the draft."""
import pytest

from goat import answers
from goat.agent.tools import selection_limit
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

OFFICES = ["New York, NY", "Washington, DC", "Palo Alto, CA", "Seattle, WA", "Denver, CO"]
PAGE = ("""<!doctype html><html><body style="margin:20px"><form><fieldset><legend>Preferred office locations (select 1-3)</legend>"""
        + "".join(f'<label><input type="checkbox" id="o{i}"> {o}</label>' for i, o in enumerate(OFFICES))
        + "</fieldset></form></body></html>")
RANKING = ("All offered locations; Dallas; Houston; Austin; New York; New York, NY; Los Angeles; Chicago; San Francisco; Seattle; "
           "Boston; Washington, DC; Atlanta; Denver")


def test_selection_limits_are_read():
    assert selection_limit("Preferred office locations (select 1-3)") == 3
    assert selection_limit("Choose up to two offices") == 2
    assert selection_limit("Which offices would you consider?") == 0


@pytest.mark.asyncio
async def test_a_capped_office_group_takes_his_top_ranked_picks_and_unticks_stay_unticked(store, tmp_path):
    answers.save_answer("availability.location_ranking", RANKING, answer_type="choice_chain", source="questionnaire")
    page = tmp_path / "pal.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        refs = {o["label"].strip(): o["ref"] for g in snap.groups for o in g["options"]}
        out, _ = await d.t_fill_batch([{"ref": refs["Denver, CO"], "answer_key": "availability.location_ranking", "value": "true",
                                        "source": "answer_key", "label": "Denver"}])
        r = out["results"][0]
        assert r["status"] == "failed" and "at most 3" in r["error"] and "New York" in r["error"], r
        out, _ = await d.t_fill_batch([{"ref": refs[o], "answer_key": "availability.location_ranking", "value": "true",
                                        "source": "answer_key", "label": o} for o in ("New York, NY", "Seattle, WA", "Washington, DC")])
        assert [x["status"] for x in out["results"]] == ["set"] * 3, out
        await ex.page.check("#o4")                                     # a box ticked earlier
        out, _ = await d.t_fill_batch([{"ref": refs["Denver, CO"], "answer_key": "availability.location_ranking", "value": "false",
                                        "source": "answer_key", "label": "Denver"}])
        assert not await ex.page.is_checked("#o4"), out
        assert [await ex.page.is_checked(f"#o{i}") for i in range(5)] == [True, True, False, True, False]
    finally:
        await ex.close(close_browser=True)
