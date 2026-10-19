"""Oracle Recruiting Cloud (onsemi, 2026-10-19): the cx-select picker input holds its own pick, and the row around it
holds the label and the error text."""
from __future__ import annotations

from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body>
<div class="input-row__control">
  <label id="lm">Start Date Month</label>
  <input id="m" class="cx-select-input" role="combobox" aria-expanded="false" aria-labelledby="lm" value="">
  <button id="m-toggle" aria-label="Open the drop-down list for Month.">v</button>
  <span>Month Year</span>
  <div class="input-row__error">You need to enter the whole date otherwise the date will not be saved.</div>
</div>
<div class="input-row__control">
  <label id="ly">Start Date Year</label>
  <input id="y" class="cx-select-input" role="combobox" aria-expanded="false" aria-labelledby="ly" value="2026">
</div>
</body></html>"""


async def test_an_oracle_picker_reads_its_own_value_not_its_row():
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        snap = await snapshot.take(ex)
        month = next(i for i in snap.items if (i.get("label") or "").startswith("Start Date Month"))
        year = next(i for i in snap.items if (i.get("label") or "").startswith("Start Date Year"))
        assert month.get("combo") and not month.get("value") and not month.get("shown"), month
        assert year.get("value") == "2026", year
        info = await ex.read_ref(month["ref"])
        assert not info.get("shown") and info.get("value") == "", info
        await ex.page.evaluate("() => { document.getElementById('m').value = 'October'; }")
        info = await ex.read_ref(month["ref"])
        assert info.get("value") == "October" and not info.get("shown"), info
    finally:
        await ex.close()


MIRROR = """<html><body>
<div class="input-row"><label for="r">Responsibilities</label>
  <textarea id="r" class="input-row__control input-row__control--autoheight">Forecast baggage revenue</textarea>
  <textarea class="input-row__control input-row__control--helper" aria-hidden="true" tabindex="-1"
            style="position:absolute;top:-800px">Forecast baggage revenue</textarea></div>
</body></html>"""


async def test_an_aria_hidden_layout_mirror_is_not_a_field():
    """onsemi (2026-10-19): Oracle sizes each textarea with an aria-hidden copy; the gate wanted a decision for it."""
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(MIRROR)
        snap = await snapshot.take(ex)
        boxes = [i for i in snap.items if i.get("tag") == "textarea"]
        assert len(boxes) == 1 and boxes[0].get("label") == "Responsibilities", boxes
    finally:
        await ex.close()


OPT_IN = """<html><body>
<label><input type="checkbox" id="alerts" checked> I agree to receive updates about new job opportunities.</label>
<label><input type="checkbox" id="privacy"> I have read and agree to the privacy policy.</label>
</body></html>"""


async def test_a_ticked_marketing_opt_in_is_unticked_by_the_consent_pass(tmp_path):
    """onsemi (2026-10-19): Oracle's job-alert box stayed ticked and the gate wanted a decision for it."""
    from goat import answers, db
    from goat.adapters import for_portal
    db.reset_for_tests(tmp_path / "t.db")
    answers.save_answer("consent.marketing_email", "No", source="seed")
    answers.save_answer("consent.privacy_notice", "Yes", source="seed")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','onsemi','Intern','https://x.oraclecloud.com/a','oracle')")
        jid = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L1','filling','account',1,1)").lastrowid
    job = {"id": jid, "listing_id": "L1", "company": "onsemi", "title": "Intern", "ats": "oracle", "url": "https://x.oraclecloud.com/a"}
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(OPT_IN)
        snap = await snapshot.take(ex)
        res = await for_portal("oracle").prefill(ex, snap, job, 1)
        assert await ex.page.evaluate("() => document.getElementById('alerts').checked") is False, res.details
        assert await ex.page.evaluate("() => document.getElementById('privacy').checked") is True, res.details
    finally:
        await ex.close()


async def test_fill_batch_refuses_to_type_into_an_oracle_picker():
    """American Express (2026-10-19): typed ZIP / City / State text never committed in Oracle's pickers."""
    from goat.browser import fill
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        snap = await snapshot.take(ex)
        month = next(i for i in snap.items if (i.get("label") or "").startswith("Start Date Month"))
        out = await fill.fill_batch(ex, [{"ref": month["ref"], "value": "October"}])
        assert not out[0].ok and out[0].error.startswith("picker:"), out
    finally:
        await ex.close()
