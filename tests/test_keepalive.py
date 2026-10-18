"""Oracle's "Are You Still With Us?" session-timeout dialog is answered with Continue before the page is
read; sign-out buttons and unrelated dialogs are never pressed."""
from __future__ import annotations

from goat import db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body>
<input aria-label="First Name">
<div role="dialog" id="timeout" style="position:fixed;top:40px;left:40px;width:360px;height:160px;background:#fff;border:1px solid #888">
  <h2>Are You Still With Us?</h2><p>Your session will expire in 2 minutes.</p>
  <button onclick="window.SIGNED_OUT = true">Sign Out</button>
  <button onclick="document.getElementById('timeout').remove(); window.KEPT = true">Continue</button>
</div>
<div role="dialog" id="confirm" style="position:fixed;top:240px;left:40px;width:360px;height:100px;background:#fff">
  <p>Ready to send your application?</p><button onclick="window.SENT = true">Yes</button>
</div>
</body></html>"""


async def test_the_timeout_dialog_is_answered_and_nothing_else_is_pressed(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        await snapshot.take(ex)
        state = await ex.page.evaluate("() => [!!window.KEPT, !!window.SIGNED_OUT, !!window.SENT, !!document.getElementById('timeout')]")
        assert state == [True, False, False, False]
        assert await snapshot.keep_session_alive(ex) is False        # nothing left to answer; the send dialog is untouched
        assert await ex.page.evaluate("() => !!window.SENT") is False
    finally:
        await ex.close()
