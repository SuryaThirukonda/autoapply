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



FOOTER = """<html><body><header><nav><a href="/x">Careers home</a></nav></header>
<main><form><label for="em">Email Address</label><input id="em" type="email">
<footer class="apply-flow-pagination__buttons"><button type="button">CANCEL</button><button type="button">NEXT</button></footer></form></main>
<footer><a href="/privacy">Privacy</a></footer></body></html>"""


async def test_a_forms_own_footer_buttons_are_in_the_snapshot():
    """AutoZone (2026-10-19): Oracle's NEXT sits in a <footer> inside the form and was dropped as site chrome."""
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(FOOTER)
        snap = await snapshot.take(ex)
        labels = [i.get("label") for i in snap.items]
        assert "NEXT" in labels and "CANCEL" in labels, labels
        assert "Privacy" not in labels and "Careers home" not in labels, labels
    finally:
        await ex.close()



async def test_a_stalled_screenshot_falls_back_to_chromes_own_capture():
    """Walmart (2026-10-20): Page.screenshot timed out waiting for fonts and the run crashed."""
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content("<html><body><h1>Hello</h1></body></html>")

        async def stalled(*a, **k):
            raise TimeoutError("Page.screenshot: Timeout 30000ms exceeded.")
        ex.page.screenshot = stalled
        png = await ex.screenshot_png()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 100
    finally:
        await ex.close()



UPLOAD = """<html><body><div data-automation-id="formField-transcript"><label>Transcript</label>
<div class="drop"><button type="button" id="b">Select files</button><input type="file" id="f" style="display:none"></div></div></body></html>"""


async def test_a_file_button_uploads_through_its_file_input(tmp_path):
    """Auto-Owners (2026-10-20): upload_resume was given the 'Select files' button and Playwright refused it."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(UPLOAD)
        await ex.page.evaluate("() => document.getElementById('b').setAttribute('data-goat-ref', 'e9')")
        await ex.set_file("e9", str(f))
        name = await ex.page.evaluate("() => document.getElementById('f').files[0] && document.getElementById('f').files[0].name")
        assert name == "doc.pdf"
    finally:
        await ex.close()



def test_a_gpa_answer_picks_the_bucket_that_holds_his_gpa():
    """Klaviyo (2026-10-19): the GPA chain landed on '3.5 - 3.74' for a 4.0."""
    from goat.browser.fill import best_option
    chain = ["4.0 or above", "4.0 and above", "4.0+", "3.7 - 4.0", "3.7-4.0", "3.75 - 4.0", "3.5 - 4.0", "3.5+", "3.5 or above"]
    def opts(*xs):
        return [{"text": x} for x in xs]
    klaviyo = opts("Below 2", "2 - 2.24", "2.25 - 2.49", "2.5 - 2.74", "2.75 - 2.99", "3 - 3.24", "3.25 - 3.49", "3.5 - 3.74", "3.75 - 4", "4+")
    assert best_option(klaviyo, chain)["text"] == "4+"
    assert best_option(opts("3.75+", "3.41 - 3.74", "Below 3.4"), chain)["text"] == "3.75+"
    assert best_option(opts("3.7 - 4.0", "3.1 - 3.6", "3.0 or under"), chain)["text"] == "3.7 - 4.0"
    assert best_option(opts("4.0 or higher", "3.5 - 3.99", "3.49 - 3.0", "2.99 or below"), chain)["text"] == "4.0 or higher"
    assert best_option(opts("Yes", "No"), chain) is None
    # a number-plus answer never prefix-matches a narrower bucket
    assert best_option(opts("5 - 10 years", "Below 5 years"), ["5+"]) is None
    assert best_option(opts("5 - 10 years", "5+"), ["5+"])["text"] == "5+"
    # years chains (whole numbers) keep their text matching
    assert best_option(opts("0-1 years", "1-3 years", "3-5 years"), ["1", "1 year", "1-3 years"])["text"] == "1-3 years"



COVERED = """<!doctype html><html><body style="margin:20px">
<h2>Forgot Password</h2>
<label for="em">Email Address</label><br><input id="em" type="text" style="width:320px"><br><br>
<button type="button" id="go" onclick="window.__reset = (window.__reset || 0) + 1">Reset Password</button>
<div id="menu" style="display:none;position:absolute;z-index:99;background:#fff;border:1px solid #888">Unlock your account to view autofill suggestions</div>
<script>
  const em = document.getElementById('em'), m = document.getElementById('menu');
  em.addEventListener('focus', () => { const r = em.getBoundingClientRect(); const b = document.getElementById('go').getBoundingClientRect();
    m.style.display = 'block'; m.style.left = r.left + 'px'; m.style.top = (r.bottom + 2) + 'px'; m.style.width = '340px';
    m.style.height = (b.bottom - r.bottom + 10) + 'px'; });
  em.addEventListener('blur', () => { m.style.display = 'none'; });
</script>
</body></html>"""


async def test_a_click_reaches_a_button_under_a_password_managers_menu(tmp_path):
    """Northwestern Mutual (2026-10-20): Bitwarden's inline menu under the focused email box covered Reset Password."""
    from goat.browser import snapshot
    page = tmp_path / "forgot.html"
    page.write_text(COVERED, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        btn = next(i["ref"] for i in snap.items if (i.get("label") or "").startswith("Reset Password"))
        await ex.page.focus("#em")
        assert await ex.page.evaluate("() => getComputedStyle(document.getElementById('menu')).display") == "block"
        await ex.click_ref(btn)
        assert await ex.page.evaluate("() => window.__reset || 0") == 1
    finally:
        await ex.close()



def test_a_decline_chain_takes_any_declining_wording():
    """GDIT (2026-10-20): 'I do not wish to disclose (United States of America)' was offered and the chain picked Asian."""
    from goat.browser.fill import best_option
    chain = ["decline", "Decline to self-identify", "I do not wish to answer", "Asian", "Asian (United States of America)"]
    opts = [{"text": t} for t in ["American Indian or Alaska Native (United States of America)", "Asian (Not Hispanic or Latino) (United States of America)",
                                   "White (United States of America)", "I do not wish to disclose (United States of America)"]]
    assert best_option(opts, chain)["text"] == "I do not wish to disclose (United States of America)"
    no_decline = [o for o in opts if "wish" not in o["text"]]
    assert best_option(no_decline, chain + ["Asian (Not Hispanic or Latino) (United States of America)"])["text"].startswith("Asian")
    # a chain that does not start with 'decline' is untouched
    assert best_option([{"text": "Male"}, {"text": "I do not wish to disclose"}], ["Male", "Man"])["text"] == "Male"



def test_a_salary_range_is_picked_by_the_amount_it_holds():
    """CACI (2026-10-20): 'Up to $40,000', '$40,000-$60,000', ... for his $41,600 a year went to Surya."""
    from goat.browser.fill import best_option
    def opts(*xs):
        return [{"text": x} for x in xs]
    caci = opts("Up to $40,000", "$40,000-$60,000", "$60,000-$80,000", "$80,000-$100,000", "$100,000-$120,000")
    assert best_option(caci, ["41600"])["text"] == "$40,000-$60,000"
    assert best_option(opts("$0 - $50,000", "$50,001 - $75,000", "$75,001+"), ["41600"])["text"] == "$0 - $50,000"
    assert best_option(opts("$15 - $19.99 per hour", "$20 - $24.99 per hour", "$25+ per hour"), ["41600"])["text"] == "$20 - $24.99 per hour"
    assert best_option(opts("40k-60k", "60k-80k"), ["41600"])["text"] == "40k-60k"
    assert best_option(opts("Yes", "No"), ["41600"]) is None
    assert best_option(opts("2027 - 2028", "2028 - 2029"), ["41600"]) is None
