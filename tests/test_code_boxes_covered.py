"""Texas Instruments (2026-10-20): a cookie wall lay over Oracle's six code boxes, the focusing click landed on the
wall, focus stayed on <body> and the code Surya typed was lost. The code now lands whatever covers the boxes."""
import pytest

from goat.browser import fill, snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body>
<h1>Confirm Your Identity</h1>
<div id="boxes">
""" + "".join(f'<input id="pin-code-{i}" type="text" inputmode="numeric" aria-label="Enter verification code digit {i} of six.">'
              for i in range(1, 7)) + """
</div>
<button>VERIFY</button>
<div class="privacy_prompt" style="position:fixed;left:0;top:0;width:100%;height:100%;z-index:20100;background:rgba(0,0,0,.4)">
  <p>TI websites use cookies. Click Agree and Proceed to accept cookies and enter the site.</p>
</div>
<script>
const boxes = Array.from(document.querySelectorAll('#boxes input'));
boxes.forEach((b, i) => b.addEventListener('input', () => { b.value = b.value.slice(-1); if (b.value && boxes[i + 1]) boxes[i + 1].focus(); }));
</script>
</body></html>"""


@pytest.mark.asyncio
async def test_the_code_lands_in_boxes_under_a_cookie_wall():
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        snap = await snapshot.take(ex)
        refs = [i["ref"] for i in snap.items if "verification code digit" in (i.get("label") or "")]
        assert len(refs) == 6
        assert await fill.type_code_into(ex, refs, "913700")
        got = await ex.page.evaluate("() => Array.from(document.querySelectorAll('#boxes input')).map(b => b.value).join('')")
        assert got == "913700"
        assert await ex.page.evaluate("() => !!document.querySelector('.privacy_prompt')")     # the wall was never clicked
    finally:
        await ex.close()
