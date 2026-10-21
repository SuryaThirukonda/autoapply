"""The worker closes every page no active job points at (62 open tabs made connect_over_cdp time out, 2026-10-21),
keeps held / queued jobs' tabs and extension pages, and never closes the last page."""
from types import SimpleNamespace

import pytest

from goat import db, worker


class FakePage:
    def __init__(self, tid, url="https://jobs.example.com/x"):
        self.tid, self.url, self.closed = tid, url, False

    async def close(self):
        self.closed = True


class FakeCDP:
    def __init__(self, tid):
        self.tid = tid

    async def send(self, method):
        return {"targetInfo": {"targetId": self.tid}}

    async def detach(self):
        pass


class FakeContext:
    def __init__(self, pages):
        self.pages = pages

    async def new_cdp_session(self, page):
        return FakeCDP(page.tid)


@pytest.fixture()
def jobs(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Acme','Intern','https://x.example/job','workday')")
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l2','Beta','Intern','https://y.example/job','workday')")
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l3','Gamma','Intern','https://z.example/job','workday')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority,tab_target_id) VALUES(1,'l1','hold','account',0,'T1')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority,tab_target_id) VALUES(2,'l2','queued','account',0,'T2')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority,tab_target_id) VALUES(3,'l3','dead','account',0,'T5')")


@pytest.mark.asyncio
async def test_pages_no_active_job_keeps_are_closed(jobs):
    pages = [FakePage("T1"), FakePage("T2"), FakePage("T3"), FakePage("T4"), FakePage("T5"),
             FakePage("T6", "chrome-extension://abc/popup/index.html")]
    await worker._prune_held_tabs(SimpleNamespace(_context=FakeContext(pages)))
    assert [p.tid for p in pages if p.closed] == ["T3", "T4", "T5"]


@pytest.mark.asyncio
async def test_the_last_page_is_never_closed(jobs):
    pages = [FakePage("T8"), FakePage("T9")]
    await worker._prune_held_tabs(SimpleNamespace(_context=FakeContext(pages)))
    assert [p.closed for p in pages] == [True, False]
