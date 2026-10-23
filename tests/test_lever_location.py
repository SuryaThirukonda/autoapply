"""Lever's 'Current location' (Palantir, 2026-10-22): only a place picked from Lever's own search counts. Typing 'Sugar Land,
TX, United States' found nothing, Lever emptied the box, fill_batch and choose both failed and the gate refused the page."""
import json
from pathlib import Path

import pytest

from goat import db
from goat.browser import fill
from goat.browser.executor import Executor, HostPolicy

FIX = Path(__file__).parent / "fixtures" / "lever" / "location.html"


async def _open(tmp_path) -> Executor:
    db.reset_for_tests(tmp_path / "t.db")
    url = FIX.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    await ex.page.goto(url)
    await ex.settle(quiet_ms=100)
    from goat.browser import snapshot
    await snapshot.take(ex)            # tags the controls with data-goat-ref
    return ex


async def _ref(ex, sel: str) -> str:
    return await ex.page.locator(sel).first.get_attribute("data-goat-ref")


def test_place_variants_spell_the_state_and_country_both_ways():
    v = fill.place_variants(["Sugar Land, Texas, United States"])
    assert {"Sugar Land, TX, USA", "Sugar Land, TX, United States", "Sugar Land, TX"} <= set(v)
    assert fill.place_variants(["Sugar Land"]) == ["Sugar Land"]


@pytest.mark.asyncio
async def test_choose_picks_his_city_from_levers_search(tmp_path):
    ex = await _open(tmp_path)
    try:
        r = await fill.choose(ex, await _ref(ex, "#location-input"),
                              ["Sugar Land, Texas, United States", "Sugar Land, TX, United States", "Sugar Land"])
        assert r.ok and r.readback == "Sugar Land, TX, USA", (r.ok, r.readback, r.error)
        assert json.loads(await ex.page.input_value("#selected-location"))["name"] == "Sugar Land, TX, USA"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_fill_batch_picks_the_place_and_types_the_other_boxes(tmp_path):
    ex = await _open(tmp_path)
    try:
        loc, name = await _ref(ex, "#location-input"), await _ref(ex, "#name")
        out = await fill.fill_batch(ex, [{"ref": name, "value": "Surya Thirukonda"},
                                         {"ref": loc, "value": "Sugar Land, TX, United States"}])
        assert [r.ref for r in out] == [name, loc]
        assert out[1].ok and out[1].readback == "Sugar Land, TX, USA", (out[1].ok, out[1].readback, out[1].error)
        assert out[0].ok and await ex.page.input_value("#name") == "Surya Thirukonda"
        assert await ex.page.input_value("#selected-location")
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_place_lever_does_not_list_is_reported_not_typed(tmp_path):
    ex = await _open(tmp_path)
    try:
        r = await fill.choose(ex, await _ref(ex, "#location-input"), ["Springfield, Illinois, United States"])
        assert not r.ok and "no match" in r.error
        assert await ex.page.input_value("#selected-location") == ""
    finally:
        await ex.close(close_browser=True)
