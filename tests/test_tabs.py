"""Tabs and file pickers, offline with Playwright's bundled Chromium and a local HTTP server: a click that
opens the form in a new tab is followed, an off-site popup is closed, the tabs tool lists / switches /
closes, and a custom upload button's file picker is intercepted and answered."""
from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from goat import db
from goat.browser.executor import Executor, HostPolicy, NavigationRefused

PAGES = {
    "/form": """<html><body><h1>Job</h1>
      <a id="apply" href="/apply" target="_blank" style="display:block;width:200px;height:40px">Apply in a new tab</a>
      <button id="offsite" style="display:block;width:200px;height:40px" onclick="window.open('http://127.0.0.2:%PORT%/elsewhere')">Share</button>
      <button id="upload" style="display:block;width:200px;height:40px" onclick="document.getElementById('f').click()">Upload résumé</button>
      <input type="file" id="f" style="display:none">
      </body></html>""",
    "/apply": "<html><body><h1>Application form</h1><input name='first'></body></html>",
    "/elsewhere": "<html><body>somewhere else</body></html>",
}


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], "not found").replace("%PORT%", str(self.server.server_address[1]))
        self.send_response(200 if self.path.split("?")[0] in PAGES else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


@pytest.fixture()
def site(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


async def _open(base: str) -> Executor:
    ex = Executor(policy=HostPolicy([base + "/form"]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    await ex.page.goto(base + "/form")
    await ex.settle(quiet_ms=100)
    return ex


async def _center(ex: Executor, sel: str) -> tuple[int, int]:
    box = await ex.page.locator(sel).bounding_box()
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


async def test_a_new_tab_on_the_application_is_followed_and_tabs_can_be_switched(site):
    ex = await _open(site)
    try:
        await ex.click_xy(*await _center(ex, "#apply"))
        note = await ex.follow_new_tab()
        assert "new tab" in note and ex.url.endswith("/apply")
        tabs = await ex.list_tabs()
        assert [t["current"] for t in tabs] == [False, True]
        await ex.switch_tab(0)
        assert ex.url.endswith("/form")
        out = await ex.close_tab(1)
        assert out["closed"] == 1 and len(ex.job_pages) == 1
        with pytest.raises(NavigationRefused):
            await ex.close_tab(0)            # never the last tab
        with pytest.raises(NavigationRefused):
            await ex.switch_tab(5)
    finally:
        await ex.close()


async def test_an_offsite_popup_is_closed(site):
    ex = await _open(site)
    try:
        await ex.click_xy(*await _center(ex, "#offsite"))
        note = await ex.follow_new_tab()
        assert "outside this application" in note
        assert ex.url.endswith("/form") and len(ex.job_pages) == 1
    finally:
        await ex.close()


async def test_a_file_picker_is_intercepted_and_answered(site, tmp_path):
    ex = await _open(site)
    try:
        f = tmp_path / "Surya_Thirukonda_Resume_SWE.pdf"
        f.write_bytes(b"%PDF-1.4 test")
        await ex.click_xy(*await _center(ex, "#upload"))
        await ex.page.wait_for_timeout(300)
        note = await ex.follow_new_tab()
        assert ex.pending_chooser is not None and "file picker" in note
        await ex.pending_chooser.set_files(str(f))
        name = await ex.page.evaluate("() => document.getElementById('f').files[0].name")
        assert name == f.name
    finally:
        await ex.close()


def test_the_tabs_tool_is_offered_to_the_model():
    from goat.agent.tools import FUNCTION_TOOLS, ToolDispatcher
    t = next(t for t in FUNCTION_TOOLS if t["name"] == "tabs")
    assert set(t["parameters"]["properties"]["action"]["enum"]) == {"list", "switch", "close", "back", "reload", "goto"}
    assert hasattr(ToolDispatcher, "t_tabs")
    json.dumps(FUNCTION_TOOLS)
