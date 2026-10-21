"""TikTok (2026-10-21): the account-creation puzzle is drawn in a page container, not an iframe; the run could not park it
for Surya and stalled. The challenge detector now sees it. Nothing here interacts with a challenge."""
import pytest

from goat.browser import captcha
from goat.browser.executor import Executor, HostPolicy

PUZZLE = """<html><body><h1>Create Account</h1>
<div class="captcha_verify_container" style="width:340px;height:260px;border:1px solid #ccc">
  <p>Select 2 objects that are the same shape</p><div class="captcha_verify_img"></div></div></body></html>"""
HIDDEN = """<html><body><h1>Create Account</h1><div class="captcha-holder" style="display:none;width:340px;height:260px"></div>
<input type="email"><button>Create</button></body></html>"""


async def _detect(html):
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(html)
        return await captcha.detect_challenge(ex)
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_a_page_drawn_puzzle_is_a_challenge():
    d = await _detect(PUZZLE)
    assert d["challenge"] is True and "captcha" in d["kinds"]


@pytest.mark.asyncio
async def test_a_hidden_captcha_container_is_not():
    d = await _detect(HIDDEN)
    assert d["challenge"] is False



@pytest.mark.asyncio
async def test_a_password_help_submit_is_not_an_application_submit():
    """Phillips 66 (2026-10-21): SuccessFactors' Password Help button is 'Submit'; it requests the reset email."""
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content("<html><head><title>Career Opportunities: Password Help</title></head><body><h1>Password Help</h1>"
                                  "<input type='email' value='x@y.z'><button type='submit' id='s' style='width:120px;height:40px'>Submit</button>"
                                  "</body></html>")
        box = await ex.page.evaluate("() => { const r = document.getElementById('s').getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }")
        el = await ex.element_at(int(box[0]), int(box[1]))
        assert el.submit is False
    finally:
        await ex.close()


CHOOSER_ONLY = """<html><body><button id="up" type="button" style="width:120px;height:30px">Upload resume</button><span id="got"></span>
<script>
  document.getElementById('up').addEventListener('click', () => {
    const i = document.createElement('input'); i.type = 'file';
    i.addEventListener('change', () => { document.getElementById('got').textContent = i.files[0].name; });
    i.click();
  });
</script></body></html>"""


@pytest.mark.asyncio
async def test_an_upload_button_that_only_opens_the_file_chooser_still_takes_the_file(tmp_path):
    """Google Careers (2026-10-21): no file input on the page; the button opens the browser's chooser."""
    f = tmp_path / "Surya_Thirukonda_Resume.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(CHOOSER_ONLY)
        await ex.page.evaluate("() => document.getElementById('up').setAttribute('data-goat-ref', 'e1')")
        await ex.set_file("e1", str(f))
        assert await ex.page.evaluate("() => document.getElementById('got').textContent") == f.name
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_eightfolds_consent_step_submit_is_an_account_action():
    """Qualcomm (2026-10-21): 'Please review before continuing' has a Submit that creates the account; the run parked on it."""
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content("<html><body><h2>Please review before continuing</h2><input value='a'><input value='b'><input value='c'>"
                                  "<button type='submit' id='s' style='width:120px;height:40px'>Submit</button></body></html>")
        box = await ex.page.evaluate("() => { const r = document.getElementById('s').getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }")
        el = await ex.element_at(int(box[0]), int(box[1]))
        assert el.submit is False
    finally:
        await ex.close()
