from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from goat import db
from goat.dashboard import action, api_events, api_job, api_jobs, api_overview, serve


@pytest.fixture()
def seeded(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','SWE Intern','https://x.com/a','greenhouse')")
        con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at,hold_reason) VALUES('L1','hold','no_account',1,1,'no_standing_answer: SAT score')")
    db.event(1, "tool:snapshot", {"args": {"with_text": False}}, latency_ms=12)
    db.event(1, "model:step", {"effort": "low"}, model="gpt-6.1-sol", tokens_in=1000, tokens_cached=800, tokens_out=50, cost_usd=0.001)
    return 1


def test_api_functions(seeded):
    o = api_overview()
    assert o["jobs"] == {"hold": 1} and o["holds_by_reason"][0]["reason"] == "no_standing_answer"
    assert o["spend_usd"]["24h"] == 0.001
    rows = api_jobs({"status": ["hold"]})
    assert rows and rows[0]["company"] == "Acme"
    assert api_jobs({"q": ["nomatch"]}) == []
    j = api_job(1)
    assert j["job"]["title"] == "SWE Intern" and len(j["events"]) == 2 and j["cost_usd"] == 0.001
    assert j["events"][0]["detail"] == {"effort": "low"}
    assert api_events({"kind": ["tool:"]})[0]["kind"] == "tool:snapshot"
    assert action(1, "requeue", {})["ok"]
    assert api_jobs({})[0]["status"] == "queued"


def test_http_server_roundtrip(seeded):
    srv = serve(0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "goat dashboard" in html
        ov = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview").read())
        assert ov["jobs"]["hold"] == 1
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/job/1/hold", data=json.dumps({"reason": "test"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        assert json.loads(urllib.request.urlopen(req).read())["ok"]
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/api/job/1").status == 200
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/records/1/../../.env")
    finally:
        srv.shutdown()
