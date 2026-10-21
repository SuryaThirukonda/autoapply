"""Texas Instruments (2026-10-20): a Tealium cookie wall lay over the Oracle form and caught every picker click. The
harness closes such a layer: decline / necessary-only when offered, accept only when it blocks the page."""
import pytest

from goat.browser import fill
from goat.browser.executor import Executor, HostPolicy

FORM = '<h1>Job application form</h1><label for="g">Gender</label><select id="g"><option>Male</option></select>'


def wall(buttons, style="position:fixed;left:0;top:0;width:100%;height:100%;z-index:20100"):
    btns = "".join(f'<button onclick="document.getElementById(\'w\').remove(); window.picked = this.textContent">{b}</button>'
                   for b in buttons)
    return (f'<html><body>{FORM}<div id="w" style="{style};background:#fff">'
            f'<p>TI websites use cookies to optimize site functionality. Click Agree and Proceed to accept cookies.</p>{btns}</div></body></html>')


async def _run(html):
    ex = Executor(policy=HostPolicy(["about:blank"]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    await ex.page.set_content(html)
    try:
        out = await fill.clear_cookie_wall(ex)
        picked = await ex.page.evaluate("() => window.picked || null")
        gone = await ex.page.evaluate("() => !document.getElementById('w')")
        return out, picked, gone
    finally:
        await ex.close()


@pytest.mark.asyncio
async def test_a_blocking_wall_with_only_agree_is_accepted():
    out, picked, gone = await _run(wall(["MANAGE PREFERENCES", "AGREE AND PROCEED"]))
    assert out["action"] == "accept" and picked == "AGREE AND PROCEED" and gone


@pytest.mark.asyncio
async def test_a_decline_button_wins_over_accept():
    out, picked, gone = await _run(wall(["Accept All", "Reject All"]))
    assert out["action"] == "decline" and picked == "Reject All" and gone


@pytest.mark.asyncio
async def test_a_small_bar_with_only_accept_is_left_alone():
    out, picked, gone = await _run(wall(["Accept"], style="position:fixed;left:0;bottom:0;width:100%;height:60px"))
    assert out["clicked"] is None and picked is None and not gone


@pytest.mark.asyncio
async def test_a_page_without_a_cookie_layer_is_untouched():
    out, picked, _ = await _run(f"<html><body>{FORM}<button>Continue</button></body></html>")
    assert out is None and picked is None
