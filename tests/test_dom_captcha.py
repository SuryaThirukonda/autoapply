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
