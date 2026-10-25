"""DTCC (2026-10-25): its salary-range radios read '$0 to $50,000.00', '$50,001.00 to $75,000.00', ... The band parser took
'1.00' out of '$50,001.00' as a band's low end, so his $41,600 'fit' every band below $100,000; and a radio whose band holds
his figure was refused as no match for '41600'."""
import pytest

from goat import answers
from goat.browser import fill
from tests.test_answer_rules import _dispatcher, store  # noqa: F401 - fixtures

DTCC = ["$0 to $50,000.00", "$50,001.00 to $75,000.00", "$75,001.00 to $100,000.00", "$100,001.00 to $125,000.00",
        "$225,001.00 plus"]


def test_bands_with_cents_hold_only_his_figure():
    held = [b for b in DTCC if fill.money_band_holds(b, ["41600"])]
    assert held == ["$0 to $50,000.00"]
    assert fill._money_bucket("$50,001.00 to $75,000.00") == (50001.0, 75000.0)
    assert fill._money_bucket("$225,001.00 plus") == (225001.0, float("inf"))
    # the bands he has already sent keep reading the same
    assert fill.money_band_holds("$40,000 - $50,000 ($19.23 - $24.04)", ["41600"])
    assert fill.money_band_holds("$40,000 - $49,999", ["41600"])
    assert not fill.money_band_holds("$20/hr", ["41600"])


@pytest.mark.asyncio
async def test_a_salary_radio_whose_band_holds_his_figure_is_ticked(store, monkeypatch):
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 26, "company": "DTCC", "ats": "oracle"})
    g = "What are your annual salary range expectations?"
    items = [{"ref": f"e{4 + n}", "tag": "input", "type": "radio", "label": lab, "group": g, "checked": False} for n, lab in enumerate(DTCC)]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    ticked = []

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        ticked.extend(p["ref"] for p in plan)
        return [FieldResult(p["ref"], True, "checked", "") for p in plan]
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("compensation.desired_annual", "41600", answer_type="text", source="questionnaire")
    out, _ = await d.t_fill_batch([{"ref": "e4", "answer_key": "compensation.desired_annual", "value": None, "source": None, "label": g}])
    assert ticked == ["e4"], out
    out, _ = await d.t_fill_batch([{"ref": "e5", "answer_key": "compensation.desired_annual", "value": None, "source": None, "label": g}])
    assert "e5" not in ticked, out
