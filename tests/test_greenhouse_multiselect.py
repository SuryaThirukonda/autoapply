"""Greenhouse's react-select multi-selects (Twitch, 2026-10-22): a picked country is a chip while the combobox input stays
empty. The page-error scan took the chip for the box's label, cut it from the shown text and reported the box empty, so
the gate refused every submit and the run parked as a portal error."""
from pathlib import Path

import pytest

from goat import db
from goat.browser import snapshot
from goat.browser.errors import scan_errors
from goat.browser.executor import Executor, HostPolicy

FIX = Path(__file__).parent / "fixtures" / "greenhouse" / "multiselect.html"


@pytest.mark.asyncio
async def test_a_picked_chip_is_a_value_and_an_empty_box_is_still_reported(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    url = FIX.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    try:
        await ex.page.goto(url)
        await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        scan = await scan_errors(ex)
        labels = [e["label"] for e in scan.get("empty_required", [])]
        assert not any("citizenship" in label for label in labels), labels
        assert any("sponsorship" in label for label in labels), labels
        item = next(i for i in snap.items if "citizenship" in (i.get("label") or ""))
        assert item.get("shown") == "United States of America (USA)"
    finally:
        await ex.close(close_browser=True)
