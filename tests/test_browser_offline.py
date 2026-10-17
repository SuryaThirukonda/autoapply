"""Offline browser tests against a local fixture form using Playwright's bundled Chromium.
Exercises snapshot refs, fill_batch read-back, the error scan, the submit guard, and submit()."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from goat import db
from goat.browser import fill, snapshot, submit as submit_mod
from goat.browser.executor import ClickRefused, Executor, HostPolicy

FIXTURE = (Path(__file__).parent / "fixtures" / "form.html").resolve().as_uri()


@pytest.fixture()
def tmpdb(tmp_path):
    return db.reset_for_tests(tmp_path / "t.db")


async def _open() -> Executor:
    ex = Executor(policy=HostPolicy([FIXTURE]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")  # file:// has an empty host
    await ex.page.goto(FIXTURE)
    await ex.settle(quiet_ms=100)
    return ex


def _by_label(snap, needle):
    for i in snap.items:
        if needle.lower() in (i.get("label") or "").lower():
            return i
    for g in snap.groups:
        if needle.lower() in (g.get("label") or "").lower():
            return g
    return None


@pytest.mark.asyncio
async def test_snapshot_fill_errors_submit(tmpdb):
    ex = await _open()
    try:
        snap = await snapshot.take(ex)
        text = snapshot.render(snap)
        assert "First Name" in text and "SUBMIT-CONTROL" in text
        assert "empty_required" in text  # required fields are empty at first
        first = _by_label(snap, "First Name")
        email = _by_label(snap, "Email")
        loc = _by_label(snap, "Current location")
        school = _by_label(snap, "School")
        auth = _by_label(snap, "legally authorized")
        cert = _by_label(snap, "certify")
        why = _by_label(snap, "Why Acme")
        assert first and email and loc and school and auth and cert and why
        yes_ref = next(o["ref"] for o in auth["options"] if o["label"].strip().lower().startswith("yes"))

        # A click on the submit control from the "model" is refused.
        sub = next(i for i in snap.items if i.get("submitish"))
        with pytest.raises(ClickRefused):
            await ex.click_xy(sub["bbox"][0], sub["bbox"][1])

        res = await fill.fill_batch(ex, [
            {"ref": first["ref"], "value": "Surya"},
            {"ref": email["ref"], "value": "applicant@example.com"},
            {"ref": loc["ref"], "value": "Sugar Land"},           # wrong shape -> page error appears on blur
            {"ref": school["ref"], "value": "Texas A&M University - College Station"},
            {"ref": yes_ref, "value": True},
            {"ref": why["ref"], "value": "Because of the backend work."},
            {"ref": cert["ref"], "value": True},
        ])
        by = {r.ref: r for r in res}
        assert by[first["ref"]].ok and by[first["ref"]].readback == "Surya"
        assert by[school["ref"]].ok and "Texas A" in by[school["ref"]].readback
        assert by[yes_ref].ok and by[yes_ref].readback == "checked"

        # The Lever-style location error is now on the page, and submit() must refuse to click.
        snap2 = await snapshot.take(ex)
        assert any("No location found" in (a.get("text") or "") for a in snap2.errors["error_text"] + snap2.errors["alerts"])
        r = await submit_mod.submit(ex, None, wait_s=2)
        assert r.status == "blocked" and any("No location found" in w for w in r.why)
        assert await ex.eval("() => window.__submits") == 0

        # Fix the location and the last name, attach a file, then submit succeeds with a positive signal.
        last = _by_label(snap2, "Last Name")
        await fill.fill_batch(ex, [{"ref": loc["ref"], "value": "Sugar Land, TX, USA"}, {"ref": last["ref"], "value": "Thirukonda"}])
        resume = _by_label(snap2, "Resume")
        pdf = Path(__file__).parent / "fixtures" / "Surya_Thirukonda_Resume_SWE.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        up = await fill.upload_file(ex, resume["ref"], pdf)
        assert up.ok, up.error
        snap3 = await snapshot.take(ex)
        assert snap3.errors["clean"], snap3.errors
        r2 = await submit_mod.submit(ex, None, wait_s=4)
        assert r2.status == "submitted", r2
        assert "confirmation" in r2.url or "confirmation_text" in r2.signal
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_enter_guard_and_choose_native(tmpdb):
    ex = await _open()
    try:
        snap = await snapshot.take(ex)
        first = _by_label(snap, "First Name")
        await ex.focus_ref(first["ref"])
        with pytest.raises(ClickRefused):
            await ex.keypress(["ENTER"])  # implicit form submit risk
        school = _by_label(snap, "School")
        res = await fill.fill_batch(ex, [{"ref": school["ref"], "value": "university of texas"}])
        assert res[0].ok and "Texas" in res[0].readback
    finally:
        await ex.close(close_browser=True)
