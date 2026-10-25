"""BNY (Oracle, 2026-10-25): 'Total compensation expectations' is a list of bands; choose was given the band
'40,001 - 50,000' by the model but searched the list for his stored '41600', matched nothing, and left the typed text."""
import pytest

from goat import answers
from goat.browser.fill import FieldResult
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures


@pytest.mark.asyncio
async def test_a_band_holding_his_figure_is_chosen_before_the_figure(store, monkeypatch):
    answers.save_answer("compensation.desired_annual", "41600", answer_type="text", source="questionnaire")
    d, ctx = _dispatcher(_job("BNY", "B1"))
    tried = []

    async def fake_choose(ex, ref, wants, **k):
        tried.append(list(wants))
        return FieldResult(ref, True, wants[0], "")
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "choose", fake_choose)
    out, _ = await d._t_choose("e93", "compensation.desired_annual", ["40,001 - 50,000", "50,001 - 60,000"],
                               "Total compensation expectations", None)
    assert tried and tried[0][0] == "40,001 - 50,000", tried
    assert "50,001 - 60,000" not in tried[0], tried
