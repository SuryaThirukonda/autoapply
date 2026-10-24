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



LOC_PAGE = """<html><body>
<div class="field"><label for="loc">Which available location(s) on the posting are you open to relocating to? Please only select
locations that are listed on the posting. Type city and states below (e.g. Palmdale, CA).</label>
<textarea id="loc" style="width:300px;height:40px">Anywhere in the United States</textarea></div>
<div class="field"><label for="loc2">Which of the locations listed in the job posting would you accept?</label>
<input id="loc2" style="width:200px"></div>
</body></html>"""


async def test_prefill_types_the_posting_locations_into_a_box_that_asks_for_them(tmp_path):
    """Northrop Grumman (2026-10-19): the box held 'Anywhere in the United States' from his relocation answer."""
    db.reset_for_tests(tmp_path / "t.db")
    answers.save_answer("availability.relocation_scope", "Anywhere in the United States", source="seed")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats,locations) VALUES('L1','Northrop Grumman','SWE Intern',"
                    "'https://ngc.wd1.myworkdayjobs.com/x','workday','[\"Aurora, CO\"]')")
        jid = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L1','filling','no_account',1,1)").lastrowid
    job = {"id": jid, "listing_id": "L1", "company": "Northrop Grumman", "title": "SWE Intern", "ats": "workday",
           "url": "https://ngc.wd1.myworkdayjobs.com/x", "locations": '["Aurora, CO"]'}
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(LOC_PAGE)
        snap = await snapshot.take(ex)
        res = await for_portal("greenhouse").prefill(ex, snap, job, 1)
        assert await ex.page.evaluate("() => document.getElementById('loc').value") == "Aurora, CO", res.details
        assert await ex.page.evaluate("() => document.getElementById('loc2').value") == "Aurora, CO", res.details
    finally:
        await ex.close()


CHAIN_PAGE = """<html><body><form>
<div class="field"><label for="loc">Where are you currently located?*</label><input id="loc" type="text" style="width:300px"></div>
</form></body></html>"""


async def test_a_text_box_gets_the_first_entry_of_a_choice_chain(tmp_path):
    """Panthalassa (Greenhouse, 2026-10-24) went out with 'Sugar Land, Texas, United States; Sugar Land, TX, United States; ...'
    typed into 'Where are you currently located?': the prefill typed his whole wording chain."""
    db.reset_for_tests(tmp_path / "t.db")
    answers.save_answer("identity.location_city", "Sugar Land, Texas, United States; Sugar Land, TX, United States; Sugar Land",
                        answer_type="choice_chain", source="seed")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','SWE Intern','https://boards.greenhouse.io/acme/jobs/1','greenhouse')")
        jid = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L1','filling','no_account',1,1)").lastrowid
    job = {"id": jid, "listing_id": "L1", "company": "Acme", "title": "SWE Intern", "ats": "greenhouse",
           "url": "https://boards.greenhouse.io/acme/jobs/1", "resume_variant": "swe"}
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(CHAIN_PAGE)
        snap = await snapshot.take(ex)
        res = await for_portal("greenhouse").prefill(ex, snap, job, 1)
        assert await ex.page.evaluate("() => document.getElementById('loc').value") == "Sugar Land, Texas, United States", res.details
    finally:
        await ex.close()
