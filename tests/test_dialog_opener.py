"""SuccessFactors' Create Account page (Westinghouse, 2026-10-22): 'Read and accept the data privacy acknowledgment statement'
is a link that opens a dialog (aria-haspopup="dialog"). The snapshot called every aria-haspopup element a dropdown, the
click tool refused it as a choice control, and account creation parked as credentials_unknown."""
from pathlib import Path

import pytest

from goat import db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

FIX = Path(__file__).parent / "fixtures" / "successfactors" / "privacy.html"


@pytest.mark.asyncio
async def test_a_dialog_opener_is_a_button_and_a_list_opener_a_dropdown(tmp_path):
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
        by_id = {}
        for i in snap.items:
            dom_id = await ex.page.evaluate("(r) => { const e = document.querySelector('[data-goat-ref=\"' + r + '\"]'); return e ? e.id : ''; }", i["ref"])
            by_id[dom_id] = i
        assert by_id["dataPrivacyId"]["tag"] != "dropdown" and by_id["dataPrivacyId"]["role"] == "button", by_id["dataPrivacyId"]
        assert by_id["country"]["tag"] == "dropdown", by_id["country"]
    finally:
        await ex.close(close_browser=True)
