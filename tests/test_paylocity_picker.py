"""Paylocity's pcty-input-select pickers (Transcard, North Atlantic Industries, 2026-10-22): every work-history State came
back 'the picker for this field did not open' (its rows are role=listitem, which the generic list read does not know),
a typed 'Texas' answers 'No Results Found' (the rows are abbreviations), and Country renders only its first 57 rows."""
from pathlib import Path

import pytest

from goat import db
from goat.browser import fill
from goat.browser.executor import Executor, HostPolicy

FIX = Path(__file__).parent / "fixtures" / "paylocity" / "picker.html"
STATE = "public-candidate-work-history-address-0-us-state"
COUNTRY = "public-candidate-work-history-address-0-country"


async def _open(tmp_path) -> Executor:
    db.reset_for_tests(tmp_path / "t.db")
    url = FIX.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    await ex.page.goto(url)
    await ex.settle(quiet_ms=100)
    return ex


async def _ref(ex, sel: str) -> str:
    return await ex.page.locator(sel).first.get_attribute("data-goat-ref")


async def _shown(ex, pid: str) -> str:
    return (await ex.page.inner_text(f'[id="{pid}-select-wrapper"] .input-select-input-single-value')).strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["wrapper", "input"])
async def test_a_state_list_of_abbreviations_takes_his_state(tmp_path, target):
    ex = await _open(tmp_path)
    try:
        sel = f'[id="{STATE}-select-wrapper"]' if target == "wrapper" else f'[id="{STATE}"]'
        r = await fill.choose(ex, await _ref(ex, sel), ["Texas"])
        assert r.ok and r.readback == "TX", (r.ok, r.readback, r.error)
        assert await _shown(ex, STATE) == "TX"
        assert await ex.page.locator(f'[id="{STATE}-dropdown-list-container"]').count() == 0
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_country_beyond_the_rendered_rows_is_found_by_typing_and_picked_exactly(tmp_path):
    ex = await _open(tmp_path)
    try:
        ref = await _ref(ex, f'[id="{COUNTRY}-select-wrapper"]')
        r = await fill.choose(ex, ref, ["United States", "United States of America"])
        assert r.ok and r.readback == "United States", (r.ok, r.readback, r.error)
        assert await _shown(ex, COUNTRY) == "United States"
        # already showing it: nothing is opened again
        r = await fill.choose(ex, ref, ["United States of America", "United States"])
        assert r.ok and r.readback == "United States"
        assert await ex.page.locator(f'[id="{COUNTRY}-dropdown-list-container"]').count() == 0
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_wording_the_list_lacks_is_reported_with_what_it_offers(tmp_path):
    ex = await _open(tmp_path)
    try:
        r = await fill.choose(ex, await _ref(ex, f'[id="{STATE}-select-wrapper"]'), ["Ontario"])
        assert not r.ok and "the list offers" in r.error and "AL" in r.error, r.error
        assert await _shown(ex, STATE) in ("Select a state", "")
    finally:
        await ex.close(close_browser=True)


def test_state_names_and_codes_stand_for_each_other():
    assert fill.with_state_codes(["Texas"]) == ["Texas", "TX"]
    assert fill.with_state_codes(["TX", "Texas"]) == ["TX", "Texas"]
    assert fill.with_state_codes(["United States"]) == ["United States"]


HISTORY_ADDRESS = """<html><body><h3>Work History</h3>
<label for="workHistory.companyName.0">Company Name (required)</label><input id="workHistory.companyName.0" required>
<label for="public-candidate-work-history-address-0-address-1">Address Line 1</label><input id="public-candidate-work-history-address-0-address-1" required>
<label for="public-candidate-work-history-address-0-city">City</label><input id="public-candidate-work-history-address-0-city" required>
<label for="public-candidate-work-history-address-0-zip">Zip Code</label><input id="public-candidate-work-history-address-0-zip" required>
<h3>Address</h3>
<label for="public-site-address-zip">Zip Code</label><input id="public-site-address-zip" required>
</body></html>"""


@pytest.mark.asyncio
async def test_a_history_address_paylocity_never_enforces_is_not_an_empty_required_field(tmp_path):
    """Transcard (2026-10-22): Next Step took a work-history entry with its address empty, but the outline listed Address
    Line 1 / City / Zip as empty required fields and the run held over 'unsourced required work-history fields'. His own
    home address stays required."""
    from goat.browser import snapshot
    db.reset_for_tests(tmp_path / "t.db")
    ex = Executor(policy=HostPolicy(["about:blank"]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page()
        await ex.page.set_content(HISTORY_ADDRESS)
        snap = await snapshot.take(ex)
        names = {e.get("name") for e in snap.errors.get("empty_required", [])}
        assert names == {"workHistory.companyName.0", "public-site-address-zip"}, names
    finally:
        await ex.close(close_browser=True)
