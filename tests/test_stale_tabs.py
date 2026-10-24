"""2026-10-24 10:11: Chrome held 42 tabs left by held and finished jobs, and connect_over_cdp (which attaches to every page)
took longer than its 15 s timeout: every claim failed 'browser_down'. Tabs no live or ready-for-you job uses are closed once
there are too many."""
import pytest

from goat import db
from goat.browser import chrome


@pytest.mark.asyncio
async def test_stale_tabs_are_closed_past_the_cap_and_live_ones_kept(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','Intern','https://x.example/j','workday')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,tab_target_id,created_at,updated_at) VALUES(1,'L1','ready_for_you','no_account','KEEP',1,1)")
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L2','Acme','Intern 2','https://x.example/k','workday')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,tab_target_id,created_at,updated_at) VALUES(2,'L2','hold','no_account','T3',1,1)")
    pages = [{"id": "KEEP", "type": "page"}] + [{"id": f"T{i}", "type": "page"} for i in range(30)]
    closed = []

    async def list_targets(base=None):
        return pages

    async def close_target(tid, base=None):
        closed.append(tid)
    monkeypatch.setattr(chrome, "_list_targets", list_targets)
    monkeypatch.setattr(chrome, "_close_target", close_target)
    n = await chrome.close_stale_tabs(cap=20)
    assert n == 30 and "KEEP" not in closed and "T3" in closed
    closed.clear()
    pages[:] = pages[:5]
    assert await chrome.close_stale_tabs(cap=20) == 0 and closed == []
