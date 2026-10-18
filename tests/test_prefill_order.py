"""Greenhouse clears a typed phone number when the country is picked afterwards: prefill picks the country
first, then types, and re-types anything a later step cleared."""
from __future__ import annotations

from goat import answers, db
from goat.adapters import for_portal
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body>
<div class="field"><label id="cl">Country</label>
  <button id="country" aria-haspopup="listbox" aria-labelledby="cl" style="width:200px;height:28px">Select...</button>
  <ul id="lb" role="listbox" style="display:none;list-style:none;padding:0">
    <li role="option" style="height:26px">United States</li><li role="option" style="height:26px">Canada</li></ul></div>
<div class="field"><label for="phone">Phone</label><input id="phone" type="tel" style="width:200px"></div>
<script>
  const c = document.getElementById('country'), lb = document.getElementById('lb'), phone = document.getElementById('phone');
  c.addEventListener('click', () => { lb.style.display = 'block'; });
  lb.querySelectorAll('li').forEach(li => li.addEventListener('click', () => {
    c.textContent = li.textContent; lb.style.display = 'none'; phone.value = '';      // the Greenhouse behaviour
  }));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') lb.style.display = 'none'; });
</script></body></html>"""


async def test_country_is_picked_before_the_phone_is_typed(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    answers.save_answer("identity.phone", "281-832-1205", source="seed")
    answers.save_answer("identity.country", "United States", source="seed")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','SWE Intern','https://boards.greenhouse.io/acme/jobs/1','greenhouse')")
        jid = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L1','filling','no_account',1,1)").lastrowid
    job = {"id": jid, "listing_id": "L1", "company": "Acme", "title": "SWE Intern", "ats": "greenhouse",
           "url": "https://boards.greenhouse.io/acme/jobs/1", "resume_variant": "swe"}
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        snap = await snapshot.take(ex)
        res = await for_portal("greenhouse").prefill(ex, snap, job, 1)
        assert await ex.page.evaluate("() => document.getElementById('country').textContent") == "United States"
        assert await ex.page.evaluate("() => document.getElementById('phone').value") == "281-832-1205", res.details
    finally:
        await ex.close()
