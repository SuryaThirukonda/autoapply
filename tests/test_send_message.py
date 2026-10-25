"""Work at a Startup (DimeHealth, 2026-10-25): the 'Reach out to Akash at Dime' dialog's Send was clicked with the click tool and
the message went to the founder past the gate and the judge. A Send beside a written message is a submit control."""
import pytest

from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<!doctype html><html><body>
<div role="dialog" aria-modal="true"><h2>Reach out to Akash at Dime</h2>
<textarea id="m">Hi Akash, I am a computer science student at Texas A&M focused on backend systems.</textarea>
<button id="s">Send</button></div>
<form><label for="q">Search</label><input id="q"><button id="go">Send</button></form>
</body></html>"""


@pytest.mark.asyncio
async def test_send_beside_a_written_message_is_a_submit_control(tmp_path):
    page = tmp_path / "waas.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        sends = [i for i in snap.items if i.get("tag") == "button" and (i.get("label") or "").strip() == "Send"]
        assert len(sends) == 2
        assert sends[0].get("submitish") is True          # the message dialog's Send
        assert not sends[1].get("submitish")              # a Send beside no written message is no application
    finally:
        await ex.close(close_browser=True)
