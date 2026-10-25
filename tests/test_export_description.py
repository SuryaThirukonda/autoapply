"""Antares (Ashby, 2026-10-24): the Export Compliance question's description 'To conform to U.S. Government export regulations,
applicant must be a U.S. person ...' was read as an error under a ticked 'I am currently a U.S. Person', and the gate held the
draft three times."""
from goat import db
from goat.browser import errors
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body style="font:14px sans-serif"><form>
<fieldset><legend>Export Compliance *</legend>
  <div>To conform to U.S. Government export regulations, applicant must be a U.S. person or eligible for an export license.</div>
  <label><input type="radio" name="ex" checked> I am currently a "U.S. Person"</label>
  <label><input type="radio" name="ex"> I will soon become a "U.S. Person"</label>
</fieldset></form></body></html>"""


async def test_an_export_regulations_description_is_not_an_error(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        scan = await errors.scan_errors(ex)
        assert not scan.get("error_text"), scan.get("error_text")
    finally:
        await ex.close()
