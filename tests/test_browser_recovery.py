"""Chrome answers but a tab does not: goat closes the hung tab so Playwright can attach again, and the worker retries
as soon as Chrome is back (2026-10-22 21:19-21:55: two hung tabs held up the queue for 35 minutes)."""
import asyncio
import socket
import urllib.parse

import httpx
import pytest

from goat import worker
from goat.browser import chrome


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_a_hung_tab_is_found_and_closed():
    from playwright.async_api import async_playwright
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[f"--remote-debugging-port={port}"])
        try:
            page = await browser.new_page()
            await page.goto("data:text/html,<p>fine</p>")
            busy = "data:text/html," + urllib.parse.quote("<script>setTimeout(function(){while(true){}},200)</script>")
            async with httpx.AsyncClient() as c:
                hung_id = (await c.put(f"{base}/json/new?{busy}")).json()["id"]
            await asyncio.sleep(1.0)
            closed = await chrome.close_unresponsive_tabs(base, timeout=2.0)
            assert [t["id"] for t in closed] == [hung_id]
            await asyncio.sleep(0.5)
            async with httpx.AsyncClient() as c:
                ids = [t["id"] for t in (await c.get(f"{base}/json/list")).json() if t.get("type") == "page"]
            assert hung_id not in ids and ids
            assert await page.evaluate("1 + 1") == 2           # the tab that still answers is left alone
        finally:
            await browser.close()


def test_the_backoff_ends_when_chrome_is_back(monkeypatch):
    monkeypatch.setattr(worker.CONTROL, "browser_back", False)
    assert not worker.backoff_over(100.0, 50.0)
    assert worker.backoff_over(100.0, 150.0)
    worker.CONTROL.browser_back = True
    assert worker.backoff_over(100.0, 50.0) and worker.CONTROL.browser_back is False
