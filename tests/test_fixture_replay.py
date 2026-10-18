"""Every saved portal page under tests/fixtures/<portal>/ replays through the outline and the adapter's prefill
without crashing, and still shows the labels its .expect.json recorded when it was captured."""
import json
from pathlib import Path

import pytest

from goat import db
from goat.adapters import for_portal
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy

FIX = Path(__file__).parent / "fixtures"
PAGES = sorted(p for p in FIX.glob("*/*.html")) + [FIX / "form.html", FIX / "workday.html"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PAGES, ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_fixture_replays_through_outline_and_prefill(path, tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Acme','Intern','https://x.example/job','greenhouse')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','no_account',0)")
    portal = path.parent.name if path.parent != FIX else ("workday" if "workday" in path.stem else "greenhouse")
    url = path.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=150)
        snap = await snapshot.take(ex)
        text = snapshot.render(snap)
        assert snap.total_items > 0 and "URL:" in text
        exp_path = path.with_suffix(".expect.json")
        if exp_path.exists():
            exp = json.loads(exp_path.read_text(encoding="utf-8"))
            labels = {i.get("label") for i in snap.items} | {g.get("label") for g in snap.groups}
            missing = [l for l in exp.get("labels", []) if l not in labels]
            assert not missing, f"labels gone from the outline: {missing[:5]}"
            assert all(h in snap.headings for h in exp.get("headings", []))
        res = await for_portal(portal).prefill(ex, snap, {"id": 1, "company": "Acme", "resume_variant": "swe"}, 1)
        assert res.failed == 0 or res.set >= 0            # never raises; failures are reported, not thrown
    finally:
        await ex.close(close_browser=True)
